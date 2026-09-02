# ============================================================
# AUTOQUENCE FIREBASE AUTH
#
# Flow:
#   browser  -> Firebase JS SDK (email/password, Google popup,
#               email-link "magic link") -> Firebase ID token
#   browser  -> POST /api/auth/session { idToken }
#   server   -> verifies the ID token with firebase-admin
#               (signature checked against Google's public
#               certificates; only the project ID is required,
#               no service-account JSON) -> issues a signed
#               Flask session cookie
#   browser  -> every later request carries the cookie
#               (works for fetch, form posts and video tags)
#
# Set FIREBASE_PROJECT_ID to enable auth. If it is unset the
# guard is disabled and a warning is printed, so local dev
# keeps working without Firebase.
# ============================================================

import os
import time

import requests
from flask import (
    Blueprint,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)

# Load .env BEFORE reading the env var below. This module is imported
# from app.py earlier than app.py's own load_dotenv() call, so without
# this the project ID would be read from an empty environment.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

auth_bp = Blueprint("auth", __name__)

# PostHog capture (server-side, best effort). The project API key is
# public (it also ships in the client-side posthog-js snippet in
# templates/login.html). Used as the reliable source of truth for
# user_signed_up / user_signed_in counting, since the client-side
# event is skipped whenever an adblocker blocks posthog-js. Server
# events carry a "server_" prefix so insights can pick one source
# without double-counting.
POSTHOG_API_KEY = "phc_vHshobJ7D7CZLrgevyGTDH5d6FDvgUPUDdY6tZQ5MQks"
POSTHOG_HOST = "https://us.i.posthog.com"


def _posthog_capture(event, distinct_id, properties=None):
    """Fire-and-forget PostHog event; never raises, never blocks auth."""
    try:
        requests.post(
            f"{POSTHOG_HOST}/capture/",
            json={
                "api_key": POSTHOG_API_KEY,
                "event": event,
                "distinct_id": distinct_id,
                "properties": properties or {},
            },
            timeout=3,
        )
    except Exception as exc:  # noqa: BLE001 - analytics must not break auth
        print(f"WARNING: PostHog capture failed ({event}): {exc}")


FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
AUTH_ENABLED = bool(FIREBASE_PROJECT_ID)

# Routes reachable without a session. /static is also exempt
# (Flask serves it directly), which keeps JS/CSS/images public;
# uploaded/output media live there too and are therefore not
# token-protected either.
PUBLIC_PATHS = ("/login", "/api/auth/session", "/logout", "/healthz")

if AUTH_ENABLED:
    try:
        import google.auth.jwt

        # Firebase ID tokens are RS256 JWTs signed by Google's token
        # service. Their public x509 certificates are published at a
        # well-known URL, so verification needs ONLY the project ID -
        # no service-account JSON, no Application Default Credentials.
        _CERTS_URL = (
            "https://www.googleapis.com/robot/v1/metadata/x509/"
            "securetoken@system.gserviceaccount.com"
        )
        _certs_cache = {"certs": None, "fetched_at": 0.0}
        _CERTS_TTL = 3600

        def _get_certs():
            now = time.time()
            if (
                _certs_cache["certs"] is None
                or now - _certs_cache["fetched_at"] > _CERTS_TTL
            ):
                resp = requests.get(_CERTS_URL, timeout=10)
                resp.raise_for_status()
                # x509 PEM dict keyed by key id; decode() accepts it.
                _certs_cache["certs"] = resp.json()
                _certs_cache["fetched_at"] = now
            return _certs_cache["certs"]

        def _fb_verify(token, check_revoked=False):
            # google.auth.jwt.decode validates the RS256 signature
            # against Google's published certs plus exp; raises on any
            # failure. Issuer/audience are checked here.
            claims = google.auth.jwt.decode(
                token,
                certs=_get_certs(),
                audience=FIREBASE_PROJECT_ID,
            )
            if claims.get("iss") != (
                f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}"
            ):
                raise ValueError("Invalid token issuer")
            return claims
    except Exception as exc:  # pragma: no cover
        AUTH_ENABLED = False
        print(f"WARNING: Firebase verification unavailable, auth disabled: {exc}")
else:
    print(
        "WARNING: FIREBASE_PROJECT_ID is not set - "
        "authentication is DISABLED (open app)."
    )


def _is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/static/"):
        return True
    if path.startswith("/favicon"):
        return True
    return False


@auth_bp.before_app_request
def require_login():
    """Gate every request behind a valid Firebase-backed session."""
    if not AUTH_ENABLED or _is_public(request.path):
        return None

    if session.get("uid"):
        return None

    # Browser JS calls get a JSON 401; page loads get redirected.
    if (
        request.path.startswith("/api/")
        or request.path.startswith("/export/status/")
        or (
            request.method == "POST"
            and not request.path.startswith(("/upload", "/edit", "/export"))
        )
    ):
        return jsonify(error="unauthenticated"), 401

    return redirect(url_for("auth.login_page", next=request.full_path))


@auth_bp.route("/login")
def login_page():
    if session.get("uid"):
        return redirect(url_for("index"))
    return render_template_login()


def render_template_login():
    # Imported here to avoid a circular import at module load.
    from flask import render_template

    return render_template("login.html")


@auth_bp.post("/api/auth/session")
def create_session():
    """Exchange a Firebase ID token for a signed Flask session."""
    if not AUTH_ENABLED:
        # Auth disabled (dev): accept anything so the app still works.
        session["uid"] = "dev-local"
        session["email"] = "dev@local"
        return jsonify(ok=True)

    data = request.get_json(silent=True) or {}
    id_token = str(data.get("idToken") or "").strip()
    if not id_token:
        return jsonify(error="idToken required"), 400

    try:
        decoded = _fb_verify(id_token, check_revoked=True)
    except Exception as exc:
        return jsonify(error=f"Invalid token: {exc}"), 401

    session.clear()
    # Firebase ID tokens carry the user id in "sub" (and "user_id"),
    # not "uid" - accept any of the three spellings.
    uid = decoded.get("uid") or decoded.get("user_id") or decoded.get("sub")
    session["uid"] = uid
    # Stats: first session for this uid = sign-up, else sign-in.
    try:
        import stats
        email = decoded.get("email", "")
        if stats.is_new_user(uid):
            stats.log_event(email, uid, "signed_up")
        else:
            stats.log_event(email, uid, "signed_in")
    except Exception:
        pass
    session["email"] = decoded.get("email", "")
    session["name"] = decoded.get("name", "")
    # Session cookie lifetime (31 days).
    session.permanent = True
    # Server-side PostHog event (fire-and-forget). isSignup is decided
    # client-side (Firebase creationTime vs lastSignInTime, or the
    # signup form toggle) and forwarded in the request body.
    is_signup = bool(data.get("isSignup"))
    auth_method = str(data.get("method") or "unknown")
    _posthog_capture(
        "server_user_signed_up" if is_signup else "server_user_signed_in",
        uid,
        {"method": auth_method, "email": session["email"], "name": session["name"]},
    )
    return jsonify(ok=True)


@auth_bp.post("/logout")
@auth_bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login_page"))


@auth_bp.get("/api/me")
def me():
    import stats
    return jsonify(
        uid=session.get("uid"),
        email=session.get("email", ""),
        name=session.get("name", ""),
        is_admin=stats.is_admin(session.get("email", "")),
    )


@auth_bp.get("/healthz")
def healthz():
    return jsonify(ok=True, auth_enabled=AUTH_ENABLED)
