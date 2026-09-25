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

const extOf = (name) => {
  const parts = String(name || "").toLowerCase().split(".");
  return parts.length > 1 ? parts.pop() : "";
};

const isAudioFile = (file) => file.type.startsWith("audio/")
  || (file.type === "" && AUDIO_EXTENSIONS.includes(extOf(file.name)));

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
      window.posthog?.capture("sync_upload_failed", { error: err.message });
      alert(`Upload failed: ${err.message}`);
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
        if (!data.job_id) throw new Error("No job id returned");

        if (label) label.textContent = "Opening editor…";
        updateProgress(100);
        await new Promise((r) => setTimeout(r, 300));

        window.location.href = `/sync/${data.job_id}`;
        return;
      } catch (err) {
        lastError = err;
        console.warn(`Upload attempt ${attempt + 1} failed:`, err);
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
            reject(new Error("Invalid server response"));
          }
        } else {
          let msg = `Upload failed (${xhr.status})`;
          try { msg = JSON.parse(xhr.responseText).error || msg; } catch (_) {}
          reject(new Error(msg));
        }
      });
      xhr.addEventListener("error", () => reject(new Error("Network error during upload")));
      xhr.addEventListener("abort", () => reject(new Error("Upload aborted")));

      xhr.open("POST", "/sync/upload");
      xhr.send(fd);
    });
  }
});

