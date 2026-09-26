/**
 * image-shrink.js — client-side downscale for sync-job image uploads.
 *
 * The renderer outputs 1920x1080, so any source image longer edge is
 * pure upload weight. Downsampling in the browser BEFORE the upload
 * cuts transfer size 5-20x for phone photos and makes the server-side
 * Pillow pre-shrink a no-op. transparency is flattened to black, which
 * matches the renderer's letterbox color and the server pre-shrink.
 *
 * Preparation runs through a small worker POOL: decoding is the slow
 * part and it parallelizes well, while a sequential loop left users
 * staring at "Preparing images…" for seconds on a 20-photo selection.
 *
 * Every returned File also carries `previewUrl` - a local blob URL of a
 * ~320px copy - so the timeline can paint the pictures IMMEDIATELY,
 * before the upload finishes. Callers that ignore it are unaffected.
 *
 * Each returned File also carries `previewW` / `previewH` (the source
 * picture's real pixel size, free from the decode above) so the sync page
 * can shape its preview canvas to a non-16:9 upload before the upload has
 * even finished (see applyPreviewRatio in sync.js).
 *
 * Usage: const prepared = await window.shrinkImagesForUpload(files, onProgress);
 * Falls back to the original files when the browser can't decode one
 * (e.g. HEIC) — the server still validates types.
 */
window.shrinkImagesForUpload = async function shrinkImagesForUpload(
  files, onProgress
) {
  const MAX_EDGE = 1920;
  const JPEG_QUALITY = 0.9;
  const PREVIEW_EDGE = 320;
  const PREVIEW_QUALITY = 0.72;
  // Already-light files are passed through untouched: re-encoding a
  // small image only wastes time and quality.
  const PASS_THROUGH_BYTES = 2 * 1024 * 1024;

  if (!window.createImageBitmap || !files || !files.length) return files;

  // How many images to decode at once. Four 12MP bitmaps are ~190MB of
  // pixels, which is fine on a laptop and rude on a 2GB phone, so a
  // low-memory device drops to two.
  const poolSize = () => {
    const cores = navigator.hardwareConcurrency || 4;
    const mem = navigator.deviceMemory || 8;
    return Math.max(2, Math.min(4, cores, mem < 4 ? 2 : 4));
  };

  const out = new Array(files.length);
  let next = 0;        // next index a worker should claim
  let done = 0;
  let shrunk = 0;

  /** Point a File at a blob URL the browser can paint right now. */
  function withPreview(file, source) {
    try {
      file.previewUrl = URL.createObjectURL(source);
    } catch (e) { /* preview is a nicety, never required */ }
    return file;
  }

  function drawToBlob(bmp, w, h, quality) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, w);
    canvas.height = Math.max(1, h);
    const ctx = canvas.getContext("2d");
    if (!ctx) return Promise.resolve(null);
    // Black fill flattens PNG transparency to the letterbox color.
    ctx.fillStyle = "#000000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
    return new Promise((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", quality)
    );
  }

  async function prepare(index) {
    const file = files[index];
    // Animated GIFs must not go through canvas (animation is lost).
    if (file.type === "image/gif") {
      out[index] = withPreview(file, file);
      return;
    }
    let bmp = null;
    try {
      bmp = await createImageBitmap(file);
      // Real pixel size of the picture: the sync preview uses it to give a
      // non-16:9 image a canvas of its own shape. Attached to the ORIGINAL
      // File here so every branch below carries it, including the
      // re-encoded one (which is a brand new File - see drawToBlob call).
      file.previewW = bmp.width;
      file.previewH = bmp.height;
      const long = Math.max(bmp.width, bmp.height);
      if (long <= MAX_EDGE && file.size <= PASS_THROUGH_BYTES) {
        // Nothing to gain by re-encoding: the original file IS the
        // preview (free) and uploads untouched.
        out[index] = withPreview(file, file);
        return;
      }
      const scale = long > MAX_EDGE ? MAX_EDGE / long : 1;
      const blob = await drawToBlob(
        bmp,
        Math.round(bmp.width * scale),
        Math.round(bmp.height * scale),
        JPEG_QUALITY
      );
      if (blob && blob.size < file.size) {
        const name = file.name.replace(/\.[^.]+$/, "") + ".jpg";
        const shrunkFile = new File([blob], name, { type: "image/jpeg" });
        // A re-encoded picture is a NEW File object: re-attach the real
        // dimensions so the canvas shape survives the shrink.
        shrunkFile.previewW = bmp.width;
        shrunkFile.previewH = bmp.height;
        out[index] = shrunkFile;
        shrunk++;
      } else {
        out[index] = file;
      }
      // The preview comes from the ORIGINAL bitmap, not the lossy
      // re-encode above: a 320px tile only has to be recognisable.
      const pScale = Math.min(
        1, PREVIEW_EDGE / Math.max(bmp.width, bmp.height)
      );
      const pv = await drawToBlob(
        bmp,
        Math.round(bmp.width * pScale),
        Math.round(bmp.height * pScale),
        PREVIEW_QUALITY
      );
      if (pv) withPreview(out[index], pv);
    } catch (err) {
      // Undecodable in the browser (HEIC etc.): let the server decide,
      // and skip the optimistic preview (it would not paint either).
      out[index] = file;
    } finally {
      if (bmp && bmp.close) bmp.close();
      done++;
      if (onProgress) onProgress(done, files.length);
    }
  }

  // Pool: N workers each claim the next index until the queue is empty.
  const workers = Math.min(poolSize(), files.length);
  await Promise.all(
    Array.from({ length: workers }, async () => {
      for (;;) {
        const i = next++;
        if (i >= files.length) return;
        await prepare(i);
      }
    })
  );

  if (shrunk) {
    console.log(`image-shrink: downscaled ${shrunk}/${files.length} image(s) to ${MAX_EDGE}px before upload`);
  }
  return out;
};
