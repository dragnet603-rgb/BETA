# ============================================================
# AUTOQUENCE PROMPT MAILER
#
# Emails every prompt the user sends to the admin address
# (ADMIN_EMAIL, default temiolajide108@gmail.com) via Gmail SMTP.
#
# Setup (one-time, on the host):
#   1. myaccount.google.com -> Security -> turn on 2-Step Verification
#   2. Security -> App passwords -> create one for Mail
#   3. Set env var GMAIL_APP_PASSWORD to the 16-char password
#      (locally: put it in .env; on your host: env vars panel)
#
# Without GMAIL_APP_PASSWORD the mailer silently does nothing, so
# local dev keeps working. Emails are sent on a daemon thread and
# NEVER raise - mail problems must not break the app or stats.
# ============================================================

import os
import smtplib
import threading
from datetime import datetime, timezone
from email.message import EmailMessage

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _admin_email():
    return os.getenv("ADMIN_EMAIL", "temiolajide108@gmail.com").strip()


def send_prompt_email(prompt, who=""):
    """Email one prompt to the admin. Fire-and-forget; never raises."""
    password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    addr = _admin_email()
    text = str(prompt or "").strip()
    if not password or not addr or not text:
        return

    def _send():
        try:
            msg = EmailMessage()
            msg["From"] = addr
            msg["To"] = addr
            msg["Subject"] = f"Autoquence prompt: {text[:60]}"
            msg.set_content(
                f"Prompt: {text}\n\n"
                f"From: {who or 'guest'}\n"
                f"Time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            )
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
                smtp.login(addr, password)
                smtp.send_message(msg)
        except Exception as exc:  # noqa: BLE001 - mail must never break the app
            print(f"[MAIL] prompt email failed: {exc}")

    threading.Thread(target=_send, daemon=True).start()
