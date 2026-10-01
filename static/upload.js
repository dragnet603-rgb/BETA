const MAX_RETRIES = 3;

// The + button takes images AND/OR a voiceover in one go: the
// server stores both, then transcribes the audio in the background. An
// audio-only upload is valid — the creator follows the beat plan, makes
// images, and adds them on the sync page. A voiceover can also still be
// added later on the sync page (audio track in the timeline),
// CapCut-style; at least one of images/audio is required.
// Mirrors SYNC_AUDIO_EXTENSIONS in app.py; the extension list is the
// fallback for files the browser reports with an empty MIME type.
const AUDIO_EXTENSIONS = ["mp3", "wav", "m4a", "aac", "ogg", "flac", "webm"];

// Statuses that must NOT be retried: the server has already decided and a
// byte-identical retry can never succeed (bad type, too many, auth, …).
// Everything else (network drop, 5xx, timeout) keeps the old 3-try loop.
const NON_RETRYABLE_STATUSES = new Set([400, 401, 402, 403, 404, 405, 409, 410, 413, 414, 415, 422]);

// Accepted by SYNC_IMAGE_EXTENSIONS in app.py. Anything else (notably
// iPhone HEIC/HEIF and AVIF) passes the browser's `image/*` MIME filter
// but is rejected by the server — catch it here with a clear message
// instead of burning 3 upload attempts.
const SUPPORTED_IMAGE_EXTS = ["jpg", "jpeg", "png", "webp", "gif", "bmp"];

const extOf = (name) => {
  const parts = String(name || "").toLowerCase().split(".");
  return parts.length > 1 ? parts.pop() : "";
};

const isAudioFile = (file) => file.type.startsWith("audio/")
  || (file.type === "" && AUDIO_EXTENSIONS.includes(extOf(file.name)));

// Carries the HTTP status + optional login URL through the retry loop so
// non-retryable failures (auth, bad type) fail fast instead of retrying.
class UploadError extends Error {
  constructor(message, opts = {}) {
    super(message);
    this.name = "UploadError";
    this.status = opts.status || 0;
    this.loginUrl = opts.loginUrl || null;
    this.retryable = !NON_RETRYABLE_STATUSES.has(this.status);
  }
}

function friendlyUploadMessage(err) {
  if (err && err.status === 401) {
    return "Your session expired - please sign in again, then retry your upload.";
  }
  return (err && err.message) || "Upload failed.";
}

// Best-effort silent session repair: when Firebase still has a user, its
// ID token re-mints the 31-day Flask cookie (same idea as auth.js
// refreshServerSession). Resolves true when the re-mint POST succeeded.
async function tryRefreshSession() {
  try {
    const kit = window.__authKit;
    const auth = kit && kit._app && kit.getAuth ? kit.getAuth(kit._app) : null;
    const user = auth && auth.currentUser;
    if (!user || typeof user.getIdToken !== "function") return false;
    const idToken = await user.getIdToken();
    if (!idToken) return false;
    const res = await fetch("/api/auth/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idToken }),
    });
    return res.ok;
  } catch (_) {
    return false;
  }
}

// Server 401 bodies carry login_url="/login" (no next= for XHR calls); send
// the user through sign-in and back to the home page to re-pick files.
function loginRedirectUrl(loginUrl) {
  const base = loginUrl || "/login";
  return base.includes("next=") ? base : "/login?next=%2F";
}

