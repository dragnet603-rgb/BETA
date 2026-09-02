# ============================================================
# AUTOQUENCE STATS - dead-simple event log (SQLite)
#
# Records who did what and when:
#   signed_up, signed_in, uploaded, prompt_sent,
#   export_started, export_completed
#
# Viewed on the /admin page (admin-only).
# ============================================================

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

# Load .env BEFORE reading ADMIN_EMAIL. This module is imported from
# app.py BEFORE app.py's own load_dotenv() call, so without this the
# admin email would be read from an empty environment (same trap that
# firebase_auth.py works around).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

_DB_PATH = Path(__file__).resolve().parent / "stats.db"
_lock = threading.Lock()

# Read lazily so a late load_dotenv() (or a real env var set after
# import) is still honored.
def _admin_email():
    return os.getenv("ADMIN_EMAIL", "").strip().lower()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL DEFAULT '',
    uid        TEXT NOT NULL DEFAULT '',
    event      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
CREATE INDEX IF NOT EXISTS idx_events_uid   ON events(uid);
"""


def _conn():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init():
    with _lock:
        conn = _conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()


_init()


def log_event(email, uid, event, detail=""):
    """Record one event. Never raises - stats must not break the app."""
    try:
        with _lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO events (email, uid, event, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    (email or "").lower(),
                    uid or "",
                    event,
                    str(detail or "")[:200],
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            conn.close()
    except Exception as exc:  # pragma: no cover
        print(f"[STATS] logging failed: {exc}")


def is_new_user(uid):
    try:
        with _lock:
            conn = _conn()
            row = conn.execute(
                "SELECT 1 FROM events WHERE uid = ? LIMIT 1", (uid,)
            ).fetchone()
            conn.close()
            return row is None
    except Exception:
        return False


def is_admin(email):
    return bool(_admin_email()) and (email or "").lower() == _admin_email()


def counts():
    """Totals for the four headline metrics."""
    out = {"signups": 0, "uploads": 0, "prompts": 0, "exports": 0}
    mapping = {
        "signups": ("signed_up",),
        "uploads": ("uploaded",),
        "prompts": ("prompt_sent",),
        "exports": ("export_started", "export_completed"),
    }
    try:
        with _lock:
            conn = _conn()
            for key, events in mapping.items():
                marks = ",".join("?" * len(events))
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM events WHERE event IN ({marks})",
                    events,
                ).fetchone()
                out[key] = row["n"] if row else 0
            conn.close()
    except Exception:
        pass
    return out


def recent(limit=60):
    try:
        with _lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT email, event, detail, created_at FROM events"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
    except Exception:
        return []


def per_user():
    """One row per person: counts, first seen, last seen."""
    try:
        with _lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT email,"
                " COUNT(*) AS actions,"
                " SUM(event = 'signed_up') AS signups,"
                " SUM(event = 'uploaded') AS uploads,"
                " SUM(event = 'prompt_sent') AS prompts,"
                " SUM(event = 'export_completed') AS exports,"
                " MIN(created_at) AS first_seen,"
                " MAX(created_at) AS last_seen"
                " FROM events GROUP BY uid ORDER BY MAX(id) DESC"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
    except Exception:
        return []
