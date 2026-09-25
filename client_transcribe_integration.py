"""Integration test: on-device transcript adoption end-to-end.

Assumes a local server is running (python app.py) and exercises:
  /sync/audio (client mode) -> /sync/client-transcript -> /sync/beats
  -> /sync/status, second-adoption refusal, hash-mismatch refusal,
  /sync/transcribe guards, and the silent-run watchdog.
"""

import hashlib
import json
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

BASE = "http://127.0.0.1:5000"
UP = Path("static/uploads")


def http(path, data=None, headers=None, method=None):
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body}


def multipart(fields, file_field, filename, content, extra=None,
             content_type="audio/mpeg"):
    """Build a multipart/form-data body.

    `extra` is a list of (filename, content, content_type) tuples added
    under the same field name, for multi-file posts.
    """
    boundary = "----aqboundary7f3a"
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{k}"\r\n\r\n{v}\r\n').encode()
    parts = [(filename, content, content_type)] + list(extra or [])
    for fn, content_bytes, ct in parts:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{file_field}"; filename="{fn}"\r\n'
                 f"Content-Type: {ct}\r\n\r\n").encode()
        body += content_bytes + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def make_job(status, images=()):
    job = "ctest" + uuid.uuid4().hex[:8]
    d = UP / job
    d.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(images):
        Image.new("RGB", (800, 600), (10 * i + 5, 30, 60)).save(d / name)
    manifest = {
        "images": list(images), "audio": None, "audio_name": None,
        "status": status, "matched": None, "error": None,
    }
    (d / "manifest.json").write_text(json.dumps(manifest))
    return job, d


def read_manifest(job):
    return json.loads((UP / job / "manifest.json").read_text())


# ── 0. a REAL 12s tone: ffprobe must report the true duration (garbage
#      bytes make ffprobe return a bogus tiny duration, which the
#      duration caps then correctly reject as out-of-range) ─────────────
import subprocess
_tone = Path("test_voice_tone.wav")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=12",
                "-ac", "1", "-ar", "16000", str(_tone)],
               capture_output=True, check=True, timeout=60)
audio_bytes = _tone.read_bytes()
_tone.unlink()

# ── 1. client-mode upload holds off server Whisper ─────────────────────
job, job_dir = make_job("awaiting_audio", images=("a.png", "b.png"))
body, hdr = multipart(
    {"client_transcribe": "1"}, "audio", "voice.wav", audio_bytes
)
code, resp = http(f"/sync/audio/{job}", data=body, headers=hdr)
assert code == 200, (code, resp)
assert resp.get("client_transcribe") is True, resp
m = read_manifest(job)
assert m["status"] == "transcribing", m["status"]
assert m["client_transcribe"]["state"] == "pending", m
assert not m.get("segments_with_text")
print("1. client-mode upload: server job NOT started, marker pending OK")

# ── 2. adoption: segments -> timeline + beats + status payload ─────────
segments = [
    {"start": 0.0, "end": 5.5, "text": "Welcome to the show everyone."},
    {"start": 5.5, "end": 12.0, "text": "Today we build something great."},
]
payload = {
    "segments": segments, "duration": 12.0, "engine": "webgpu-tiny.en",
    "audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
}
data = json.dumps(payload).encode()
code, resp = http(f"/sync/client-transcript/{job}", data=data,
                  headers={"Content-Type": "application/json"})
assert code == 200, (code, resp)
assert resp["status"] == "ready" and resp["source"] == "client", resp
assert resp["matched"] is True and len(resp["segments"]) == 2, resp
assert resp["has_audio"] and resp["image_count"] == 2, resp
# Thumbnails ride along with every image list, index-aligned. These two
# images were injected straight into the job folder (the real upload
# path is exercised in 2b), so the entries are legitimately null here.
thumbs = resp.get("image_thumbs")
assert isinstance(thumbs, list) and len(thumbs) == 2, resp
m = read_manifest(job)
assert m["status"] == "ready"
assert m["segments_with_text"][0]["text"].startswith("Welcome")
assert m["client_transcribe"]["state"] == "adopted"
assert m.get("beats") and m.get("beats_version"), "beats not cached"
assert len(m["clips"]) == 2 and len(m["words"]) >= 6
print(f"2. adoption OK: matched={resp['matched']}, "
      f"beats={len(m['beats'].get('beats', []))}, "
      f"suggested={m['beats'].get('image_count')}")

