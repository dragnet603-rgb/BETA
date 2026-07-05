document.addEventListener("DOMContentLoaded", () => {
  const video = document.getElementById("videoPreview");
  const canvas = document.getElementById("videoCanvas");
  const ctx = canvas.getContext("2d");

  // Match canvas size to video once loaded
  video.addEventListener("loadedmetadata", () => {
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
  });

  function drawFrame() {
    if (!video.paused && !video.ended) {
      // Draw current video frame
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

      // Hardcoded overlay text
      ctx.font = "40px Arial";
      ctx.fillStyle = "red";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";

      ctx.fillText(
        "🔥 Hardcoded Test Text 🔥",
        canvas.width / 2,
        canvas.height / 2
      );
    }

    requestAnimationFrame(drawFrame);
  }

  requestAnimationFrame(drawFrame);
});
