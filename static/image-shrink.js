/**
 * image-shrink.js — client-side downscale for sync-job image uploads.
 *
 * The renderer outputs 1920x1080, so any source image longer edge is
 * pure upload weight. Downsampling in the browser BEFORE the upload
 * cuts transfer size 5-20x for phone photos and makes the server-side
 * Pillow pre-shrink a no-op. transparency is flattened to black, which
 * matches the renderer's letterbox color and the server pre-shrink.
 *
 * Usage: const prepared = await window.shrinkImagesForUpload(files);
 * Falls back to the original files when the browser can't decode one
 * (e.g. HEIC) — the server still validates types.
 */
window.shrinkImagesForUpload = async function shrinkImagesForUpload(files) {
  const MAX_EDGE = 1920;
  const JPEG_QUALITY = 0.9;
  // Already-light files are passed through untouched: re-encoding a
  // small image only wastes time and quality.
  const PASS_THROUGH_BYTES = 2 * 1024 * 1024;

  if (!window.createImageBitmap || !files || !files.length) return files;

  const out = [];
  let shrunk = 0;
  for (const file of files) {
    // Animated GIFs must not go through canvas (animation is lost).
    if (file.type === "image/gif") { out.push(file); continue; }
    let bmp = null;
    try {
      bmp = await createImageBitmap(file);
      const long = Math.max(bmp.width, bmp.height);
      if (long <= MAX_EDGE && file.size <= PASS_THROUGH_BYTES) {
        out.push(file);
        continue;
      }
      const scale = long > MAX_EDGE ? MAX_EDGE / long : 1;
      const w = Math.max(1, Math.round(bmp.width * scale));
      const h = Math.max(1, Math.round(bmp.height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext("2d");
      if (!ctx) { out.push(file); continue; }
      // Black fill flattens PNG transparency to the letterbox color.
      ctx.fillStyle = "#000000";
      ctx.fillRect(0, 0, w, h);
      ctx.drawImage(bmp, 0, 0, w, h);
      const blob = await new Promise(
        (resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY)
      );
      if (!blob || blob.size >= file.size) { out.push(file); continue; }
      const name = file.name.replace(/\.[^.]+$/, "") + ".jpg";
      out.push(new File([blob], name, { type: "image/jpeg" }));
      shrunk++;
    } catch (err) {
      // Undecodable in the browser (HEIC etc.): let the server decide.
      out.push(file);
    } finally {
      if (bmp && bmp.close) bmp.close();
    }
  }
  if (shrunk) {
    console.log(`image-shrink: downscaled ${shrunk}/${files.length} image(s) to ${MAX_EDGE}px before upload`);
  }
  return out;
};