# ── 2b. the real /sync/upload path generates filmstrip thumbnails ─────
img_tmp = tempfile.TemporaryDirectory()
try:
    p1 = Path(img_tmp.name) / "one.png"
    p2 = Path(img_tmp.name) / "two.png"
    Image.new("RGB", (1200, 900), (200, 40, 40)).save(p1)
    Image.new("RGB", (1000, 1400), (40, 200, 40)).save(p2)
    body, hdr = multipart(
        {}, "images", "one.png", p1.read_bytes(),
        extra=[("two.png", p2.read_bytes(), "image/png")],
        content_type="image/png",
    )
    code, up = http("/sync/upload", data=body, headers=hdr)
    assert code == 200, (code, up)
    assert up.get("image_count") == 2, up
    up_thumbs = up.get("image_thumbs") or []
    assert len(up_thumbs) == 2 and all(up_thumbs), up
    img_job = up["job_id"]
    for rel in up_thumbs:
        path = UP / img_job / rel.split("/")[-1]
        assert path.exists(), f"missing {rel}"
        # A 480px JPEG is tens of KB; the original PNGs are far bigger.
        assert path.stat().st_size < 150_000, f"thumbnail too big: {path}"
    # ...and they are served on every later status poll too.
    code, st = http(f"/sync/status/{img_job}")
    st_thumbs = st.get("image_thumbs") or []
    assert len(st_thumbs) == 2 and all(st_thumbs), st
    print("2b. /sync/upload generates filmstrip thumbnails (payload + files) OK")
finally:
    img_tmp.cleanup()
    if "img_job" in dir():
        shutil.rmtree(UP / img_job, ignore_errors=True)

# ── 3. beats + status endpoints serve the adopted transcript ───────────
code, resp = http(f"/sync/beats/{job}")
assert code == 200 and resp.get("beats"), (code, resp)
code, resp = http(f"/sync/status/{job}")
assert code == 200 and resp["status"] == "ready", (code, resp)
assert resp.get("updated_at"), "status payload must carry updated_at"
print("3. /sync/beats + /sync/status serve adopted transcript OK")

# ── 4. second adoption refused (server transcript authoritative) ───────
code, resp = http(f"/sync/client-transcript/{job}", data=data,
                  headers={"Content-Type": "application/json"})
assert code == 409, (code, resp)
assert resp.get("fallback") is True, resp
print("4. re-adoption refused with 409 + fallback flag OK")

# ── 5. audio hash mismatch refused ─────────────────────────────────────
job2, _ = make_job("awaiting_audio")   # audio-first job (no images)
body, hdr = multipart({"client_transcribe": "1"}, "audio", "voice.wav",
                      audio_bytes)
code, resp = http(f"/sync/audio/{job2}", data=body, headers=hdr)
assert code == 200 and resp.get("client_transcribe") is True, (code, resp)
bad = dict(payload, audio_sha256="0" * 64)
code, resp = http(f"/sync/client-transcript/{job2}",
                  data=json.dumps(bad).encode(),
                  headers={"Content-Type": "application/json"})
assert code == 400 and "hash" in resp.get("error", "").lower(), (code, resp)
assert read_manifest(job2)["status"] == "transcribing"  # untouched
print("5. hash mismatch refused (400), manifest untouched OK")

# ── 6. audio-first adoption (no images branch) ─────────────────────────
code, resp = http(f"/sync/client-transcript/{job2}", data=data,
                  headers={"Content-Type": "application/json"})
assert code == 200 and resp["status"] == "ready", (code, resp)
m = read_manifest(job2)
assert m["segments"] == [] and m["matched"] is False
assert m["segments_with_text"], "segments_with_text must be stored"
print("6. audio-first adoption: segments_with_text stored, timeline empty OK")

# ── 7. explicit fallback route guards ──────────────────────────────────
code, resp = http("/sync/transcribe/__nosuchjob__", data=b"", method="POST")
assert code == 404, (code, resp)
code, resp = http(f"/sync/transcribe/{job}", data=b"", method="POST")
assert code == 200 and resp.get("status") == "ready", (code, resp)
print("7. /sync/transcribe: 404 unknown, no-op on finished job OK")

# ── 8. watchdog: silent client run -> server transcription ─────────────
job3, job_dir3 = make_job("awaiting_audio")
body, hdr = multipart({"client_transcribe": "1"}, "audio", "voice.wav",
                      audio_bytes)
code, resp = http(f"/sync/audio/{job3}", data=body, headers=hdr)
assert resp.get("client_transcribe") is True, (code, resp)
m = read_manifest(job3)
m["client_transcribe"]["started_at"] = time.time() - 400  # go silent
(job_dir3 / "manifest.json").write_text(json.dumps(m))
code, resp = http(f"/sync/status/{job3}")
m = read_manifest(job3)
assert m["client_transcribe"]["state"] == "server_fallback", m
assert resp["status"] in ("processing", "error", "ready"), (resp["status"],)
print(f"8. watchdog fired: status={resp['status']}, "
      "marker=server_fallback OK")

# cleanup test jobs
import shutil
for j in (job, job2, job3):
    shutil.rmtree(UP / j, ignore_errors=True)

print("ALL FUNCTIONAL TESTS PASSED")

