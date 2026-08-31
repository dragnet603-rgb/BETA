const tap = document.getElementById("addP");
const videoInput = document.getElementById("videoInput");
const uploadForm = document.getElementById("uploadForm");

tap.addEventListener("click", () => {
  videoInput.click();
});

videoInput.addEventListener("change", () => {
  if (videoInput.files.length > 0) {
    uploadForm.submit(); // send file to Flask
  }
});
