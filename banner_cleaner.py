"""
banner_cleaner.py — find a baked-in solid white/light banner in an uploaded
video, erase the text inside it, and re-encode a clean base video the rest of
the pipeline can build on.

WHY THIS EXISTS
---------------
Autoquence renders overlays (banners/text) *on top of* the uploaded clip; the
source pixels are never edited. But many uploaded clips already have a white
banner + burned-in text baked into the footage. That text is invisible to the
normal scene-graph AI (it only sees elements) and cannot be hidden by deleting
an element. This module:

  1. Samples frames via FFmpeg and finds a near-solid light horizontal band
     (the banner) by row-wise whiteness.
  2. Confirms there is text inside the band (pixels notably darker than the
     band background).
  3. Re-encodes the video with FFmpeg drawbox, filling the band with its own
     background color so the built-in text disappears.

ASSUMPTIONS
-----------
- Banner is a FLAT, solid (or near-solid) light/white bar top or bottom.
- The bar spans the full width and stays at the same place for the whole clip.
  If it only exists in a segment, the whole clip is still cleaned (the band
  detection is global). This is a documented first-cut limitation.
- Vertical (column) banners are not handled.

DEPENDENCIES: ffmpeg/ffprobe on PATH, numpy, Pillow — all already used/app.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

import numpy as np


# A row pixel is "white" when its mean luminance is at least this.
# Compressed / hardware "white" banners are often light-grey, so 188 is
# intentionally tolerant rather than a hard 230+.
DEFAULT_WHITE_THRESH = 188
# A row pixel is "dark" when its mean luminance is at most this. Black
# subscribe-style banners are as common as white ones, so detection runs
# symmetrically on both light and dark bands.
DEFAULT_DARK_THRESH = 45
# Minimum width fraction a banner (or text-) row must be light to still count
# as part of the banner band. Kept low enough that rows containing burned-in
# text remain inside the band instead of splitting it into fragments.
MIN_ROW_WHITE_FRAC = 0.45
# Minimum solidity within the band (fraction of near-white pixels).
MIN_BAND_SOLIDITY = 0.40
# Smallest banner height as a fraction of frame height.
MIN_BAND_FRAC = 0.03
# Banners larger than this are almost certainly not a banner (whole screen).
MAX_BAND_FRAC = 0.55
# A band pixel counts as "text" when its mean channel diff vs the banner
# background exceeds this.
TEXT_DIFF_THRESH = 34
# Require at least this fraction of dark pixels to conclude "has text".
MIN_TEXT_FRAC = 0.002
# ── Band expansion (full-extent recovery) ──────────────────────────
# Rows dense with baked text have a LOW near-white fraction, so the
# white-run detection can stop short of the banner's true edges (leaving
# part of the text unpainted). Banner rows are mostly pixels CLOSE to the
# flat background color — whether they are background or text — while
# scenery rows are not. These constants drive the outward expansion.
# A pixel is "close to background" within this mean-channel distance.
BG_CLOSE_TOL = 55
# A row belongs to the banner when at least this fraction of its pixels is
# close to the background color.
MIN_ROW_CLOSE_FRAC = 0.55
# Expansion tolerates runs of up to this fraction of frame height of
# non-close rows (fully text-covered rows) before giving up.
BG_GAP_FRAC = 0.015
# The band may grow to at most this multiple of its originally-detected
# height (also hard-capped by MAX_BAND_FRAC).
BAND_EXPAND_CAP = 2.5
# How many frames to sample, spread evenly across the WHOLE video.
MAX_FRAMES = 12
# ============================================================
# Helpers
# ============================================================

def _probe_video_size(video_path):
    """Return (width, height) or (None, None) via ffprobe."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of",
                "csv=s=x:p=0", str(video_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        row = (proc.stdout or "").strip()
        if "x" in row:
            w, h = row.split("x", 1)
            return int(w), int(h)
    except Exception as exc:
        print(f"[BANNER-CLEANER] probe failed: {exc}")
    return None, None


def _probe_duration(video_path):
    """Return duration in seconds (float) or None via ffprobe."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "format=duration", "-of",
                "default=noprint_wrappers=1:nokey=1", str(video_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        val = (proc.stdout or "").strip()
        return float(val) if val else None
    except Exception as exc:
        print(f"[BANNER-CLEANER] duration probe failed: {exc}")
    return None


def _load_frames(video_path: Path, max_frames: int = MAX_FRAMES):
    """
    Extract up to ``max_frames`` small RGB frames spread evenly across the
    WHOLE clip (not just the first few seconds). ``fps`` is computed from the
    video duration so the extracted frames cover start → end.
    """
    frames = []
    duration = _probe_duration(video_path)
    # fps = max_frames / duration puts roughly one frame every duration/N sec,
    # i.e. frame timestamps span the entire clip. Guard the degenerate cases.
    if duration and duration > 0:
        fps = max(0.05, min(10.0, max_frames / duration))
    else:
        fps = 1.0
    try:
        with tempfile.TemporaryDirectory() as td:
            cmd = [
                "ffmpeg", "-y", "-i", str(video_path),
                "-vf", f"fps={fps:.6f},scale=480:-1",
                "-frames:v", str(max_frames),
                os.path.join(td, "f%03d.png"),
            ]
            subprocess.run(cmd, capture_output=True, timeout=30, check=False)
            for p in sorted(Path(td).glob("f*.png")):
                try:
                    frames.append(np.asarray(Image.open(p).convert("RGB")))
                except Exception:
                    continue
    except Exception as exc:
        print(f"[BANNER-CLEANER] frame extraction failed: {exc}")
    return frames


def _contiguous_runs(boolean_row_signal):
    """Return [(start, end), ...] for contiguous True runs (end exclusive)."""
    runs = []
    start = None
    for i, val in enumerate(boolean_row_signal):
        if val and start is None:
            start = i
        elif not val and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(boolean_row_signal)))
    return runs
# ============================================================
# Detection
# ============================================================

def _frame_best_band(frame):
    """
    Return the best LIGHT banner band and the best DARK banner band in a
    single frame as a tuple (light_cand, dark_cand); either may be None.
    Each candidate is (level0, level1, orient, bg_hex, dark).
    ``level0``/``level1`` are rounded 0-100 vertical percentages.
    """
    h, w, _ = frame.shape
    if h <= 0 or w <= 0:
        return None, None
    f = frame.astype(np.int16)
    lum = f.mean(axis=2)

    best_light = None  # (score, cand)
    best_dark = None
    # Symmetric search: solid LIGHT bands (classic white banners) and solid
    # DARK bands (black subscribe bars). Both share the solidity / size /
    # text-contrast logic; only the pixel mask differs.
    for is_dark_mode, mask in (
        (False, lum >= DEFAULT_WHITE_THRESH),
        (True, lum <= DEFAULT_DARK_THRESH),
    ):
        best = best_light if not is_dark_mode else best_dark
        row_frac = mask.mean(axis=1)

        for y0, y1 in _contiguous_runs(row_frac >= MIN_ROW_WHITE_FRAC):
            band_h = y1 - y0
            band_h_norm = band_h / h
            if band_h_norm < MIN_BAND_FRAC or band_h_norm > MAX_BAND_FRAC:
                continue

            band = f[y0:y1]
            band_mask = mask[y0:y1]
            if band_mask.mean() < MIN_BAND_SOLIDITY:
                continue

            band_px = band[band_mask]
            if len(band_px) == 0:
                continue
            bg = np.median(band_px, axis=0)

            # ── BAND EXPANSION (full-extent recovery) ──────────────
            # The white-run above can stop short of the banner's true
            # edges because text-dense rows have a low near-white
            # fraction. Expand outward using background-closeness: banner
            # rows (bg or text) are mostly pixels close to bg, scenery
            # rows are not. Small gaps (fully text-covered rows) are
            # tolerated; real scenery stops the expansion.
            close = np.abs(f - bg[None, None, :]).mean(axis=2) <= BG_CLOSE_TOL
            row_close = close.mean(axis=1)
            gap = max(1, int(round(h * BG_GAP_FRAC)))
            cap_h = min(
                int(h * MAX_BAND_FRAC), max(band_h + 1, int(band_h * BAND_EXPAND_CAP))
            )
            y0e, y1e = y0, y1
            misses = 0
            yy = y0e - 1
            while yy >= 0 and (y1e - yy) <= cap_h:
                if row_close[yy] >= MIN_ROW_CLOSE_FRAC:
                    y0e, misses = yy, 0
                else:
                    misses += 1
                    if misses > gap:
                        break
                yy -= 1
            misses = 0
            yy = y1e
            while yy < h and (yy - y0e) <= cap_h:
                if row_close[yy] >= MIN_ROW_CLOSE_FRAC:
                    y1e, misses = yy + 1, 0
                else:
                    misses += 1
                    if misses > gap:
                        break
                yy += 1
            y0, y1 = y0e, y1e
            band_h = y1 - y0
            band = f[y0:y1]

            diff = np.abs(band - bg[None, None, :]).mean(axis=2)
            dark = float((diff > TEXT_DIFF_THRESH).mean())

            # Interior banners (not flush to top/bottom) are common — we no longer
            # heavily penalize them; a mild tie-break only.
            is_top = y0 <= h * 0.10
            is_bot = y1 >= h * 0.90
            orient = "top" if is_top else ("bottom" if is_bot else "interior")

            bg_hex = "#%02x%02x%02x" % tuple(
                np.clip(np.round(bg), 0, 255).astype(int)
            )
            cand = (round(y0 / h, 2), round(y1 / h, 2), orient, bg_hex, dark)
            # Prefer wider (expanded = truer), less-penalised bands;
            # tie-break toward more text.
            score = (band_h / h) - anchor_bias(is_top, is_bot) - min(dark, 1.0)
            if best is None or score > best[0]:
                best = (score, cand)

        if is_dark_mode:
            best_dark = best
        else:
            best_light = best

    return (
        best_light[1] if best_light else None,
        best_dark[1] if best_dark else None,
    )


def anchor_bias(is_top, is_bot):
    """Small tie-break: anchor banners (top/bottom) beat interior ones."""
    return 0.0 if (is_top or is_bot) else 0.12


def _detect_pass(video_path, quiet=True, max_frames=None):
    """One detection pass using the current module-level thresholds."""
    """
    Find a solid light banner with text baked into a video.

    Samples 12 frames spread across the WHOLE clip, extracts the best band in
    each frame, then aggregates: the band that appears in the most frames wins
    (so a banner that only shows mid/late in the upload is still found).

    Returns:
        {"found": False, "reason": str}
    or
        {"found": True, "orientation", "y0", "y1", "background",
         "has_text", "text_fraction", "confidence", "reason"}

    ``y0``/``y1`` are normalized (0-1) vertical bounds.
    ``confidence`` = fraction of sampled frames with the chosen band.
    """
    default = {"found": False, "reason": "no_white_band"}
    frames = _load_frames(video_path, max_frames or MAX_FRAMES)
    if not frames:
        return {"found": False, "reason": "no_frames"}

    # (level0, level1, orient) -> {"bg": hex, "darks": [...]} votes,
    # kept SEPARATELY for light and dark bands so a black letterbox bar can
    # never out-vote a real white banner (or vice versa). Light bands win.
    votes_light = {}
    votes_dark = {}
    for frame in frames:
        fb_light, fb_dark = _frame_best_band(frame)
        for fb, votes in ((fb_light, votes_light), (fb_dark, votes_dark)):
            if fb is None:
                continue
            level0, level1, orient, bg_hex, dark = fb
            key = (level0, level1, orient)
            votes.setdefault(key, {"bg": bg_hex, "darks": []})["darks"].append(dark)

    total = len(frames)

    def select(votes, is_dark):
        """Pick the winning band from one vote pool and gate it."""
        if not votes:
            return None

        def band_height(k):
            return k[1] - k[0]

        winner_key = max(
            votes.keys(),
            key=lambda k: (len(votes[k]["darks"]), band_height(k)),
        )
        info = votes[winner_key]
        level0, level1, orient = winner_key
        darks = info["darks"]
        dark = sum(darks) / len(darks)
        confidence = len(darks) / total
        has_text = dark >= MIN_TEXT_FRAC

        # Interior "bands" are overwhelmingly scenery (sky, walls, over-exposed
        # ground) rather than banners: require BOTH high frame agreement AND
        # burned-in text before treating a mid-frame band as a banner. Edge
        # (top/bottom) bands keep the old lenient behavior.
        if orient == "interior" and (confidence < 0.75 or not has_text):
            return None

        # DARK bands are extra false-positive prone: black letterbox bars on
        # portrait footage and dark scenery both match. A dark band only
        # counts as a banner when it clearly contains burned-in (light) text
        # AND the band is present in at least half the sampled frames.
        if is_dark and (not has_text or confidence < 0.5):
            return None

        reason = "banner_with_text" if has_text else "banner_no_text"
        if confidence < 0.5:
            reason += "_partial"
        if orient == "interior":
            reason += "_interior"

        return {
            "found": True,
            "tone": "dark" if is_dark else "light",
            "orientation": orient,
            "y0": level0,
            "y1": level1,
            "background": info["bg"],
            "has_text": has_text,
            "text_fraction": round(dark, 4),
            "confidence": round(confidence, 3),
            "reason": reason,
        }

    # Light banners take priority; fall back to a gated dark-banner result.
    result = select(votes_light, is_dark=False) or select(votes_dark, is_dark=True)
    if result is not None:
        # ── Global full-extent refinement ──────────────────────────
        # Recover the banner's TRUE edges (per-frame expansion can stop
        # early on noisy frames) using the background-closeness profile
        # averaged across all sampled frames, then re-measure the text
        # fraction over the refined band.
        try:
            ry0, ry1 = _refine_band(
                frames, result["y0"], result["y1"], result["background"]
            )
            if ry1 > ry0 and (ry1 - ry0) >= (result["y1"] - result["y0"]) - 0.02:
                bg_rgb = np.array(
                    [int(result["background"][i:i + 2], 16) for i in (1, 3, 5)],
                    dtype=np.float32,
                )
                darks = []
                for fr in frames:
                    f2 = fr.astype(np.int16)
                    hh = f2.shape[0]
                    b = f2[int(ry0 * hh):int(ry1 * hh)]
                    if b.size:
                        dd = np.abs(b - bg_rgb[None, None, :]).mean(axis=2)
                        darks.append(float((dd > TEXT_DIFF_THRESH).mean()))
                result["y0"] = round(ry0, 4)
                result["y1"] = round(ry1, 4)
                if darks:
                    dark = sum(darks) / len(darks)
                    result["text_fraction"] = round(dark, 4)
                    result["has_text"] = dark >= MIN_TEXT_FRAC
        except Exception as exc:
            print(f"[BANNER-CLEANER] band refinement failed: {exc}")
    if result is None:
        result = default
    if not quiet:
        print(
            f"[BANNER-CLEANER] detect -> {result}"
        )
    return result


# Relaxed thresholds for the fallback pass: real-world banners are often
# compressed, gradient-tinted, or low-contrast and miss the strict gates.
_RELAXED_OVERRIDES = {
    "DEFAULT_WHITE_THRESH": 165,
    "DEFAULT_DARK_THRESH": 65,
    "MIN_ROW_WHITE_FRAC": 0.28,
    "MIN_BAND_SOLIDITY": 0.22,
    "TEXT_DIFF_THRESH": 22,
}


def _refine_band(frames, y0f, y1f, bg_hex):
    """
    Global full-extent refinement of a winning band.

    Per-frame expansion can stop early on frames where the banner edge is
    noisy; averaging the background-closeness profile across ALL sampled
    frames gives a much more stable banner-extent signal. Walks the averaged
    profile outward from (y0f, y1f) with gap tolerance.
    """
    bg = np.array(
        [int(bg_hex[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.float32
    )
    profiles = []
    for fr in frames:
        f = fr.astype(np.int16)
        close = np.abs(f - bg[None, None, :]).mean(axis=2) <= BG_CLOSE_TOL
        profiles.append(close.mean(axis=1))
    if not profiles:
        return y0f, y1f
    h = min(p.shape[0] for p in profiles)
    avg = np.mean(np.stack([p[:h] for p in profiles]), axis=0)

    y0 = max(0, int(round(y0f * h)))
    y1 = min(h, max(y0 + 1, int(round(y1f * h))))
    gap = max(2, int(round(h * BG_GAP_FRAC * 1.5)))
    cap_h = min(
        int(h * MAX_BAND_FRAC),
        max((y1 - y0) + 1, int((y1 - y0) * BAND_EXPAND_CAP)),
    )
    # Averaged profiles are smoother/noise-tolerant → slightly lower gate.
    thr = MIN_ROW_CLOSE_FRAC - 0.10

    misses = 0
    yy = y0 - 1
    while yy >= 0 and (y1 - yy) <= cap_h:
        if avg[yy] >= thr:
            y0, misses = yy, 0
        else:
            misses += 1
            if misses > gap:
                break
        yy -= 1
    misses = 0
    yy = y1
    while yy < h and (yy - y0) <= cap_h:
        if avg[yy] >= thr:
            y1, misses = yy + 1, 0
        else:
            misses += 1
            if misses > gap:
                break
        yy += 1
    return y0 / h, y1 / h


def detect_white_banner(video_path, quiet=False, quick=False):
    """
    Public entry point. Runs the strict detection pass; when it finds no
    band, retries once with relaxed thresholds so compressed / tinted /
    low-contrast banners still qualify. A relaxed hit is marked with a
    ``relaxed_`` reason prefix and slightly reduced confidence.

    ``quick=True`` samples only 4 frames instead of 12 — used by the fast
    hide-text endpoint when the detection cache is cold.
    """
    n_frames = 4 if quick else None
    strict = _detect_pass(video_path, quiet=True, max_frames=n_frames)
    if strict.get("found"):
        if not quiet:
            print(f"[BANNER-CLEANER] detect -> {strict}")
        return strict

    saved = {k: globals()[k] for k in _RELAXED_OVERRIDES}
    globals().update(_RELAXED_OVERRIDES)
    try:
        relaxed = _detect_pass(video_path, quiet=True, max_frames=n_frames)
    finally:
        globals().update(saved)

    if relaxed.get("found"):
        relaxed["reason"] = f"relaxed_{relaxed.get('reason', 'banner')}"
        relaxed["confidence"] = round(float(relaxed.get("confidence", 0)) * 0.8, 3)
        print(f"[BANNER-CLEANER] strict miss; RELAXED pass -> {relaxed}")
        return relaxed

    result = strict
    if not quiet:
        print(f"[BANNER-CLEANER] detect -> {result}")
    return result
# ============================================================
# Cleanup baking
# ============================================================

def bake_cleaned_video(video_path, detection: dict, output_path) -> bool:
    """
    Re-encode ``video_path`` into ``output_path`` with the detected banner
    band filled with its own background color (erasing the baked-in text).

    Returns True on success.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    width, height = _probe_video_size(video_path)
    if not width or not height:
        print("[BANNER-CLEANER] could not determine video dimensions")
        return False

    y0f = float(detection.get("y0", 0.0))
    y1f = float(detection.get("y1", 1.0))
    # Pad the band by a few pixels so anti-aliased / grey edge rows of the
    # baked banner don't ghost through around the replacement banner.
    pad = max(2, int(round(0.004 * height)))
    y0 = max(0, int(round(y0f * height)) - pad)
    y1 = min(height, int(round(y1f * height)) + pad)
    if y1 <= y0:
        return False

    color = detection.get("background") or "#ffffff"
    band_h = y1 - y0

    # drawbox fills the whole-width band with the banner's own background.
    vf = (
        f"drawbox=x=0:y={y0}:w=iw:h={band_h}"
        f":color={color}@1:t=fill"
    )

    command = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", vf,
        "-c:v", "libx264",
        # ultrafast: banner hiding must feel instant; the band is a flat
        # fill so heavier presets buy almost nothing visually here.
        "-preset", "ultrafast",
        "-crf", "23",
        "-threads", "0",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        result = subprocess.run(
            command, capture_output=True, timeout=600, check=False
        )
        if result.returncode != 0:
            print(
                "[BANNER-CLEANER] ffmpeg clean failed: "
                f"{result.stderr.decode(errors='replace')[-500:]}"
            )
            return False
    except Exception as exc:
        print(f"[BANNER-CLEANER] ffmpeg clean exception: {exc}")
        return False

    ok = output_path.exists() and output_path.stat().st_size > 0
    if ok:
        print(
            f"[BANNER-CLEANER] wrote cleaned video {output_path} "
            f"(band y={y0}..{y1}, bg={color})"
        )
    return ok


def clean_if_needed(video_path, processed_folder=None):
    """
    Convenience: detect + bake in one call. Returns
        {"cleaned": False, "detection": {...}}
    or
        {"cleaned": True, "filename", "cleaned_url", "detection": {...}}
    """
    video_path = Path(video_path)
    detection = detect_white_banner(video_path)
    if not detection.get("found") or not detection.get("has_text"):
        return {"cleaned": False, "detection": detection}

    folder = Path(processed_folder) if processed_folder else \
        Path("static/processed")
    # Fingerprinted filename: derived from the detected region so a changed
    # detection (or fixed code) always produces a NEW file instead of
    # silently reusing a stale bake from an earlier run.
    y0f = float(detection.get("y0") or 0.0)
    y1f = float(detection.get("y1") or 1.0)
    cleaned_name = (
        f"{video_path.stem}_clean_"
        f"{int(round(y0f * 1000))}-{int(round(y1f * 1000))}"
        f"{video_path.suffix or '.mp4'}"
    )
    out = folder / cleaned_name

    if not out.exists():
        if not bake_cleaned_video(video_path, detection, out):
            return {"cleaned": False, "detection": detection}

    return {
        "cleaned": True,
        "filename": cleaned_name,
        "cleaned_url": f"/static/processed/{cleaned_name}",
        "detection": detection,
    }