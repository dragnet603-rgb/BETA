document.addEventListener("DOMContentLoaded", () => {
  const addBtn = document.getElementById("addP");
  const fileInput = document.getElementById("videoFile");
  const uploadForm = document.getElementById("uploadForm");

  addBtn.addEventListener("click", () => fileInput.click());

  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) return alert("Please select a file first");

    await uploadFileInChunks(file);

    // Redirect to canvas page
    window.location.href = `/canvas/${encodeURIComponent(file.name)}`;
  });
});

async function uploadFileInChunks(file) {
  const chunkSize = 2 * 1024 * 1024; // 2 MB
  const totalChunks = Math.ceil(file.size / chunkSize);

  document.getElementById("uploadProgress").style.display = "block";

  for (let i = 0; i < totalChunks; i++) {
    const start = i * chunkSize;
    const end = Math.min(file.size, start + chunkSize);
    const chunk = file.slice(start, end);

    const formData = new FormData();
    formData.append("chunk", chunk);
    formData.append("filename", file.name);
    formData.append("chunk_index", i);
    formData.append("total_chunks", totalChunks);

    await fetch("/upload-chunk", { method: "POST", body: formData });

    // Update bar
    let percent = Math.round(((i + 1) / totalChunks) * 100);
    document.getElementById("uploadBar").style.width = percent + "%";
    document.getElementById(
      "uploadStatus"
    ).innerText = `Uploading... ${percent}%`;
  }

  document.getElementById("uploadStatus").innerText = "Finalizing upload...";
  await fetch(`/merge-chunks?filename=${encodeURIComponent(file.name)}`);
}
