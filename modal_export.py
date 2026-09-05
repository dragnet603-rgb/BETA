"""
modal_export.py â€” DIY cloud-GPU export using Modal (serverless NVIDIA).

WHY
---
The in-browser WebCodecs export and the local software FFmpeg path both
encode H.264 on CPU, which is the real bottleneck. This module implements the
"overlay-plate" design: the *browser* renders every non-video element once
(shapes / banners / free text, via static/export-engine.js buildOverlayPlate)
into a transparent 1080x1920 PNG, and we hand that plate + the source video
to a GPU that does the crop->fit->overlay->encode with NVIDIA NVENC. That
matches the canvas pixel-for-pixel while moving the heavy decode/encode off
the user's CPU.

The same command builder runs two ways:
  1. Locally (run_render): NVENC when the host exposes h264_nvenc, else a
     libx264 fallback. Used when Modal is not configured.
  2. On Modal (render_modal): wrapped as a Modal web_endpoint so a Modal GPU
     function executes it. Only wired up when the `modal` package exists.

Server-side env var:
  MODAL_WEB_URL â€” https URL of the deployed Modal render_modal endpoint.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# Output frame: every export is a fixed 9:16 canvas (matches app.py / the
# client engine). Must stay in sync with static/export-engine.js OUTPUT_* .
OUT_W = 1080
OUT_H = 1920

# NVENC presets (p4 ~= "medium"; good quality/speed balance).
_NVENC = {"encoder": "h264_nvenc", "preset": "p4", "cq": 20}
# Software fallback mirrors app.py's settings.
_X264 = {"encoder": "libx264", "preset": "veryfast", "crf": 20}


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_hex(color: str) -> str:
    """FFmpeg's pad/parse_color needs full #RRGGBB[AA] — expand 3-digit hex."""
    color = (color or "#000000").strip()
    if color.startswith("#") and len(color) in (4, 5):
        color = "#" + "".join(ch * 2 for ch in color[1:])
    return color


_NVENC_PROBE_CACHE: Optional[bool] = None


def has_nvenc() -> bool:
    """True when ffmpeg can actually *use* h264_nvenc (driver + encoder).

    Listing the encoder in `ffmpeg -encoders` is not enough â€” builds ship it
    even on machines with no NVIDIA driver. Probe with a real 1-frame encode
    and cache the (negative) result to avoid re-probing per export.
    """
    global _NVENC_PROBE_CACHE
    if _NVENC_PROBE_CACHE is not None:
        return _NVENC_PROBE_CACHE
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
             "-c:v", _NVENC["encoder"], "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        _NVENC_PROBE_CACHE = proc.returncode == 0
    except Exception:
        _NVENC_PROBE_CACHE = False
    return _NVENC_PROBE_CACHE


