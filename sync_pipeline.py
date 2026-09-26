"""
sync_pipeline.py — Autoquence "images + voiceover -> synced video" pipeline.

Flow:
  1. User uploads N images + 1 voiceover audio  -> static/uploads/<job_id>/
  2. Whisper transcribes the audio into speech segments and words
     (start/end timestamps at word granularity).
  3. create_visual_segments() cuts the voiceover into EXACTLY N
     contiguous visual segments, preferring real SENTENCE starts near
     the proportional spots (falling back to word boundaries - never
     mid-word), 0 -> full audio duration. Image i is shown during
     segment i, flipping as sentence i begins when the counts match —
     the number of Whisper speech segments does not constrain this.

  4. (Render to MP4 is a later step — this module stores the resolved
     timeline so any renderer can consume it.)

Everything a job needs lives in manifest.json inside its job folder:
  { images: [...], audio: "audio.mp3", audio_ext: "mp3",
    status: "ready|processing|error", error: null,
    segments: [ {start, end, text, image} ], matched: bool }
"""

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from segmentation import SegmentValidationError, create_visual_segments

UPLOAD_FOLDER = Path("static/uploads")


# ---------------------------------------------------------------
# Transcription engines, tried in order (STT_ENGINE pins a single one)
#
#   1. groq    whisper-large-v3(-turbo) on Groq's FREE tier
#              (GROQ_API_KEY; ~8h of audio/day free, resets daily).
#              OpenAI-compatible API: no model download, no CPU burn -
#              the fast path on small/cheap hosts.
#   2. local   faster-whisper (CTranslate2 port of Whisper) runs
#              fully offline on this machine — no API key, no per-minute cost.
#   WHISPER_MODEL        tiny|tiny.en|base|small|medium|large-v3 (default: tiny)
#   WHISPER_DEVICE       auto|cpu|cuda                    (default: auto)
#   WHISPER_COMPUTE_TYPE auto|int8|float16|float32        (default: auto -> int8 on CPU, float16 on CUDA)
#   WHISPER_CPU_THREADS  int, 0 = auto (default: 0 -> os.cpu_count())
#   WHISPER_NUM_WORKERS  int                              (default: 1)
#              Weights download once on first use, then come from cache.
#              STT_LOCAL=0 skips this engine entirely (saves RAM and the
#              download on 512MB hosts).
#   3. openai  whisper-1 (needs OPENAI_API_KEY or WHISPER_API_KEY).
#              The OpenRouter key used for chat does NOT cover audio
#              transcription.
#
# The browser may transcribe FIRST (WebGPU, static/client-transcribe.js)
# and skip this chain entirely. Any engine failure (missing key, 429 /
# quota, network) falls through to the next one, so a free-tier hiccup
# never fails a job.
# ---------------------------------------------------------------

_transcribe_client = None
_transcribe_client_lock = threading.Lock()

_whisper_model = None
_whisper_model_lock = threading.Lock()


def _get_transcribe_client():
    global _transcribe_client
    with _transcribe_client_lock:
        if _transcribe_client is None:
            api_key = os.getenv("OPENAI_API_KEY") or os.getenv("WHISPER_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "Speech recognition is not configured: set OPENAI_API_KEY."
                )
            from openai import OpenAI

            _transcribe_client = OpenAI(api_key=api_key)
        return _transcribe_client


# Groq speaks the OpenAI API, so the same client class works with a
# different base_url. Free tier: whisper-large-v3(-turbo), ~8h of audio
# per day, resets daily.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "whisper-large-v3-turbo"

_groq_client = None
_groq_client_lock = threading.Lock()


def _get_groq_client():
    """Lazily-built Groq client, or None when GROQ_API_KEY is unset."""
    global _groq_client
    if not os.getenv("GROQ_API_KEY", "").strip():
        return None
    with _groq_client_lock:
        if _groq_client is None:
            from openai import OpenAI

            _groq_client = OpenAI(
                api_key=os.getenv("GROQ_API_KEY", "").strip(),
                base_url=GROQ_BASE_URL,
            )
        return _groq_client


def _get_whisper_model():
    """Lazy-load the local faster-whisper model (once, thread-safe)."""
    global _whisper_model
    with _whisper_model_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel

            # tiny is ~3-5x faster than base on CPU; sync only needs word
            # TIMINGS for beat cuts, so the rougher tiny transcript is fine.
            # Override per deployment with WHISPER_MODEL=base|small|... .
            model_name = os.getenv("WHISPER_MODEL", "tiny")
            device = os.getenv("WHISPER_DEVICE", "auto")
            compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "auto")

            def _int_env(name, default):
                try:
                    return max(0, int(os.getenv(name, str(default))))
                except (TypeError, ValueError):
                    return default

            # 0 = auto -> all available cores; faster-whisper's built-in
            # default (4) underuses larger boxes.
            cpu_threads = _int_env("WHISPER_CPU_THREADS", 0) or (os.cpu_count() or 4)
            num_workers = _int_env("WHISPER_NUM_WORKERS", 1) or 1

            def _build(device, compute_type):
                # "auto" compute lets CTranslate2 pick float32 on CPU, which
                # is slower and heavier than int8 with no timing benefit.
                if compute_type in ("", "auto"):
                    compute_type = "float16" if device == "cuda" else "int8"
                return WhisperModel(
                    model_name,
                    device=device,
                    compute_type=compute_type,
                    cpu_threads=cpu_threads,
                    num_workers=num_workers,
                )

            try:
                _whisper_model = _build(device, compute_type)
            except Exception:
                if device == "cpu" and compute_type in ("", "auto", "int8"):
                    raise
                # e.g. CUDA requested but unavailable -> fall back to CPU.
                _whisper_model = _build("cpu", "int8")
        return _whisper_model


def warm_whisper():
    """Pre-load the local model in the background - but ONLY when it is an
    engine this deployment will actually use.

    Called once at app startup so the first local transcription does not
    pay the model-load (and possible one-time weight download) wait. It is
    SKIPPED when faster-whisper is disabled (STT_LOCAL=0) or is not part
    of the chain: on a 512MB host the ~75MB weights download plus a
    resident model is the single most expensive thing on the box, and a
    Groq/OpenAI deployment never transcribes locally - it was measured
    starving the very API call that was doing the work. Never raises.
    """
    if not _should_warm_local():
        print("[sync] whisper warm-up skipped: local engine not in use")
        return

    def _warm():
        try:
            _get_whisper_model()
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            print("[sync] whisper warm-up skipped:", exc)

    threading.Thread(target=_warm, daemon=True, name="whisper-warmup").start()


