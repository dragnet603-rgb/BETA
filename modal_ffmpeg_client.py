"""
modal_ffmpeg_client.py — web-tier proxy that farms exports out to Modal.

The browser POSTs the overlay plate + geometry to Flask; this module takes the
already-uploaded source video (it lives in static/uploads), streams source +
plate + geom to the deployed Modal web endpoint, and returns the finished MP4
bytes. If Modal is not configured or the call fails, it falls back to running
the same render locally via modal_export.run_render (NVENC if present, else
software x264), so a missing key never breaks export.

Env:
  MODAL_WEB_URL — https URL of the Modal render_modal endpoint.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

MODAL_WEB_URL = os.environ.get("MODAL_WEB_URL", "").strip().rstrip("/")


def modal_configured() -> bool:
    return bool(MODAL_WEB_URL)


def _post_modal_multipart(
    src_path: str,
    plate_path: str,
    geom: Dict[str, Any],
) -> bytes:
    """POST source + plate + geom to Modal; returns the MP4 bytes."""
    try:
        import requests
    except ImportError:
        raise RuntimeError("requests is required for the Modal export path.")

    with open(src_path, "rb") as sf, open(plate_path, "rb") as pf:
        files = {
            "source": (Path(src_path).name, sf, "video/mp4"),
            "plate": ("plate.png", pf, "image/png"),
        }
        data = {"geom": json.dumps(geom)}
        resp = requests.post(MODAL_WEB_URL, files=files, data=data, timeout=900)
    if resp.status_code != 200:
        raise RuntimeError(f"Modal render failed ({resp.status_code}): {resp.text[:500]}")
    return resp.content


def render_export(
    src_path: str,
    plate_path: str,
    out_path: str,
    geom: Dict[str, Any],
) -> str:
    """
    Render src + plate to out_path, preferring Modal. Returns the backend that
    actually ran ("modal" or "local"). Raising the remote errors happens only
    after the local fallback also fails.
    """
    # Try Modal first.
    if modal_configured():
        try:
            data = _post_modal_multipart(src_path, plate_path, geom)
            Path(out_path).write_bytes(data)
            return "modal"
        except Exception as exc:  # remote path failed — fall through to local
            print(f"[AUTOQUENCE] Modal render failed, using local: {exc}")

    # Local fallback (modal_export.run_render: NVENC if present, else x264).
    from modal_export import run_render
    run_render(src_path, plate_path, out_path, geom)
    return "local"


def export_with_plate(
    src_path: str,
    plate_bytes: bytes,
    geom: Dict[str, Any],
    out_path: str,
) -> str:
    """Convenience: write the plate blob, then render. Returns backend name."""
    with tempfile.NamedTemporaryFile(
        "wb", suffix=".png", delete=False
    ) as tf:
        tf.write(plate_bytes)
        plate_path = tf.name
    try:
        return render_export(src_path, plate_path, out_path, geom)
    finally:
        try:
            os.remove(plate_path)
        except OSError:
            pass