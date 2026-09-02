// ============================================================
// AUTOQUENCE AUTH (Firebase)
//
// Loaded on both the login page and the app pages.
//  - Login page: email/password and Google sign-in. On success
//    the Firebase ID token is POSTed to /api/auth/session which
//    sets a signed Flask session cookie; then we redirect to the app.
//  - App pages: verifies the session via /api/me (redirects to
//    /login when it 401s) and builds a profile avatar menu on
//    the index page only (wherever #profileSlot exists).
// ============================================================

// Firebase is loaded with a *dynamic* import so a blocked/failed CDN
// request can never kill the whole module: the avatar + session check
// only need the Flask session, not Firebase.
let auth = null;
try {
  if (
    window.FIREBASE_CONFIG &&
    !Object.values(window.FIREBASE_CONFIG).some((v) =>
      String(v).startsWith("PASTE_")
    )
  ) {
    const [{ initializeApp }, firebaseAuth] = await Promise.all([
      import("https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js"),
      import("https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js"),
    ]);
    const app = initializeApp(window.FIREBASE_CONFIG);
    auth = firebaseAuth.getAuth(app);
    // Keep the Firebase user on disk so a browser restart / language switch /
    // old tab does not silently drop them. This is the source of truth that
    // lets us always re-mint the server session cookie (Plan A).
    try {
      await firebaseAuth.setPersistence(auth, firebaseAuth.browserLocalPersistence);
    } catch (_) { /* persistence is best-effort; auth still works in-memory */ }
    // Re-export the pieces the login page needs onto window-level
    // helpers used below (signInWithPopup etc. come from firebaseAuth).
    Object.assign(window.__authKit = {}, firebaseAuth, { _app: app });
  } else {
    console.warn("[AUTH] Firebase config missing/placeholder - auth disabled on this page.");
  }
} catch (err) {
  // CDN blocked/offline: keep going so the avatar + session gate still work.
  console.warn("[AUTH] Firebase SDK failed to load (continuing without it):", err);
}

// Names used by the login-page code below. Undefined when Firebase
// didn't load — those functions simply won't be callable.
const {
  GoogleAuthProvider,
  signInWithPopup,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  onIdTokenChanged,
} = window.__authKit || {};

const statusEl = () => document.getElementById("authStatus");
const errorEl = () => document.getElementById("authError");

function showError(msg) {
  const el = errorEl();
  if (el) {
    el.textContent = msg;
    el.style.display = "block";
  }
  console.error("[AUTH]", msg);
}

function showStatus(msg) {
  const el = statusEl();
  if (el) el.textContent = msg;
}