def _transcribe_local(audio_path: Path):
    """faster-whisper -> ({start, end, text} segments, {start, end} words).

    Word-level timestamps are required by the segmentation engine: it
    cuts images on speech boundaries instead of splitting audio blindly.
    """
    model = _get_whisper_model()
    # vad_filter trims silence so timestamps stay tight around speech,
    # which keeps the image->segment sync accurate. beam_size=1 (greedy)
    # is ~1.5-2x faster than the default beam search with no meaningful
    # accuracy change for sync purposes — we only need word TIMINGS, not
    # a perfect transcript.
    raw_segments, _info = model.transcribe(
        str(audio_path),
        vad_filter=True,
        word_timestamps=True,
        beam_size=1,
    )
    segments = []
    words = []
    for seg in raw_segments:
        text = (seg.text or "").strip()
        for w in getattr(seg, "words", None) or []:
            w_start = getattr(w, "start", None)
            w_end = getattr(w, "end", None)
            if w_start is None or w_end is None:
                continue
            words.append({"start": float(w_start), "end": float(w_end)})
        if not text:
            continue
        segments.append({
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "text": text,
        })
    return segments, words


def _parse_api_transcription(result):
    """OpenAI-shaped verbose_json response -> (segments, words).

    Shared by the OpenAI and Groq paths: both return {"text",
    "segments": [{start, end, text}], "words": [{start, end, word}]}.
    Word entries are optional - when an engine omits them the caller
    approximates timings from the segment text (words_from_segments).
    """
    segments = []
    for seg in getattr(result, "segments", None) or []:
        is_dict = isinstance(seg, dict)
        text = (seg.get("text") if is_dict else getattr(seg, "text", None)) or ""
        text = str(text).strip()
        if not text:
            continue
        try:
            start = float(seg.get("start") if is_dict else seg.start)
            end = float(seg.get("end") if is_dict else seg.end)
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
        segments.append({
            "start": round(start, 3), "end": round(end, 3), "text": text,
        })

    words = []
    for w in getattr(result, "words", None) or []:
        is_dict = isinstance(w, dict)
        start = w.get("start") if is_dict else getattr(w, "start", None)
        end = w.get("end") if is_dict else getattr(w, "end", None)
        if start is None or end is None:
            continue
        try:
            words.append({"start": float(start), "end": float(end)})
        except (TypeError, ValueError):
            continue
    return segments, words


def _transcribe_api(audio_path: Path):
    """OpenAI whisper-1 API -> ({start, end, text} segments, {start, end} words)."""
    client = _get_transcribe_client()
    with open(audio_path, "rb") as fh:
        result = client.audio.transcriptions.create(
            model=os.getenv("OPENAI_STT_MODEL", "whisper-1"),
            file=fh,
            response_format="verbose_json",
            # Ask for word-level timestamps too; the API only honours this
            # for verbose_json responses.
            timestamp_granularities=["word", "segment"],
        )
    segments, words = _parse_api_transcription(result)
    if segments and not words:
        words = words_from_segments(segments)
    return segments, words


