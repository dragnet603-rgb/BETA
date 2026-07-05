const MAX_RETRIES = 3;

document.addEventListener("DOMContentLoaded", () => {
  const addBtn = document.getElementById("addP");
  const fileInput = document.getElementById("videoInput");

  addBtn.addEventListener("click", () => fileInput.click());

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    try {
      await uploadFile(file);
    } catch (err) {
      alert(`Upload failed: ${err.message}`);
    }
  });
});

function uploadWithProgress(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const fd = new FormData();
    fd.append("video", file);

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch (e) {
          reject(new Error("Invalid server response"));
        }
      } else {
        reject(new Error(`Upload failed (${xhr.status})`));
      }
    });

    xhr.addEventListener("error", () =>
      reject(new Error("Network error during upload")),
    );
    xhr.addEventListener("abort", () => reject(new Error("Upload aborted")));

    xhr.open("POST", "/upload");
    xhr.send(fd);
  });
}

async function uploadFile(file) {
  const overlay = document.getElementById("uploadOverlay");
  const bar = document.getElementById("progressBar");
  const pct = document.getElementById("uploadPercent");
  const label = document.getElementById("uploadLabel");

  if (overlay) overlay.style.display = "flex";
  if (label) label.textContent = "Uploading…";

  const updateProgress = (percent) => {
    if (bar) bar.style.width = `${percent}%`;
    if (pct) pct.textContent = `${percent}%`;
  };

  let lastError;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      const data = await uploadWithProgress(file, updateProgress);
      if (!data.filename) throw new Error("No filename returned");

      if (label) label.textContent = "Processing…";
      updateProgress(100);
      await new Promise((r) => setTimeout(r, 300));

      window.location.href = `/canvas/${data.filename}`;
      return;
    } catch (err) {
      lastError = err;
      console.warn(`Upload attempt ${attempt + 1} failed:`, err);
      if (attempt < MAX_RETRIES - 1) {
        if (label)
          label.textContent = `Retrying (${attempt + 2}/${MAX_RETRIES})…`;
        await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
      }
    }
  }

  if (overlay) overlay.style.display = "none";
  throw lastError;
}