async function sessionSignIn(idToken, meta = {}) {
  const res = await fetch("/api/auth/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // isSignup/method are forwarded so the server can fire its own
    // (reliable) PostHog event even when the client's PostHog is
    // blocked by an adblocker.
    body: JSON.stringify({
      idToken,
      isSignup: meta.isSignup === true,
      method: meta.method || "unknown",
    }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Session failed (${res.status})`);
  }
  // Identify the user in PostHog so sign-ups / uploads / prompts /
  // exports are counted per real user, not per anonymous visit.
  // The redirect waits for PostHog to confirm delivery (with a
  // fallback timeout) - otherwise navigation cancels the event.
  const go = () => { window.location.href = "/"; };
  try {
    const me = await (await fetch("/api/me")).json();
    if (me && me.uid && window.posthog) {
      window.posthog.identify(me.uid, { email: me.email, name: me.name });
      let redirected = false;
      const redirectOnce = () => { if (!redirected) { redirected = true; go(); } };
      setTimeout(redirectOnce, 1500); // safety net if PostHog is blocked
      window.posthog.capture(
        meta.isSignup ? "user_signed_up" : "user_signed_in",
        { method: meta.method || "unknown", email: me.email || "" },
        redirectOnce
      );
      return;
    }
  } catch (_) {}
  go();
}

// ------------------------------------------------------------
// Sign-in methods (login page only)
// ------------------------------------------------------------

async function signInWithGoogle() {
  showStatus("Opening Google sign-in…");
  const cred = await signInWithPopup(auth, new GoogleAuthProvider());
  const m = cred.user.metadata || {};
  // First-ever login: creation and last-sign-in timestamps match.
  const isSignup = !!m.creationTime && m.creationTime === m.lastSignInTime;
  await sessionSignIn(await cred.user.getIdToken(), { method: "google", isSignup });
}

async function signInWithPassword(email, password, isSignup) {
  const cred = isSignup
    ? await createUserWithEmailAndPassword(auth, email, password)
    : await signInWithEmailAndPassword(auth, email, password);
  await sessionSignIn(await cred.user.getIdToken(), { method: "password", isSignup });
}

function friendlyAuthError(err) {
  const code = err?.code || "";
  const map = {
    "auth/invalid-email": "That email address doesn't look right.",
    "auth/user-not-found": "No account with that email. Try creating one.",
    "auth/wrong-password": "Incorrect password.",
    "auth/invalid-credential": "Incorrect email or password.",
    "auth/email-already-in-use": "That email already has an account.",
    "auth/weak-password": "Password must be at least 6 characters.",
    "auth/popup-closed-by-user": "Google sign-in was cancelled.",
    "auth/unauthorized-domain":
      "This domain isn't authorized in Firebase. Add it under " +
      "Authentication -> Settings -> Authorized domains.",
  };
  return map[code] || err.message;
}

// ------------------------------------------------------------
// Login page wiring
// ------------------------------------------------------------

function initLoginPage() {
  const googleBtn = document.getElementById("googleBtn");
  const pwForm = document.getElementById("passwordForm");
  const pwTitle = document.getElementById("passwordTitle");
  const toggleSignup = document.getElementById("toggleSignup");
  let isSignup = false;

  googleBtn?.addEventListener("click", () =>
    signInWithGoogle().catch((e) => showError(e.message))
  );

  toggleSignup?.addEventListener("click", () => {
    isSignup = !isSignup;
    pwTitle.textContent = isSignup ? "Create account" : "Sign in";
    toggleSignup.textContent = isSignup
      ? "Already have an account? Sign in"
      : "New here? Create an account";
  });

  pwForm?.addEventListener("submit", (e) => {
    e.preventDefault();
    const email = pwForm.email.value.trim();
    const password = pwForm.password.value;
    signInWithPassword(email, password, isSignup).catch((err) =>
      showError(friendlyAuthError(err))
    );
  });

  // Already signed in with Firebase (e.g. revisiting the page)
  // -> refresh the server session and continue into the app.
  if (!auth) return;
  onAuthStateChanged(auth, (user) => {
    if (user) {
      user
        .getIdToken()
        .then(sessionSignIn)
        .catch(() => {}); // fall back to manual sign-in
    }
  });
}

// ------------------------------------------------------------
// App pages: session check; profile UI only where #profileSlot
// exists (index page). Canvas pages stay clean.
// ------------------------------------------------------------

// Silently refresh the server session cookie from an existing Firebase user.
// POSTs just the ID token back to /api/auth/session, which re-mints the
// 31-day Flask cookie (rolling window). No PostHog event, no redirect — pure
// maintenance so the user stays signed in until they explicitly Sign out.
async function refreshServerSession(user) {
  if (!auth || !user) return;
  try {
    const idToken = await user.getIdToken();
    if (!idToken) return;
    await fetch("/api/auth/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idToken }),
    });
  } catch (err) {
    // Best-effort: a flaky refresh must never bounce the user. The existing
    // /api/me gate still handles genuine expiry.
    console.warn("[AUTH] background session refresh failed:", err);
  }
}

function initAppPage() {
  // Plan A: keep the server session cookie alive on app pages. Whenever
  // Firebase has a user (restored, or the ID token rotated ~hourly), re-mint
  // the Flask cookie silently. This turns the fixed 31-day cookie into a
  // rolling window — the user stays logged in until they Sign out.
  if (auth) {
    onAuthStateChanged(auth, (user) => { if (user) refreshServerSession(user); });
    if (typeof onIdTokenChanged === "function") {
      onIdTokenChanged(auth, (user) => { if (user) refreshServerSession(user); });
    }
  }

  fetch("/api/me")
    .then((res) => {
      if (res.status === 401) {
        // Not signed in (or session expired): go to the login page.
        window.location.href = "/login";
        return null;
      }
      if (!res.ok) {
        console.error("[AUTH] /api/me unexpected status", res.status);
        return null;
      }
      return res.json();
    })
    .then((me) => {
      if (!me) return;
      const slot = document.getElementById("profileSlot");
      console.log("[AUTH] session OK; profileSlot present:", !!slot);
      if (!slot) {
        console.warn(
          "[AUTH] #profileSlot not found - the server is serving a stale " +
          "cached index.html. RESTART FLASK."
        );
        return;
      }
      // A UI bug must never bounce the user to /login - log and stay.
      try {
        buildProfileUI(slot, me);
      } catch (err) {
        console.error("[AUTH] profile UI failed:", err);
      }
    })
    .catch((err) => {
      // Network/parse errors: log loudly, do NOT redirect (redirecting
      // here caused an infinite / <-> /login loop).
      console.error("[AUTH] index init failed:", err);
    });
}

function buildProfileUI(slot, me) {
  if (document.getElementById("profileBtn")) return;

  const displayName =
    me.name || (me.email ? me.email.split("@")[0] : "Account");
  const initial = (displayName[0] || "A").toUpperCase();

  const btn = document.createElement("button");
  btn.id = "profileBtn";
  btn.className = "profileBtn";
  btn.setAttribute("aria-label", "Account menu");
  btn.textContent = initial;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.classList.toggle("open");
  });

  const menu = document.createElement("div");
  menu.id = "profileMenu";
  menu.className = "profileMenu";
  let items =
    '<div class="profileInfo">' +
    '<div class="profileName"></div>' +
    '<div class="profileEmail"></div>' +
    "</div>";
  if (me.is_admin) {
    items += '<a class="profileStats" href="/admin">Stats</a>';
  }
  items += '<button class="profileSignOut" type="button">Sign out</button>';
  menu.innerHTML = items;
  menu.querySelector(".profileName").textContent = displayName;
  menu.querySelector(".profileEmail").textContent = me.email || "";

  menu.querySelector(".profileSignOut").addEventListener("click", async () => {
    if (auth) {
      try {
        await auth.signOut();
      } catch (_) {}
    }
    window.location.href = "/logout";
  });

  document.addEventListener("click", (e) => {
    if (!menu.contains(e.target)) menu.classList.remove("open");
  });

  slot.appendChild(btn);
  slot.appendChild(menu);
}

// ------------------------------------------------------------
// Bootstrap: pick login page vs app page (timing-proof: the
// dynamically-appended module on app pages may load after
// DOMContentLoaded has already fired).
// ------------------------------------------------------------

function bootstrap() {
  console.log("[AUTH] auth.js loaded on", window.location.pathname);
  if (document.getElementById("loginCard")) {
    initLoginPage();
  } else {
    initAppPage();
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootstrap);
} else {
  bootstrap();
}