def _is_granularity_error(exc):
    """True when an engine rejected timestamp_granularities.

    Word-level granularity is a per-engine feature; when one refuses it
    we retry with segments only and approximate word timings locally
    instead of failing the job.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return "granularit" in text


def _transcribe_groq(audio_path: Path):
    """Groq whisper (free tier) -> ({start, end, text}, {start, end} words)."""
    client = _get_groq_client()
    if client is None:
        raise RuntimeError("GROQ_API_KEY is not set.")
    model = os.getenv("GROQ_STT_MODEL", GROQ_DEFAULT_MODEL)
    result = None
    for granularities in (["word", "segment"], ["segment"]):
        with open(audio_path, "rb") as fh:
            try:
                result = client.audio.transcriptions.create(
                    model=model,
                    file=fh,
                    response_format="verbose_json",
                    timestamp_granularities=granularities,
                )
                break
            except Exception as exc:  # noqa: BLE001 - retried without words
                if granularities == ["segment"] or not _is_granularity_error(exc):
                    raise
    segments, words = _parse_api_transcription(result)
    if not segments:
        raise RuntimeError("Groq returned no usable transcript segments.")
    if not words:
        # Engine omitted word timings: approximate from the segment text,
        # which is all the segmentation engine needs (same trick the
        # on-device WebGPU path uses).
        words = words_from_segments(segments)
    return segments, words


def _local_enabled():
    """STT_LOCAL=0 skips faster-whisper (saves RAM + the weights download)."""
    return os.getenv("STT_LOCAL", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _api_configured():
    """True when at least one API engine has credentials in this process."""
    if os.getenv("GROQ_API_KEY", "").strip():
        return True
    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("WHISPER_API_KEY"))


def _should_warm_local():
    """Whether to pre-load faster-whisper at startup.

    Derived from the RESOLVED chain, so the STT_LOCAL=0 fail-safe (which
    puts local back as the only engine) still gets a warm model. When an
    API engine is configured, local stays in the chain as a LAST RESORT
    (loaded lazily, and only if an API call actually fails) - but a Groq
    deployment must not pay a ~75MB weights download plus a resident
    model on every cold start: on a 512MB host that starves the API call
    doing the real work.
    """
    if "local" not in _engine_order():
        return False
    if os.getenv("STT_ENGINE", "auto").strip().lower() == "local":
        return True
    return not _api_configured()


def _engine_order():
    """Engines to try: STT_ENGINE pins one, otherwise Groq first."""
    pinned = os.getenv("STT_ENGINE", "auto").strip().lower()
    if pinned in ("", "auto"):
        order = ["groq", "local", "openai"]
    elif pinned in ("groq", "local", "openai"):
        order = [pinned]
    else:
        raise RuntimeError(
            f"Unknown STT_ENGINE={pinned!r} (use auto, groq, local or openai)."
        )
    if "local" in order and not _local_enabled():
        # Dropping local is only safe while an API engine can take the job.
        api_ok = (
            ("groq" in order and os.getenv("GROQ_API_KEY", "").strip())
            or ("openai" in order and (
                os.getenv("OPENAI_API_KEY") or os.getenv("WHISPER_API_KEY")
            ))
        )
        if api_ok or pinned not in ("", "auto"):
            order.remove("local")
        else:
            # Misconfiguration guard: STT_LOCAL=0 with no API engine
            # configured would fail every job. Keep local and say so.
            print("[sync] STT_LOCAL=0 but no API engine is configured "
                  "(GROQ_API_KEY / OPENAI_API_KEY) - using faster-whisper "
                  "anyway so jobs keep working")
            order = ["local"]
    return order


def transcribe_with_engine(audio_path: Path):
    """Voiceover -> (engine, segments, words), trying engines in order.

    The engine name is returned so the job manifest can record which
    one served the transcript. Engines that are not configured are
    skipped; an engine that fails falls through to the next, so a
    free-tier hiccup never fails a job.
    """
    errors = []
    silent = None       # valid run, no speech detected (last resort)
    for name in _engine_order():
        if name == "groq" and _get_groq_client() is None:
            continue
        if name == "openai" and not (
            os.getenv("OPENAI_API_KEY") or os.getenv("WHISPER_API_KEY")
        ):
            continue
        try:
            if name == "groq":
                segments, words = _transcribe_groq(audio_path)
            elif name == "local":
                segments, words = _transcribe_local(audio_path)
            else:
                segments, words = _transcribe_api(audio_path)
            if segments:
                return name, segments, words
            if silent is None:
                silent = (name, segments, words)
        except Exception as exc:  # noqa: BLE001 - try the next engine
            errors.append(f"{name}: {exc}")
            # A silent fallback hides WHY the job ran on a different
            # engine than expected - say so in the console and in
            # stats.db so a "why whisper?" question is answerable.
            print(f"[sync] transcribe: {name} failed ({exc}) - "
                  f"trying the next engine", flush=True)
            _log_transcribe_event("engine_fallback",
                                  f"{name}: {str(exc)[:300]}")
    if silent is not None:
        return silent
    detail = "; ".join(errors) or "no transcription engine is configured"
    raise RuntimeError(
        f"Transcription failed ({detail}). Set GROQ_API_KEY for the free "
        f"API, install faster-whisper locally, or set OPENAI_API_KEY."
    )


def transcribe(audio_path: Path):
    """
    Voiceover -> ({start, end, text} speech segments, {start, end} words).

    Tries the configured engines in order (see the chain docs at the top
    of this module); see transcribe_with_engine() for the engine-aware
    variant used by the job worker.
    """
    _engine, segments, words = transcribe_with_engine(audio_path)
    return segments, words


# ============================================================
# CLIENT-SIDE (WebGPU) TRANSCRIPTION  -  helpers
#
# The browser optionally runs Whisper itself (transformers.js + WebGPU,
# see static/client-transcribe.js) and POSTs the segment chunks here.
# These helpers validate/normalize that untrusted input so the server
# can adopt it exactly like faster-whisper's output - the rest of the
# pipeline (resolve_visual_segments, beat_planner) then works unchanged.
# ============================================================

CLIENT_TRANSCRIBE_MAX_SEGMENTS = 400
CLIENT_TRANSCRIBE_MAX_TEXT = 400


def client_transcribe_enabled() -> bool:
    """SYNC_CLIENT_TRANSCRIBE env toggle: may the browser's transcript be
    adopted instead of running server-side transcription? Disabled by
    default so every voiceover goes through the server chain
    (groq -> local -> openai) on every deployment; set
    SYNC_CLIENT_TRANSCRIBE=1 to let a WebGPU-capable browser transcribe
    on-device instead. Anything that is not an explicit opt-in is off."""
    return os.getenv("SYNC_CLIENT_TRANSCRIBE", "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


def clean_client_segments(raw_segments, audio_duration):
    """Validate/normalize browser transcript chunks -> [{start, end, text}]
    or None when nothing usable is left.

    Client input is untrusted: drop non-dicts, non-finite/negative times,
    empty text, out-of-order chunks and times past the audio's end; clamp
    small overlaps from chunked Whisper output; cap count and text length.
    """
    if not isinstance(raw_segments, list) or not raw_segments:
        return None
    cleaned = []
    prev_end = 0.0
    for seg in raw_segments[:CLIENT_TRANSCRIBE_MAX_SEGMENTS]:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError):
            continue
        text = str(seg.get("text") or "").strip()
        if not (start >= 0 and end > start and text):
            continue
        if audio_duration and audio_duration > 0 and end > audio_duration + 1:
            continue
        # Chunked output can overlap by a few ms: clamp instead of drop.
        start = max(start, prev_end)
        if end <= start:
            continue
        cleaned.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text[:CLIENT_TRANSCRIBE_MAX_TEXT],
        })
        prev_end = end
    return cleaned or None


def words_from_segments(segments):
    """Approximate word timings from segment text: [{'start', 'end'}, ...].

    faster-whisper emits real word timestamps, the client pass does not.
    The segmentation engine only needs monotonic in-range word boundaries
    (and tolerates an empty list), so split each segment's text on
    whitespace and share its time proportionally to character count.
    Word dicts mirror _transcribe_local's shape: {'start', 'end'}.
    """
    words = []
    for seg in segments or []:
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not (start >= 0 and end > start):
            continue
        tokens = str(seg.get("text") or "").split()
        if not tokens:
            continue
        span = end - start
        weights = [len(t) + 1 for t in tokens]
        total = float(sum(weights))
        cursor = start
        for i, w in enumerate(weights):
            w_end = end if i == len(weights) - 1 else start + span * (
                sum(weights[:i + 1]) / total
            )
            if w_end > cursor:
                words.append({
                    "start": round(cursor, 3),
                    "end": round(w_end, 3),
                })
            cursor = w_end
    return words


def file_sha256(path) -> str:
    """SHA-256 hex of a stored file ("" when unreadable) - used to prove
    an on-device transcript was produced from the exact uploaded bytes."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def get_media_duration(path: Path) -> float:
    """ffprobe duration in seconds (fallback: 0)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(out.stdout or "{}")
        return float(data.get("format", {}).get("duration", 0.0))
    except Exception:
        return 0.0


def match_images_to_segments(segments, image_count):
    """Return a copy of segments with each assigned an image filename index.

    When counts match: segment i -> image i (1-based). When they don't,
    image is None — the client shows the mismatch UI.
    """
    matched = image_count == len(segments)
    out = []
    for i, seg in enumerate(segments):
        s = dict(seg)
        s["image"] = i if matched and i < image_count else None
        out.append(s)
    return out, matched


# ---------------------------------------------------------------
# Manifest storage
# ---------------------------------------------------------------

def job_dir(job_id: str) -> Path:
    return UPLOAD_FOLDER / job_id


def manifest_path(job_id: str) -> Path:
    return job_dir(job_id) / "manifest.json"


def load_manifest(job_id: str):
    p = manifest_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_manifest(job_id: str, manifest: dict):
    manifest["updated_at"] = time.time()
    manifest_path(job_id).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _prepare_transcription_source(audio_file: Path) -> Path:
    """Normalize the voiceover for Whisper: 16 kHz mono WAV.

    Uploaded MP3s are sometimes written with corrupt/missing frame
    headers: players and the ffmpeg CLI resync silently, but the decoder
    inside faster-whisper stops at the first bad frame (a 52s voiceover
    once yielded only the first ~3s of words, so every image after the
    first fell back to proportional timing). Decoding through ffmpeg
    produces a clean WAV whose timestamps map 1:1 onto the original,
    which is still used untouched for the final audio mux. Returns the
    original path when ffmpeg is unavailable or conversion fails, so
    transcription keeps working.
    """
    wav_path = audio_file.with_name("audio_16k.wav")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-nostdin", "-v", "error",
             "-i", str(audio_file),
             "-ac", "1", "-ar", "16000", "-vn",
             str(wav_path)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and wav_path.exists():
            return wav_path
    except (OSError, subprocess.SubprocessError):
        pass
    return audio_file


def resolve_visual_segments(words, segments_with_text, image_count, audio_duration):
    """Transcript -> timeline segments for `image_count` images.

    The exact resolution the transcription worker runs, shared with the
    add-images route: when the voiceover is transcribed before any images
    exist (audio-first job), the same computation runs later once images
    are added — no second Whisper run. Returns
    (segments, matched, sync_warning), mirroring the worker's manifest
    fields. Raises only for non-validation failures.
    """
    try:
        # The core sync contract: image i is shown during visual
        # segment i. create_visual_segments() produces exactly
        # image_count contiguous segments, cutting on real sentence
        # starts where possible (so an image flips exactly when its
        # sentence begins) and never mid-word, running 0 -> end of the
        # audio.
        sentence_starts = []
        for s in segments_with_text or []:
            try:
                sentence_starts.append(float(s.get("start")))
            except (TypeError, ValueError, AttributeError):
                continue
        visual = create_visual_segments(
            words, image_count, audio_duration,
            sentence_starts=sentence_starts,
        )
        segments = [
            {
                "start": seg["start"],
                "end": seg["end"],
                "image": i,
            }
            for i, seg in enumerate(visual)
        ]
        return segments, True, None
    except SegmentValidationError as exc:
        # More images than the speech can serve (e.g. 20 images for
        # a 10s voiceover): surface a useful validation error and
        # keep the old segment->image matching so the user can fix
        # the timeline instead of getting a broken video.
        segs, matched = match_images_to_segments(
            segments_with_text, image_count
        )
        return segs, matched, str(exc)


def clips_from_segments(segments):
    """Manifest clips mirroring the visual segments (image i, duration
    i). Keeps the on-screen timeline and the rendered video in lockstep:
    the client rebuilds its blocks from segments, and the sync_update
    route compares saved clips against the incoming layout."""
    clips = []
    for seg in segments or []:
        try:
            idx = int(seg.get("image"))
            dur = float(seg["end"]) - float(seg["start"])
        except (KeyError, TypeError, ValueError):
            continue
        clips.append({
            "image": idx,
            "duration": round(
                max(MIN_CLIP_SECONDS, min(MAX_CLIP_SECONDS, dur)), 3
            ),
        })
    return clips


def segments_from_clips(clips, audio_duration):
    """Authoritative visual segments derived from the client's clip layout.

    The preview edits CLIPS; synced renders read SEGMENTS. Deriving one
    from the other on every real change means a drag, a resize or a
    script retime always lands in both - preview == render.

    Rules:
      * contiguous segments in clip order (image = clip's image);
      * the final segment always ends at the audio's end, so a short
        timeline extends on the LAST image (it holds the tail) and the
        video always covers the full voiceover;
      * a timeline longer than the audio is squeezed from the tail so
        no image is pushed past the end (each keeps at least
        MIN_CLIP_SECONDS; with pathological counts an equal split is
        the only feasible layout).
    """
    audio_duration = float(audio_duration or 0.0)
    n = len(clips)
    if not n or audio_duration <= 0:
        return []

    min_d = MIN_CLIP_SECONDS
    durs = [c["duration"] for c in clips]
    total = sum(durs)

    if n * min_d > audio_duration:
        # Not enough audio even for minimum clips: equal split (the
        # resolve path raises the real validation error for this case).
        durs = [audio_duration / n] * n
    elif total < audio_duration:
        durs[-1] += audio_duration - total   # last image holds the tail
    elif total > audio_duration:
        # Cumulative clamp: earlier clips keep their length, the overflow
        # squeezes out of the tail, and every image still gets min_d.
        fitted = []
        prev = 0.0
        for k, d in enumerate(durs):
            upper = audio_duration - (n - 1 - k) * min_d
            end = min(prev + d, upper)
            if end < prev + min_d:
                end = prev + min_d
            if k == n - 1:
                end = audio_duration
            fitted.append(end - prev)
            prev = end
        durs = fitted

    segs = []
    cum = 0.0
    for i, d in enumerate(durs):
        start = round(cum, 3)
        cum = round(cum + d, 3)
        # Same shape the transcription worker has always stored
        # ({start, end, image} - no index key).
        segs.append({
            "start": start,
            "end": cum,
            "image": clips[i]["image"],
        })

    # Rounding safety: strictly increasing, ending exactly at the audio.
    prev_end = 0.0
    for s in segs:
        if s["start"] < prev_end:
            s["start"] = prev_end
        if s["end"] <= s["start"]:
            s["end"] = round(s["start"] + 0.1, 3)
        prev_end = s["end"]
    if segs:
        segs[-1]["end"] = round(audio_duration, 3)
        if segs[-1]["end"] <= segs[-1]["start"]:
            # Degenerate (timeline pinned past the end): fall back to an
            # equal split so the manifest can never contain an empty
            # segment the server would reject.
            step = round(audio_duration / len(segs), 3)
            for i, s in enumerate(segs):
                s["start"] = round(step * i, 3)
                s["end"] = (
                    round(audio_duration, 3) if i == len(segs) - 1
                    else round(step * (i + 1), 3)
                )
    return segs


def find_duplicate_images(folder, names):
    """Content groups of STORED image files: [[first, dup, ...], ...].

    Only groups with more than one member come back. Byte-identical
    files (the same picture picked twice) can never produce a visible
    cut, so the video looks out of sync even when its timing is exact -
    job 0d3be1a34c3f hit this: img_001..img_004 shared one hash, so the
    first 19.5s correctly showed a single picture and every verification
    probe reported "missing cut" for those three beats.
    """
    groups = {}
    for name in names or []:
        try:
            digest = hashlib.sha256((folder / name).read_bytes()).hexdigest()
        except OSError:
            continue          # unreadable file must never fail an upload
        groups.setdefault(digest, []).append(name)
    return [g for g in groups.values() if len(g) > 1]


def _image_label(name):
    """img_003.jpeg -> 'Image 3' (falls back to the raw filename)."""
    stem = str(name).split("_", 1)[-1].split(".")[0]
    try:
        return f"Image {int(stem)}"
    except ValueError:
        return str(name)


def duplicate_image_warning(groups, image_count=None):
    """User-facing "the same picture is in here twice" warning, or None.

    Deliberately a warning and not an error: repeats are a legal choice
    (a talking-head shot held across beats), they just cannot cut.
    """
    if not groups:
        return None
    extra = sum(len(g) - 1 for g in groups)
    repeats = "; ".join(
        f"{_image_label(g[0])} is also "
        + ", ".join(_image_label(n) for n in g[1:])
        for g in groups[:3]
    )
    if len(groups) > 3:
        repeats += f" (and {len(groups) - 3} more)"
    scope = (f"{extra} of your {image_count} images"
             if image_count else f"{extra} image{'' if extra == 1 else 's'}")
    return (
        f"{scope} repeat the same picture - {repeats}. A picture cannot "
        "cut to itself, so the video holds that image through every beat "
        "it repeats on; upload a different picture for those moments to "
        "see a change there."
    )


def sync_image_warning(job_id):
    """Recompute the duplicate-image warning for a stored job (None when
    the images are all different or the job is unknown)."""
    manifest = load_manifest(job_id)
    if not manifest:
        return None
    names = manifest.get("images") or []
    groups = find_duplicate_images(job_dir(job_id), names)
    return duplicate_image_warning(groups, len(names))


def manifest_image_warning(manifest, job_id):
    """The job's stored duplicate-image warning.

    Jobs uploaded before this check existed have no field, so they are
    hashed on demand (a few ms for a handful of images) instead of the
    route writing to the manifest behind the transcription worker's back.
    """
    if "image_warning" in manifest:
        return manifest["image_warning"]
    return sync_image_warning(job_id)


def _log_transcribe_event(event, detail):
    """Best-effort stats event for transcription timing (never raises).

    Makes slow server runs diagnosable from stats.db: every worker run
    logs `transcribe_done | <job> <N>s audio in <M>s wall` (or
    `transcribe_failed | ... after <M>s: <error>`).
    """
    try:
        import stats as _stats
        _stats.log_event("", "", event, detail)
    except Exception:  # noqa: BLE001 - stats must not break transcription
        pass


def start_transcription_job(job_id: str):
    """Run Whisper in a background thread; manifest flips to ready/error."""
    manifest = load_manifest(job_id)
    if not manifest:
        raise FileNotFoundError(job_id)
    manifest["status"] = "processing"
    # Drop the PREVIOUS run's engine label: the page's "via ..." badge
    # would otherwise claim groq while this run may be served by
    # another engine (the worker re-records it on success).
    manifest.pop("transcript_engine", None)
    save_manifest(job_id, manifest)

    audio_file = job_dir(job_id) / manifest["audio"]

    def _worker():
        started = time.time()
        try:
            # Transcribe a normalized copy; the original stays as the
            # render's mux source.
            engine, segments, words = transcribe_with_engine(
                _prepare_transcription_source(audio_file)
            )
            m = load_manifest(job_id) or manifest
            image_count = len(m.get("images") or [])
            audio_duration = get_media_duration(audio_file)
            if image_count:
                (m["segments"],
                 m["matched"],
                 m["sync_warning"]) = resolve_visual_segments(
                    words, segments, image_count, audio_duration
                )
                # Preview (clips) mirrors the resolved timeline so the
                # manifest is self-consistent before any client loads -
                # and so a later client save cannot mistake the old clip
                # layout for a real edit.
                m["clips"] = clips_from_segments(m["segments"])
            else:
                # Audio-first job: no images to time yet. Keep the sync
                # timeline empty; the add-images route resolves the exact
                # same segments once images arrive.
                m["segments"] = []
                m["matched"] = False
                m["sync_warning"] = None
            m["words"] = words  # kept for debugging / future re-syncs
            # Whisper's text-bearing segments, kept for the advisory beat
            # planner (voiceover -> suggested image count + visual beats).
            # The sync timeline above needs only timings; the sync engine
            # never reads this key.
            m["segments_with_text"] = segments
            # Which engine produced this transcript (groq | local | openai)
            # - shown on the admin page alongside the timing event.
            m["transcript_engine"] = engine
            m["status"] = "ready"
            m["error"] = None
            save_manifest(job_id, m)
            _log_transcribe_event(
                "transcribe_done",
                f"{job_id} {audio_duration:.0f}s audio in "
                f"{time.time() - started:.0f}s wall via {engine}",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the client
            # Log FIRST: if the job dir vanished (job deleted mid-run) the
            # save below raises, and the timing event must still land.
            _log_transcribe_event(
                "transcribe_failed",
                f"{job_id} after {time.time() - started:.0f}s: {exc}",
            )
            m = load_manifest(job_id) or manifest
            m["status"] = "error"
            m["error"] = str(exc)
            try:
                save_manifest(job_id, m)
            except Exception as save_exc:  # noqa: BLE001
                print(f"[sync] could not persist error state for "
                      f"{job_id}: {save_exc}")

    threading.Thread(target=_worker, daemon=True).start()



# ============================================================
# RENDER  (matched timeline -> single 16:9 MP4)
#
# Each image is displayed for exactly its segment's duration, all
# segments are concatenated in order, and the original voiceover is
# muxed as the audio track. Output is a fixed 1920x1080 (16:9) frame:
# mismatched image aspect ratios are letterboxed with black bars,
# matching the FIT-mode behaviour of the rest of the app.
# ============================================================

# Screen time given to each image when the job has no speech timestamps
# (no voiceover yet, or transcription was never run).
DEFAULT_CLIP_SECONDS = 3.0
MIN_CLIP_SECONDS = 0.5
MAX_CLIP_SECONDS = 60.0


def _clean_clip(clip, image_count):
    """Validate one timeline clip -> (image_index, duration) or None."""
    try:
        idx = int(clip.get("image"))
        dur = float(clip.get("duration"))
    except (AttributeError, TypeError, ValueError):
        return None
    if not 0 <= idx < image_count:
        return None
    if dur <= 0:
        return None
    return idx, max(MIN_CLIP_SECONDS, min(MAX_CLIP_SECONDS, dur))


def _segment_plan(segments, images, audio_duration):
    """
    Synced mode -> [(image_index, screen_seconds)].

    Screen time runs from a segment's start to the NEXT segment's start,
    so pauses between sentences are held on the previous image instead of
    being skipped (skipping them used to make the images creep ahead of
    the voiceover). The last image holds to the end of the audio.
    """
    plan = []
    for i, seg in enumerate(segments):
        idx = seg.get("image")
        if not isinstance(idx, int) or not 0 <= idx < len(images):
            raise RuntimeError(f"Segment {i + 1} has no image assigned.")

        try:
            start = max(0.0, float(seg.get("start") or 0.0))
            end = float(seg.get("end") or 0.0)
        except (TypeError, ValueError):
            raise RuntimeError(f"Segment {i + 1} has invalid timestamps.")
        if end <= start:
            raise RuntimeError(f"Segment {i + 1} ends before it starts.")

        if i + 1 < len(segments):
            try:
                hold_until = float(segments[i + 1].get("start") or end)
            except (TypeError, ValueError):
                hold_until = end
            hold_until = max(hold_until, end)
        else:
            hold_until = max(end, audio_duration or end)

        # The first image also covers any leading silence, so the rendered
        # video runs 0 -> end of the audio and video time maps 1:1 onto
        # audio time (no drift when the voiceover starts with a pause).
        block_start = 0.0 if i == 0 else start
        plan.append((idx, max(0.1, hold_until - block_start)))
    return plan


def _clip_plan(clips, images):
    """
    Slideshow mode -> [(image_index, screen_seconds)] from the timeline.

    Falls back to one block per image at the default length when the
    client has not sent its timeline yet, so a render always succeeds as
    long as the job has images.
    """
    plan = [c for c in (_clean_clip(c, len(images)) for c in clips) if c]
    if plan:
        return plan
    return [(i, DEFAULT_CLIP_SECONDS) for i in range(len(images))]


# Images are decoded at FULL size for every rendered frame, and all image
# inputs run through the filter graph simultaneously. A 2752x1536 photo is
# ~12 MB of raw pixels per frame; several such inputs queueing while x264
# crawls at a few fps exhausted the commit budget on small machines and
# ffmpeg died mid-render with "Cannot allocate memory". Anything larger
# than the 1080p output frame is therefore pre-shrunk once with Pillow
# (already a dependency) into a cached copy next to the original.
MAX_RENDER_LONG_EDGE = 1920


def _shrink_for_render(folder: Path, image_name: str) -> Path:
    """Return a render-safe path for an image, shrinking it if needed.

    Oversized images are downsampled once to MAX_RENDER_LONG_EDGE on the
    long edge and cached as <stem>_r.jpg in the job folder; later renders
    reuse the cache. The original upload is never modified. Falls back to
    the original path when Pillow is unavailable or the file cannot be
    processed (the renderer's own scale/pad filter still handles it).
    """
    src = folder / image_name
    if not src.exists():
        return src
    try:
        from PIL import Image
    except ImportError:
        return src

    cache = src.with_name(src.stem + "_r.jpg")
    if cache.exists():
        return cache

    try:
        with Image.open(src) as im:
            width, height = im.size
            long_edge = max(width, height)
            if long_edge <= MAX_RENDER_LONG_EDGE:
                return src
            scale = MAX_RENDER_LONG_EDGE / long_edge
            resized = im.convert("RGB").resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.LANCZOS,
            )
        resized.save(cache, "JPEG", quality=90)
        resized.close()
    except Exception:  # noqa: BLE001 - never block a render on pre-shrink
        return src
    return cache



THUMB_LONG_EDGE = 480
THUMB_QUALITY = 78


def make_thumbnail(folder: Path, image_name: str):
    """Create/return "<stem>_t.jpg" for an image, or None when useless.

    The sync timeline paints every uploaded picture as a small filmstrip
    tile, so it needs a tiny copy - not the 1920px original (a 20-image
    job was pulling ~10MB per page load just to draw thumbnails, through
    a CDN that could not cache them). Generated once at upload time
    (~20-40ms) and reused on every later load. Never raises: a missing
    thumbnail simply means the client keeps using the full image.
    """
    src = folder / image_name
    if not src.exists():
        return None
    try:
        from PIL import Image
    except ImportError:
        return None

    name = f"{src.stem}_t.jpg"
    out = src.with_name(name)
    if out.exists():
        return name
    try:
        with Image.open(src) as im:
            width, height = im.size
            long_edge = max(width, height)
            if long_edge <= THUMB_LONG_EDGE:
                return None       # already tiny - serve the original
            scale = THUMB_LONG_EDGE / long_edge
            thumb = im.convert("RGB").resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.LANCZOS,
            )
        thumb.save(out, "JPEG", quality=THUMB_QUALITY)
        thumb.close()
    except Exception:  # noqa: BLE001 - a thumbnail is never load-bearing
        return None
    return name


def thumbnail_names(folder: Path, names):
    """[thumbnail filename or None, per image] for the sync payloads.

    Index-aligned with the image list so the client can pick a tile URL
    per image. Jobs uploaded before thumbnails existed simply get None
    everywhere and keep using the full images - no migration needed.
    """
    out = []
    for name in names or []:
        thumb = f"{Path(name).stem}_t.jpg"
        out.append(thumb if (folder / thumb).exists() else None)
    return out


def _render_fps():
    """Still-input frame rate for the render concat (SYNC_RENDER_FPS).

    Cut points live on this source frame grid, so this IS the cut
    precision: at 2 fps a boundary could only land on a 0.5s step, which
    is the visible "the image waits a moment after the sentence ends"
    drift. 30 fps puts every switch on the same grid as the output video
    (<= 1/30s off, i.e. at most one frame).

    Measured cost on a 10-image / 52s job: peak 265 MB and 0.5x realtime
    at 30 fps vs 1.05x at 2 fps. Memory is driven by the NUMBER of inputs
    (one queued packet each, see `-thread_queue_size 1` below), not by
    this value - a 30-image job peaked at ~490 MB at either rate - so the
    finer grid buys exact cuts for encode time only. Lower it (or set
    SYNC_RENDER_FPS=2) on a very memory- or CPU-tight host.
    """
    try:
        fps = int(os.getenv("SYNC_RENDER_FPS", "30"))
    except (TypeError, ValueError):
        fps = 30
    return max(1, min(60, fps))


def _frame_counts(durations, fps):
    """Quantize segment durations onto the fps frame grid WITHOUT drift.

    Each count is derived from the CUMULATIVE plan time, so the cut after
    segment k lands within half a frame of its timeline position and
    rounding errors never pile up (the old code let every segment round
    itself independently, which pushed late cuts up to ~0.7s behind the
    audio by the end of a 10-image slideshow).

    Whole frames (not seconds) are what the render graph consumes: each
    still is scaled ONCE and then duplicated to exactly this many frames
    (see build_render_command), so `trim=end_frame=N` is exact where a
    floating-point -t can land between frames.
    """
    out = []
    prev_frames = 0
    cum = 0.0
    for dur in durations:
        cum += max(0.05, float(dur))
        target = int(round(cum * fps))
        frames = max(1, target - prev_frames)
        out.append(frames)
        prev_frames += frames
    return out


def _frame_durations(durations, fps):
    """The same grid expressed in seconds.

    Still used by the legacy graph, the verification tools and the tests;
    derived from _frame_counts so the two can never disagree.
    """
    return [count / fps for count in _frame_counts(durations, fps)]


# Encoder threads for the slideshow render. This was hard-coded to 2 to
# protect a 512MB host, and 2 stays the DEFAULT: measured on a box with no
# idle cores, going from 2 to 8 threads gained nothing (3.1s vs 3.5s per 60
# frames of 1080p), and libx264's per-thread memory grows with resolution.
# A deployment that really does have spare cores raises it with
# SYNC_FFMPEG_THREADS - or with the shared FFMPEG_THREADS that app.py
# already honours for the editor export - so both paths answer to one knob.
# 0 means "let x264 decide".
_SYNC_THREADS_DEFAULT = 2
_SYNC_THREADS_MAX = 32


def _render_threads():
    """libx264 threads for the sync render (see _SYNC_THREADS_DEFAULT)."""
    for name in ("SYNC_FFMPEG_THREADS", "FFMPEG_THREADS"):
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue          # unusable value: fall through to the default
        return max(0, min(_SYNC_THREADS_MAX, value))
    return _SYNC_THREADS_DEFAULT


def _render_filter_threads():
    """Threads for the filter graph (-filter_complex_threads).

    Default 1 keeps the single-threaded, low-memory profile the 512MB
    deployments were tuned for; a host with room to spare raises it with
    SYNC_FFMPEG_FILTER_THREADS.
    """
    try:
        value = int(os.getenv("SYNC_FFMPEG_FILTER_THREADS", "1"))
    except (TypeError, ValueError):
        value = 1
    return max(1, min(16, value))


def _legacy_graph():
    """Opt back into the old per-frame `-loop 1 -t` graph (one env var).

    The default graph decodes and scales each still ONCE and duplicates the
    result with the `loop` filter, which removes 30 still decodes + scales
    per second per segment. This flag is only a rollback valve if a host
    ever disagrees with it.
    """
    return str(os.getenv("SYNC_RENDER_LEGACY_GRAPH", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def build_render_command(job_id: str, output_path: Path):
    """
    Build the FFmpeg command that renders a sync job as a 16:9 MP4.

    Two modes:
      * synced    - speech segments exist: each image is shown for its
                    segment's screen time and the voiceover is muxed in.
      * slideshow - no segments: the timeline's own order and per-image
                    durations are rendered silent, so the tool works with
                    or without a transcription.

    Returns (command, total_duration_seconds). Raises RuntimeError with a
    user-readable message when the job cannot be rendered yet.
    """
    manifest = load_manifest(job_id)
    if not manifest:
        raise FileNotFoundError("Unknown job.")

    images = manifest.get("images") or []
    segments = manifest.get("segments") or []
    clips = manifest.get("clips") or []
    if not images:
        raise RuntimeError("No images in this job.")

    folder = job_dir(job_id)
    audio_path = None

    if segments:
        # Segments exist only when transcription finished (the worker)
        # or when sync_update derived them from the clip layout (a
        # script apply on a job whose transcription failed still renders
        # with its typed timing) - both are renderable; mid-transcription
        # is not.
        if manifest.get("status") in ("transcribing", "processing"):
            raise RuntimeError("Transcription is not finished yet.")
        if not manifest.get("matched"):
            raise RuntimeError(
                "Timeline is not fully matched - every segment needs an image."
            )
        audio_name = manifest.get("audio")
        candidate = folder / audio_name if audio_name else None
        if not candidate or not candidate.exists():
            raise RuntimeError("Audio file is missing.")
        audio_path = candidate
        plan = _segment_plan(segments, images, get_media_duration(audio_path))
    else:
        # A job with a voiceover but no segments yet must NOT fall through
        # to the silent slideshow: that used to render images at 3s each
        # with the audio dropped (-an) whenever the user clicked Build
        # while Whisper was still transcribing. The downloaded file then
        # had no audio and no sync. Guard it here and tell the user why.
        if manifest.get("audio"):
            status = manifest.get("status")
            if status in ("transcribing", "processing"):
                raise RuntimeError(
                    "Voiceover is still transcribing - please wait a "
                    "moment and build again."
                )
            if status == "error":
                raise RuntimeError(
                    "Transcription failed: "
                    + (manifest.get("error") or "unknown error")
                    + " - fix the voiceover and re-upload it."
                )
            # status == ready but no segments (e.g. corrupt audio produced
            # nothing at all): same guard, different wording.
            raise RuntimeError(
                "No speech could be synced from this voiceover - "
                "check the audio file and re-upload it."
            )
        plan = _clip_plan(clips, images)


    if not plan:
        raise RuntimeError("Nothing to render.")

    command = ["ffmpeg", "-y", "-nostdin"]
    filters = []
    total_weight = 0.0

    # Frame-grid quantization (see _frame_counts): each segment's screen
    # time is an exact INTEGER number of frames taken from the CUMULATIVE
    # timeline, so boundary errors stay within half a frame and never
    # accumulate across the slideshow.
    render_fps = _render_fps()
    frame_counts = _frame_counts([d for _, d in plan], render_fps)
    legacy = _legacy_graph()

    for i, (idx, dur) in enumerate(plan):
        img_path = _shrink_for_render(folder, images[idx])
        if not img_path.exists():
            raise RuntimeError(f"Image file missing: {images[idx]}")
        total_weight += dur

        command += [
            # Bound this input's read-ahead to a single packet. concat
            # only consumes the ACTIVE input, so without this the image2
            # demuxer runs ahead and queues every remaining still's frames
            # (the reason the source grid used to be capped at 2 fps).
            # One packet per input keeps a 30 fps grid at a few MB.
            "-thread_queue_size", "1",
            "-framerate", str(render_fps),
        ]
        if legacy:
            command += ["-loop", "1", "-t", f"{frame_counts[i] / render_fps:.6f}"]
        command += ["-i", str(img_path)]

        # Scale + letterbox ONCE per still. The default graph then hands that
        # single finished frame to the `loop` filter, which duplicates it to
        # exactly this segment's frame count - so the still is decoded and
        # scaled once per segment instead of once per output frame (30x/s).
        base = (
            f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,format=yuv420p"
        )
        if legacy:
            filters.append(f"{base}[v{i}]")
        else:
            filters.append(
                f"{base},loop=loop=-1:size=1:start=0,"
                f"trim=end_frame={frame_counts[i]},"
                f"setpts=PTS-STARTPTS[v{i}]"
            )

    # The audio input must be declared BEFORE the filter/map options:
    # FFmpeg applies any option that precedes an -i to that input, so a
    # -map placed before it fails with "Option map ... cannot be applied
    # to input url".
    if audio_path:
        command += ["-i", str(audio_path)]

    labels = "".join(f"[v{i}]" for i in range(len(plan)))
    # Every source segment already sits on the render fps grid (see
    # _render_fps / _frame_counts), so concat cuts exactly where the timeline
    # says. The trailing fps= normalises the stream for the encoder at that
    # SAME grid - it used to be hard-coded to 30, which silently disagreed
    # with SYNC_RENDER_FPS on any deployment that lowered it. (An earlier
    # version used 2 fps inputs because concat buffers every not-yet-active
    # input: that queue is now bounded by -thread_queue_size 1 per input,
    # which is what makes the fine grid affordable - unbounded, a 30 fps grid
    # held the whole remaining timeline x 30 frames x ~3 MB and died with
    # "Cannot allocate memory".)
    filters.append(
        f"{labels}concat=n={len(plan)}:v=1:a=0,fps={render_fps}[vout]"
    )

    # True length of the render: the sum of the per-image screen times,
    # which now includes the pauses (used for progress reporting).
    total = total_weight

    command += [
        "-filter_complex", ";".join(filters),
        "-filter_complex_threads", str(_render_filter_threads()),
        "-map", "[vout]",
    ]

    if audio_path:
        command += [
            "-map", f"{len(plan)}:a",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
        ]
    else:
        # Slideshow mode: no voiceover, so no audio track at all.
        command += ["-an"]

    command += [
        "-c:v", "libx264",
        # ultrafast + stillimage: a slideshow has no motion for inter-frame
        # prediction to exploit, so the costlier presets buy nothing —
        # ultrafast roughly halves encode time vs veryfast at CRF 20-21,
        # and -tune stillimage keeps static frames crisp. Override with
        # SYNC_X264_PRESET if a deployment prefers e.g. veryfast.
        "-preset", os.getenv("SYNC_X264_PRESET", "ultrafast"),
        "-tune", "stillimage", "-crf", "21",
        "-pix_fmt", "yuv420p",
        # Memory-lean x264: lookahead frame queues caused "malloc of size
        # 11619264 failed" encoder crashes on small-RAM machines (4-core
        # Celeron at ~99% commit charge) even with several GB nominally
        # free. Disabling the lookaheads removes those buffers with no
        # meaningful quality change at CRF 20; the thread count defaults to
        # 2 for the same reason and is raised per deployment (see
        # _render_threads / SYNC_FFMPEG_THREADS).
        "-threads", str(_render_threads()),
        "-x264-params", "rc-lookahead=0:sync-lookahead=0",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        str(output_path),
    ]
    return command, total