document.addEventListener("DOMContentLoaded", () => {
  const addBtn     = document.getElementById("addP");
  const imageInput = document.getElementById("imageInput");
  if (!addBtn || !imageInput) return;

  addBtn.addEventListener("click", () => imageInput.click());

  imageInput.addEventListener("change", async () => {
    const picked = Array.from(imageInput.files || []);
    // Reset first so picking the same files again still fires a change.
    imageInput.value = "";
    const files = picked.filter((f) => f.type.startsWith("image/"));
    // One voiceover per job: the first audio file wins. Everything else in
    // the selection is ignored, exactly like before.
    const audio = picked.find(isAudioFile) || null;

    if (!files.length && !audio) {
      alert("Add images or a voiceover to start.");
      return;
    }
    // HEIC/HEIF/AVIF pass the browser `image/*` filter but the server only
    // accepts SYNC_IMAGE_EXTENSIONS — fail here with an actionable message
    // instead of burning 3 upload attempts against a guaranteed 400.
    const badType = files.find((f) => !SUPPORTED_IMAGE_EXTS.includes(extOf(f.name)));
    if (badType) {
      const rawExt = extOf(badType.name);
      window.posthog?.capture("sync_upload_failed", {
        error: `Unsupported image type: ${badType.name}`,
        status: 0,
        preflight: "unsupported_image_type",
        image_count: files.length,
        has_audio: !!audio,
      });
      alert(
        rawExt
          ? `${rawExt.toUpperCase()} photos aren't supported yet — on iPhone, go to Settings > Camera > Formats > Most Compatible (JPEG), ` +
            `or convert to JPG/PNG and try again.`
          : `One of these images has no file extension, so it can't be uploaded — rename it to end in .jpg or .png and try again.`
      );
      return;
    }
    window.posthog?.capture("sync_upload_started", {
      image_count: files.length,
      has_audio: !!audio,
    });
    const prepLabel = document.getElementById("uploadLabel");
    if (prepLabel) {
      prepLabel.textContent = files.length
        ? "Preparing images…"
        : "Preparing voiceover…";
    }
    // Downscale in the browser first: 1920px is the render target, so
    // full-res phone photos are pure upload weight (5-20x smaller).
    // Audio-only uploads skip shrinking entirely.
    const prepared = files.length
      ? await window.shrinkImagesForUpload(files, (done, total) => {
          // Preparation used to be a silent multi-second wait; the pool
          // makes it short, and counting down makes it visible either way.
          if (prepLabel) {
            prepLabel.textContent = `Preparing images… ${done}/${total}`;
          }
        })
      : [];
    uploadSyncJob(prepared, audio).catch((err) => {
      const status = (err && err.status) || 0;
      window.posthog?.capture("sync_upload_failed", {
        error: (err && err.message) || "Upload failed",
        status,
        image_count: prepared.length,
        has_audio: !!audio,
      });
      if (status === 401) {
        // Expired/revoked session: take the user back through sign-in with
        // ?next=/ so they land on the home page and can re-pick their
        // files. The alert + navigation replace the old silent overlay
        // hide, which looked like a reload back to the + button.
        alert(friendlyUploadMessage(err));
        window.location.href = loginRedirectUrl(err && err.loginUrl);
        return;
      }
      alert(`Upload failed: ${friendlyUploadMessage(err)}`);
    });
  });

  async function uploadSyncJob(files, audio) {
    const overlay = document.getElementById("uploadOverlay");
    const bar     = document.getElementById("progressBar");
    const pct     = document.getElementById("uploadPercent");
    const label   = document.getElementById("uploadLabel");

    if (overlay) overlay.style.display = "flex";
    if (label) {
      label.textContent = files.length && audio
        ? "Uploading images and voiceover…"
        : audio
          ? "Uploading voiceover…"
          : "Uploading images…";
    }

    const updateProgress = (percent) => {
      if (bar) bar.style.width = `${percent}%`;
      if (pct) pct.textContent = `${percent}%`;
    };

    let lastError;
    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      try {
        const data = await uploadSyncWithProgress(files, audio, updateProgress);
        if (!data.job_id) throw new UploadError("No job id returned", { status: 0 });

        if (label) label.textContent = "Opening editor…";
        updateProgress(100);
        await new Promise((r) => setTimeout(r, 300));

        window.location.href = `/sync/${data.job_id}`;
        return;
      } catch (err) {
        // Non-retryable (401/400/…): the server already decided, so fail
        // fast — EXCEPT a 401 gets one silent session re-mint first, since
        // Firebase often still holds a valid user while the Flask cookie
        // has expired.
        if (err && err.status === 401 && !err._refreshed) {
          err._refreshed = true;
          if (label) label.textContent = "Refreshing session…";
          if (await tryRefreshSession()) continue;
        }
        lastError = err;
        console.warn(`Upload attempt ${attempt + 1} failed:`, err);
        const retryable = !err || err.retryable !== false;
        if (!retryable) break;
        if (attempt < MAX_RETRIES - 1) {
          if (label) label.textContent = `Retrying (${attempt + 2}/${MAX_RETRIES})…`;
          await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
        }
      }
    }

    if (overlay) overlay.style.display = "none";
    throw lastError;
  }

  function uploadSyncWithProgress(files, audio, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const fd  = new FormData();
      files.forEach((f) => fd.append("images", f));
      // Optional voiceover: the same field the sync page's audio upload
      // uses, so /sync/upload stores it and starts transcription right
      // away. Retries re-send the same File object, so this stays safe.
      if (audio) fd.append("audio", audio);

      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      });
      xhr.addEventListener("load", () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText));
          } catch (e) {
            reject(new UploadError("Invalid server response", { status: xhr.status }));
          }
        } else {
          let msg = `Upload failed (${xhr.status})`;
          let loginUrl = null;
          try {
            const body = JSON.parse(xhr.responseText);
            msg = body.error || msg;
            loginUrl = body.login_url || null;
          } catch (_) {}
          reject(new UploadError(msg, { status: xhr.status, loginUrl }));
        }
      });
      xhr.addEventListener("error", () => reject(new UploadError("Network error during upload", { status: 0 })));
      xhr.addEventListener("abort", () => reject(new UploadError("Upload aborted", { status: 0 })));

      xhr.open("POST", "/sync/upload");
      xhr.send(fd);
    });
  }
});