def build_render_command(
    src_path: str,
    plate_path: str,
    out_path: str,
    geom: Dict[str, Any],
    opts: Optional[Dict[str, Any]] = None,
    prefer_nvenc: bool = True,
) -> List[str]:
    """
    Build an ffmpeg command that reproduces app.py's export geometry using a
    full-frame overlay plate instead of the lossy drawbox/drawtext chain.

    geom (from the browser) carries the fields computeOutput() exposes:
      width, height, srcRect {x,y,w,h}, trim {start,end}, speed,
      bg (letterbox fill color), bitrate (target bps).

    Filter graph mirrors the client's FIT semantics:
      crop to srcRect -> (speed: setpts) -> scale to fit 9:16 keeping AR ->
      pad to 1080x1920 centered with bg -> setsar=1 -> overlay plate -> yuv420p
    The plate is drawn by the browser at OUTPUT resolution using the same
    offX/offY/placedW/placedH it computed, so it lines up with the padded
    letterboxed picture (identical centering to app.py's pad).
    """
    opts = opts or {}
    speed = max(0.01, _float(geom.get("speed", 1.0), 1.0) or 1.0)
    bg = _normalize_hex(str(geom.get("bg") or "#000000"))

    w = max(2, int(_float(geom.get("width"), OUT_W) or OUT_W))
    h = max(2, int(_float(geom.get("height"), OUT_H) or OUT_H))

    sr = geom.get("srcRect") or {}
    cx = int(_float(sr.get("x"), 0))
    cy = int(_float(sr.get("y"), 0))
    cw = max(2, int(_float(sr.get("w"), 0)))
    ch = max(2, int(_float(sr.get("h"), 0)))

    bitrate = int(_float(geom.get("bitrate"), 12000000) or 12000000)
    has_gpu = prefer_nvenc and has_nvenc()

    # Encode flags: NVENC on a GPU host, else software x264.
    if has_gpu:
        vc = (f'-c:v {_NVENC["encoder"]} -preset {_NVENC["preset"]} '
              f'-cq {_NVENC["cq"]} -b:v {bitrate}')
    else:
        threads = max(1, int(os.environ.get("FFMPEG_THREADS", os.cpu_count() or 1)))
        vc = (f'-c:v {_X264["encoder"]} -preset {_X264["preset"]} '
              f'-crf {_X264["crf"]} -threads {threads}')

    vf = []  # video filter pieces
    if cw and ch:
        # Crop to the source region (defaults default to the full frame).
        vf.append(f"crop={cw}:{ch}:{cx}:{cy}")
    if speed != 1.0:
        vf.append(f"setpts={1.0 / speed:.8f}*PTS")
    vf.append(f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease")
    vf.append(f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:color={bg}")
    vf.append("setsar=1")

    vf_graph = ",".join(vf) + "[bgv];[bgv][1:v]overlay=0:0,format=yuv420p[vout]"

    # Audio: copy when speed is 1, else atempo + AAC.
    audio_codec = "copy"
    af = []
    if speed != 1.0:
        atempo = []
        remaining = speed
        if remaining > 2.0:
            while remaining > 2.0:
                atempo.append("atempo=2.0"); remaining /= 2.0
            atempo.append(f"atempo={remaining:.6f}")
        elif remaining < 0.5:
            while remaining < 0.5:
                atempo.append("atempo=0.5"); remaining *= 2.0
            atempo.append(f"atempo={remaining:.6f}")
        else:
            atempo.append(f"atempo={remaining:.6f}")
        af = atempo
        audio_codec = "aac"

    cmd = ["ffmpeg", "-y"]
    ts = _float(geom.get("trim", {}).get("start"))
    te = _float(geom.get("trim", {}).get("end"))
    if ts > 0:
        cmd += ["-ss", f"{ts}"]
    cmd += ["-i", str(src_path), "-i", str(plate_path)]
    if te > ts:
        cmd += ["-t", f"{te - ts}"]

    cmd += ["-filter_complex", vf_graph, "-map", "[vout]", "-map", "0:a?"]
    cmd += vc.split()
    cmd += ["-c:a", audio_codec]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-movflags", "+faststart", str(out_path)]
    return cmd


def run_render(
    src_path: str,
    plate_path: str,
    out_path: str,
    geom: Dict[str, Any],
    opts: Optional[Dict[str, Any]] = None,
) -> None:
    """Execute the render command locally. Uses NVENC when the host can
    actually run it; if the NVENC attempt fails at runtime (driver hiccup,
    out-of-video-memory, etc.) it retries once with software x264 so an
    export never hard-fails because of the encoder choice."""
    opts = opts or {}
    cmd = build_render_command(src_path, plate_path, out_path, geom, opts)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and "-c:v h264_nvenc" in " ".join(cmd):
        print("[AUTOQUENCE] NVENC render failed, retrying with libx264:",
              (proc.stderr or "")[-500:])
        cmd = build_render_command(
            src_path, plate_path, out_path, geom,
            {**opts, "prefer_nvenc": False},
        )
        proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + (proc.stderr or "")[-4000:])
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Modal wrapper. Modal requires decorated functions to live at global scope,
# so the app/function are defined here directly (not inside a factory).
# Deploy with:  modal deploy modal_export.py   â†’ prints the web endpoint URL.
# The endpoint uses Modal's fastapi_endpoint with typed multipart fields, so
# FastAPI/python-multipart handle the upload parsing (no manual ASGI fiddling).
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    import modal

    _image = (
        modal.Image.debian_slim(python_version="3.11")
        .apt_install("ffmpeg")
        .pip_install("python-multipart")
    )
    app = modal.App("autoquence-export")

    @app.function(image=_image, gpu="T4", timeout=600)
    @modal.fastapi_endpoint(method="POST", label="render")
    def render_modal(
        source: "modal.fastapi_endpoint.UploadFile",
        plate: "modal.fastapi_endpoint.UploadFile",
        geom: str,
    ):
        """Multipart fields: `source` (video), `plate` (PNG), `geom` (JSON str).
        Returns the rendered MP4 bytes."""
        from fastapi import Response

        tmpdir = tempfile.mkdtemp(prefix="aq_render_")
        try:
            ext = Path(source.filename or "src.mp4").suffix or ".mp4"
            src_p = os.path.join(tmpdir, "src" + ext)
            plate_p = os.path.join(tmpdir, "plate.png")
            out_p = os.path.join(tmpdir, "out.mp4")
            with open(src_p, "wb") as fh:
                shutil.copyfileobj(source.file, fh)
            with open(plate_p, "wb") as fh:
                shutil.copyfileobj(plate.file, fh)
            run_render(src_p, plate_p, out_p, json.loads(geom),
                       {"prefer_nvenc": True})
            with open(out_p, "rb") as fh:
                data = fh.read()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return Response(
            content=data,
            media_type="video/mp4",
            headers={"Content-Disposition": "attachment"},
        )

except Exception:  # modal not installed locally â€” module still importable
    app = None
    render_modal = None
