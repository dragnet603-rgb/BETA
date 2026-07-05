document.addEventListener("DOMContentLoaded", () => {
  const tap = document.getElementById("addP");
  const videoInput = document.getElementById("videoInput");

  tap.addEventListener("click", () => {
    videoInput.click();
  });

  videoInput.addEventListener("change", () => {
    if (videoInput.files.length > 0) {
      const file = videoInput.files[0];
      uploadFileInChunks(file);
    }
  });

  async function uploadFileInChunks(file) {
    const chunkSize = 2 * 1024 * 1024; // 5 MB
    const totalChunks = Math.ceil(file.size / chunkSize);
    const filename = encodeURIComponent(file.name);

    for (let i = 0; i < totalChunks; i++) {
      const start = i * chunkSize;
      const end = Math.min(file.size, start + chunkSize);
      const chunk = file.slice(start, end);

      const formData = new FormData();
      formData.append("chunk", chunk);
      formData.append("filename", filename);
      formData.append("chunk_index", i);
      formData.append("total_chunks", totalChunks);

      try {
        await fetch("/upload-chunk", {
          method: "POST",
          body: formData,
        });

        console.log(`✅ Uploaded chunk ${i + 1} of ${totalChunks}`);
      } catch (err) {
        console.error("❌ Chunk upload failed:", err);
        alert("Upload failed, please try again.");
        return;
      }
    }

    // Tell server we're done
    const res = await fetch(`/merge-chunks?filename=${filename}`);
    if (res.ok) {
      const data = await res.json();
      console.log("✅ File merge complete:", data);

      // Redirect to canvas page
      window.location.href = `/canvas/${filename}`;
    } else {
      console.error("❌ Merge failed");
    }
  }
});
