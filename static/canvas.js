document.addEventListener("DOMContentLoaded", () => {
  const video = document.getElementById("videoPreview");
  const form = document.getElementById("promptForm");
  const container = document.querySelector(".canvas");
  const expBtn = document.getElementById("Export");
  const statusBox = document.getElementById("statusBox");
  const statusText = document.getElementById("statusText");
  const uploadForm = document.getElementById("uploadForm");
  const videoFileInput = document.getElementById("videoFile");
  const playPauseBtn = document.getElementById("playPauseBtn");
  const konvaContainer = document.getElementById("konvaStage");
  const topBannerBtn = document.getElementById("topBannerBtn");
  konvaContainer.style.display = "none";

  if (!video || !container) return;

function showEditingOverlay() {
  const overlay = document.getElementById("editOverlay");
  if (!overlay) return;
  container.classList.add("processing");
  overlay.classList.add("active");
}

function hideEditingOverlay() {
  const overlay = document.getElementById("editOverlay");
  container.classList.remove("processing");
  overlay?.classList.remove("active");
}

  // Banner should be addable any time — cropped or not. It no longer waits
  // for a crop to be applied before showing/working.

  const stage = new Konva.Stage({
    container: "konvaStage",
    width: container.clientWidth,
    height: container.clientHeight,
  });

  const layer = new Konva.Layer();
  stage.add(layer);

  let bannerLayer = null;
  function initBannerLayer() {
    if (bannerLayer) return;
    bannerLayer = new Konva.Layer();
    stage.add(bannerLayer);
  }

  const cropRectangle = new Konva.Rect({
    x: 50,
    y: 50,
    width: 150,
    height: 250,
    stroke: "white",
    strokeWidth: 2,
    draggable: true,
    dragBoundFunc(pos) {
      return {
        x: Math.max(0, Math.min(pos.x, stage.width() - this.width())),
        y: Math.max(0, Math.min(pos.y, stage.height() - this.height())),
      };
    },
  });
  layer.add(cropRectangle);

  const handle = new Konva.Circle({
    x: cropRectangle.x() + cropRectangle.width(),
    y: cropRectangle.y() + cropRectangle.height(),
    radius: 15,
    fill: "white",
    draggable: true,
  });
  layer.add(handle);

  const overlay = new Konva.Shape({
    listening: false,
    sceneFunc(ctx) {
      ctx.beginPath();
      ctx.rect(0, 0, stage.width(), stage.height());
      ctx.rect(
        cropRectangle.x(),
        cropRectangle.y(),
        cropRectangle.width(),
        cropRectangle.height(),
      );
      ctx.fillStyle = "rgba(0,0,0,0.6)";
      ctx.fill("evenodd");
    },
  });
  layer.add(overlay);
  overlay.moveToBottom();

  // Crop UI (rectangle/handle/overlay, and later the post-apply resize box)
  // all live on `layer`. Keep it hidden until crop mode is actually entered
  // so it doesn't flash into view just because the banner turned the stage on.
  layer.visible(false);

  function redrawCropUI() {
    layer.batchDraw();
  }

  handle.on("dragmove", () => {
    let newWidth = Math.max(50, handle.x() - cropRectangle.x());
    if (currentAspectRatio) {
      cropRectangle.width(newWidth);
      cropRectangle.height(newWidth / currentAspectRatio);
    } else {
      cropRectangle.width(newWidth);
      cropRectangle.height(Math.max(50, handle.y() - cropRectangle.y()));
    }
    handle.position({
      x: cropRectangle.x() + cropRectangle.width(),
      y: cropRectangle.y() + cropRectangle.height(),
    });
    redrawCropUI();
  });

  cropRectangle.on("dragmove", () => {
    handle.position({
      x: cropRectangle.x() + cropRectangle.width(),
      y: cropRectangle.y() + cropRectangle.height(),
    });
    redrawCropUI();
  });

  layer.draw();

  function getVideoRenderBox() {
    const vr = video.getBoundingClientRect();
    const cr = container.getBoundingClientRect();
    return {
      left: vr.left - cr.left,
      top: vr.top - cr.top,
      w: vr.width,
      h: vr.height,
    };
  }

  // Returns the box the <video> element occupies in the container (its CSS
  // box), NOT the actual rendered video pixels. Use getIntrinsicVideoRect()
  // below when you need the real visible video content (handles
  // letterboxing/pillarboxing for any aspect ratio — 16:9, 1:1, 4:3, 9:16…).
  function getVideoBoundsOnCanvas() {
    const vr = video.getBoundingClientRect();
    const cr = container.getBoundingClientRect();
    return {
      x: vr.left - cr.left,
      y: vr.top - cr.top,
      w: vr.width,
      h: vr.height,
    };
  }

  // The <video> element's CSS box and the actual visible video frame are
  // only the same when the element's aspect ratio matches the video's
  // intrinsic aspect ratio. With object-fit: contain (the default we rely
  // on), any mismatch — a 1:1 or 4:3 clip inside a 16:9-shaped box, etc. —
  // produces letterboxing/pillarboxing. This computes the real content
  // rect so banners/crop UI snap to the footage itself, not the empty bars.
  function getIntrinsicVideoRect() {
    const vr = video.getBoundingClientRect();
    const cr = container.getBoundingClientRect();
    const boxLeft = vr.left - cr.left;
    const boxTop = vr.top - cr.top;
    const boxW = vr.width;
    const boxH = vr.height;

    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!vw || !vh || !boxW || !boxH) {
      return { x: boxLeft, y: boxTop, w: boxW, h: boxH };
    }

    const videoRatio = vw / vh;
    const boxRatio = boxW / boxH;

    let w, h, x, y;
    if (videoRatio > boxRatio) {
      // Video is relatively wider than the box → letterboxed top/bottom
      w = boxW;
      h = boxW / videoRatio;
      x = boxLeft;
      y = boxTop + (boxH - h) / 2;
    } else {
      // Video is relatively taller/narrower than the box → pillarboxed sides
      h = boxH;
      w = boxH * videoRatio;
      x = boxLeft + (boxW - w) / 2;
      y = boxTop;
    }

    return { x, y, w, h };
  }

  // NOTE: getCropData() previously read against getVideoRenderBox() (the
  // <video> element's outer CSS box). That box can include empty
  // letterbox/pillarbox space when the source's intrinsic aspect ratio
  // doesn't match the box's shape. Crop math needs to be relative to the
  // actual visible video content, so this now uses getIntrinsicVideoRect()
  // — the same rect banner-snapping already relies on.
  function getCropData() {
    const vb = getIntrinsicVideoRect();
    return {
      nx: Math.max(0, (cropRectangle.x() - vb.x) / vb.w),
      ny: Math.max(0, (cropRectangle.y() - vb.y) / vb.h),
      nw: Math.min(1, cropRectangle.width() / vb.w),
      nh: Math.min(1, cropRectangle.height() / vb.h),
    };
  }

  let edits = [];
  let cropDisplayRect = null;
  let currentAspectRatio = null;
  let cropApplied = false;

  const cropApply = document.getElementById("cropApply");
  cropApply.addEventListener("click", () => {
    const crop = getCropData();
    console.log("APPLY CLICKED", crop);

    edits = edits.filter((e) => e.type !== "crop");
    edits.unshift({ type: "crop", ...crop });

    document.getElementById("cropControls").style.display = "none";
    layer.destroyChildren();
    konvaContainer.style.display = "none";

    cropApplied = true;

    updateCanvas();
  });

  const cropCancelBtn = document.getElementById("cropCancel");
  if (cropCancelBtn) {
    cropCancelBtn.addEventListener("click", () => {
      const hasBanners = edits.some(
        (e) => e.type === "top_banner" || e.type === "bottom_banner",
      );
      if (!hasBanners) konvaContainer.style.display = "none";
      document.getElementById("cropControls").style.display = "none";
      const hasCrop = edits.some((e) => e.type === "crop");
      if (!hasCrop) layer.visible(false);
    });
  }

  const ratio169Btn = document.getElementById("ratio169");
  const ratio11Btn = document.getElementById("ratio11");

  // NOTE: showCropBox() previously sized/positioned the crop rectangle
  // against getVideoRenderBox() (the <video> element's outer CSS box).
  // When the source video's intrinsic aspect ratio doesn't match that box
  // (letterboxing/pillarboxing from object-fit: contain), the outer box
  // includes empty space that isn't part of the actual footage. For a
  // ratio like 1:1 this often happened to still look right by coincidence,
  // but 16:9 would come out sized/positioned against the empty bars
  // instead of the real video content. Using getIntrinsicVideoRect() here
  // (same rect banner snapping already relies on) fixes that for every
  // ratio consistently.
  function showCropBox(ratio) {
    currentAspectRatio = ratio;
    const vb = getIntrinsicVideoRect();
    let boxW, boxH;
    if (ratio) {
      boxW = vb.w;
      boxH = boxW / ratio;
      if (boxH > vb.h) {
        boxH = vb.h;
        boxW = boxH * ratio;
      }
    } else {
      boxW = vb.w * 0.6;
      boxH = vb.h * 0.6;
    }
    const startX = vb.x + (vb.w - boxW) / 2;
    const startY = vb.y + (vb.h - boxH) / 2;
    cropRectangle.position({ x: startX, y: startY });
    cropRectangle.width(boxW);
    cropRectangle.height(boxH);
    handle.position({
      x: cropRectangle.x() + cropRectangle.width(),
      y: cropRectangle.y() + cropRectangle.height(),
    });
    layer.draw();
  }

  ratio169Btn?.addEventListener("click", () => showCropBox(16 / 9));
  ratio11Btn?.addEventListener("click", () => showCropBox(1));

  let _resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
      stage.width(container.clientWidth);
      stage.height(container.clientHeight);
      const cropEdit = edits.find((e) => e.type === "crop");
      if (cropEdit) {
        layer.batchDraw();
        if (bannerLayer) bannerLayer.batchDraw();
        return;
      }
      updateCanvas();
    }, 100);
  });

  function getFilename() {
    const src = (video.querySelector("source") || video).src || "";
    return src.split("/").pop().split("?")[0];
  }

  function showStatus(msg) {
    if (!statusText || !statusBox) return;
    statusText.textContent = msg;
    statusBox.style.display = "block";
  }

  function hideStatus() {
    if (!statusBox) return;
    statusBox.style.display = "none";
  }

  video.addEventListener("loadedmetadata", updateCanvas);
  if (video.readyState >= 1) updateCanvas();

  if (playPauseBtn) {
    playPauseBtn.addEventListener("click", () => {
      if (video.paused) {
        video.play();
        playPauseBtn.classList.add("active");
      } else {
        video.pause();
        playPauseBtn.classList.remove("active");
      }
    });
  }

  // ── AI prompt → edits bridge ────────────────────────────────────────────
  // /edit-json returns an array of {type, ...} items shaped by the LLM
  // system prompt (top_banner, bottom_banner, overlay_text, crop). This is
  // what actually turns that JSON into real Konva banners / crop / overlay
  // state instead of letting it sit unused after the fetch resolves.

  function aspectRatioToNumber(str) {
    if (!str || typeof str !== "string" || !str.includes(":")) return null;
    const [a, b] = str.split(":").map(Number);
    if (!a || !b) return null;
    return a / b;
  }

  // Converts an aspect_ratio string ("9:16", "1:1", etc) into the same
  // normalized nx/ny/nw/nh format the drag-crop UI produces via
  // getCropData(), centered on the source video. This keeps the AI crop
  // path compatible with /edit-video's freeform crop handler, which only
  // understands nx/ny/nw/nh — not aspect_ratio.
  function applyCropFromAI(aspectRatioStr) {
    const ratio = aspectRatioToNumber(aspectRatioStr);
    if (!ratio) {
      console.warn(
        "Could not parse aspect ratio from AI response:",
        aspectRatioStr,
      );
      return;
    }
    if (!video.videoWidth || !video.videoHeight) {
      video.addEventListener(
        "loadedmetadata",
        () => applyCropFromAI(aspectRatioStr),
        { once: true },
      );
      return;
    }

    const vw = video.videoWidth;
    const vh = video.videoHeight;
    let cropW, cropH;
    if (vw / vh > ratio) {
      cropH = vh;
      cropW = vh * ratio;
    } else {
      cropW = vw;
      cropH = vw / ratio;
    }

    const nx = (vw - cropW) / 2 / vw;
    const ny = (vh - cropH) / 2 / vh;
    const nw = cropW / vw;
    const nh = cropH / vh;

    edits = edits.filter((e) => e.type !== "crop");
    edits.unshift({ type: "crop", nx, ny, nw, nh });

    konvaContainer.style.display = "block";
    stage.width(container.clientWidth);
    stage.height(container.clientHeight);
    showCroppedPreview({ nx, ny, nw, nh });
  }

  // Dispatches every item the AI returned to the right handler. Crop is
  // applied first since banner snapping (getVisibleVideoRect) depends on
  // cropDisplayRect already being set.
  function applyAIEdits(newEdits) {
    console.log("[applyAIEdits] received from /edit-json:", newEdits);

    if (!Array.isArray(newEdits) || !newEdits.length) {
      console.warn(
        "[applyAIEdits] empty or non-array response, nothing to apply",
      );
      return;
    }

    const cropItem = newEdits.find((e) => e.type === "crop");
    const bannerItems = newEdits.filter(
      (e) => e.type === "top_banner" || e.type === "bottom_banner",
    );
    const overlayItems = newEdits.filter((e) => e.type === "overlay_text");
    const unrecognized = newEdits.filter(
      (e) =>
        e.type !== "crop" &&
        e.type !== "top_banner" &&
        e.type !== "bottom_banner" &&
        e.type !== "overlay_text",
    );
    if (unrecognized.length) {
      console.warn(
        "[applyAIEdits] unrecognized item types, ignored:",
        unrecognized,
      );
    }

    if (cropItem) {
      console.log("[applyAIEdits] applying crop:", cropItem);
      applyCropFromAI(cropItem.aspect_ratio);
    }

    bannerItems.forEach((item) => {
      console.log("[applyAIEdits] applying banner:", item);
      const group = addBanner({
        position: item.type === "bottom_banner" ? "bottom" : "top",
        text: item.text || "",
        bg_color:
          item.bg_color ||
          (item.type === "bottom_banner" ? "#000000" : "#ffffff"),
        text_color:
          item.text_color ||
          (item.type === "bottom_banner" ? "#ffffff" : "#000000"),
        font_size: item.font_size || 22,
      });
      console.log(
        "[applyAIEdits] banner group created:",
        group,
        "visible:",
        konvaContainer.style.display,
      );
    });

    if (overlayItems.length) {
      console.log("[applyAIEdits] applying overlay text:", overlayItems);
      overlayItems.forEach((item) => edits.push(item));
      updateCanvas();
    }
  }

  if (form) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.querySelector(".uploadBtn");
    if (btn) {
      btn.classList.add("loading");
      btn.disabled = true;
    }
    try {
      const promptInput = form.querySelector("input");
      if (!promptInput) return;
      const prompt = promptInput.value.trim();
      if (!prompt) return;

      showEditingOverlay(); // <-- added

      const res = await fetch(`/edit-json/${getFilename()}`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ prompt }),
      });
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const newEdits = await res.json();
      if (!Array.isArray(newEdits)) throw new Error("Invalid server response");

      applyAIEdits(newEdits);
      promptInput.value = "";
    } catch (err) {
      console.error("Prompt error:", err);
      alert(`Error: ${err.message}`);
    } finally {
      hideEditingOverlay(); // <-- added
      if (btn) {
        btn.classList.remove("loading");
        btn.disabled = false;
      }
    }
  });
}

  function renderOverlays(vb) {
    const scale = vb.w / 720;
    edits.forEach((item) => {
      if (item.type === "crop") return;
      if (item.type === "top_banner" || item.type === "bottom_banner") return;
      if (item.type === "overlay_text") {
        const fsize = (item.font_size || 28) * scale;
        const pos = item.position || "bottom";
        const topPx =
          pos === "top"
            ? vb.top + 10
            : pos === "center"
              ? vb.top + vb.h / 2 - fsize / 2
              : vb.top + vb.h - fsize * 2 - 10;
        const el = document.createElement("div");
        el.className = "generated";
        Object.assign(el.style, {
          position: "absolute",
          left: `${vb.left}px`,
          top: `${topPx}px`,
          width: `${vb.w}px`,
          textAlign: "center",
          fontSize: `${fsize}px`,
          fontWeight: "bold",
          color: item.text_color || "white",
          background: `rgba(0,0,0,${item.bg_opacity ?? 0.5})`,
          padding: "4px 8px",
          boxSizing: "border-box",
          zIndex: "10",
          fontFamily: "Arial, sans-serif",
        });
        el.innerText = item.text || "";
        container.appendChild(el);
      }
    });
  }

  // ── FIXED: showCroppedPreview ───────────────────────────────────────────
  // Previously this treated the container's raw clientWidth/clientHeight
  // (cw/ch) as if that were the native, unscaled size of the full video
  // frame. That's only true when the video exactly fills the container.
  // Any time the video's intrinsic aspect ratio doesn't match the
  // container's shape (letterboxing/pillarboxing from object-fit: contain
  // — true for most 16:9 or 1:1 crops), cw/ch no longer represent the
  // video's actual content size, so the scale factor, clip-path offsets,
  // and video positioning were all computed against the wrong box —
  // producing a crop preview that looked wrong or didn't visibly apply.
  //
  // Fix: use getIntrinsicVideoRect() (vb) — the same helper the crop-box
  // UI and banner snapping already rely on — as the reference size for
  // the video's "native" unscaled frame. cw/ch are still used, but only
  // for centering the crop-selection box on screen.
  function showCroppedPreview(crop) {
    const { nx, ny, nw, nh } = crop;
    const cw = container.clientWidth;
    const ch = container.clientHeight;

    // The video's actual native content box (accounts for letterbox/
    // pillarbox). nx/ny/nw/nh are fractions of THIS box, not of cw/ch.
    const vb = getIntrinsicVideoRect();

    let currentVideoW = vb.w;
    let currentVideoH = vb.h;
    let currentVideoLeft = cw / 2 - (nx + nw / 2) * currentVideoW;
    let currentVideoTop = ch / 2 - (ny + nh / 2) * currentVideoH;

    function applyVideoStyle() {
      video.style.position = "absolute";
      video.style.width = `${currentVideoW}px`;
      video.style.height = `${currentVideoH}px`;
      video.style.left = `${currentVideoLeft}px`;
      video.style.top = `${currentVideoTop}px`;
      // We're manually sizing the video to an arbitrary scaled box below.
      // Force "fill" so the browser doesn't re-letterbox the content
      // inside that box on top of our own math (which already accounts
      // for the real aspect ratio via vb).
      video.style.objectFit = "fill";
      video.style.clipPath = `inset(
        ${ny * 100}%
        ${(1 - nx - nw) * 100}%
        ${(1 - ny - nh) * 100}%
        ${nx * 100}%
      )`;
    }

    applyVideoStyle();
    container.style.overflow = "hidden";
    container.style.background = "black";
    cropDisplayRect = { left: 0, top: 0, w: cw, h: ch };

    konvaContainer.style.display = "block";
    stage.width(cw);
    stage.height(ch);
    layer.visible(true);
    layer.destroyChildren();
    if (bannerLayer) bannerLayer.moveToTop();

    const initW = nw * vb.w;
    const initH = nh * vb.h;
    const initX = cw / 2 - initW / 2;
    const initY = ch / 2 - initH / 2;

    function makeAnchor(x, y, cursor) {
      const size = 10;
      const anchor = new Konva.Rect({
        x: x - size / 2,
        y: y - size / 2,
        width: size,
        height: size,
        fill: "white",
        cornerRadius: 2,
        shadowColor: "black",
        shadowBlur: 6,
        shadowOpacity: 0.4,
        draggable: true,
      });
      anchor.on("mouseenter", () => {
        document.body.style.cursor = cursor;
      });
      anchor.on("mouseleave", () => {
        document.body.style.cursor = "default";
      });
      return anchor;
    }

    const box = new Konva.Rect({
      x: initX,
      y: initY,
      width: initW,
      height: initH,
      fill: "rgba(99,179,237,0.06)",
      stroke: "#63b3ed",
      strokeWidth: 1.5,
      draggable: true,
      cornerRadius: 2,
    });

    const grid = new Konva.Shape({
      listening: false,
      sceneFunc(ctx) {
        ctx.strokeStyle = "rgba(99,179,237,0.25)";
        ctx.lineWidth = 0.8;
        const x = box.x(),
          y = box.y(),
          w = box.width(),
          h = box.height();
        [1 / 3, 2 / 3].forEach((t) => {
          ctx.beginPath();
          ctx.moveTo(x + w * t, y);
          ctx.lineTo(x + w * t, y + h);
          ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(x, y + h * t);
          ctx.lineTo(x + w, y + h * t);
          ctx.stroke();
        });
      },
    });

    const glow = new Konva.Rect({
      x: initX - 1,
      y: initY - 1,
      width: initW + 2,
      height: initH + 2,
      stroke: "rgba(99,179,237,0.3)",
      strokeWidth: 4,
      listening: false,
      cornerRadius: 3,
    });

    const anchors = {
      nw: makeAnchor(initX, initY, "nw-resize"),
      ne: makeAnchor(initX + initW, initY, "ne-resize"),
      sw: makeAnchor(initX, initY + initH, "sw-resize"),
      se: makeAnchor(initX + initW, initY + initH, "se-resize"),
    };

    layer.add(glow);
    layer.add(box);
    layer.add(grid);
    Object.values(anchors).forEach((a) => layer.add(a));

    function syncAll() {
      const x = box.x(),
        y = box.y(),
        w = box.width(),
        h = box.height();
      glow.x(x - 1);
      glow.y(y - 1);
      glow.width(w + 2);
      glow.height(h + 2);
      anchors.nw.position({ x: x - 5, y: y - 5 });
      anchors.ne.position({ x: x + w - 5, y: y - 5 });
      anchors.sw.position({ x: x - 5, y: y + h - 5 });
      anchors.se.position({ x: x + w - 5, y: y + h - 5 });
      layer.batchDraw();
    }

    function syncVideo() {
      const bx = box.x(),
        by = box.y(),
        bw = box.width(),
        bh = box.height();
      cropDisplayRect = {
        left: bx,
        top: by,
        w: bw,
        h: bh,
      };

      const scaleX = bw / (nw * vb.w);
      const scaleY = bh / (nh * vb.h);
      currentVideoW = vb.w * scaleX;
      currentVideoH = vb.h * scaleY;
      const cropCX = (nx + nw / 2) * currentVideoW;
      const cropCY = (ny + nh / 2) * currentVideoH;
      currentVideoLeft = bx + bw / 2 - cropCX;
      currentVideoTop = by + bh / 2 - cropCY;
      applyVideoStyle();
    }

    box.on("dragmove", () => {
      syncAll();
      syncVideo();
    });
    box.on("mouseenter", () => {
      document.body.style.cursor = "move";
    });
    box.on("mouseleave", () => {
      document.body.style.cursor = "default";
    });

    function setupCornerDrag(anchor, getNewBox) {
      anchor.on("dragmove", () => {
        const ap = anchor.position();
        const result = getNewBox(ap);
        if (result.w < 40 || result.h < 40) return;
        box.x(result.x);
        box.y(result.y);
        box.width(result.w);
        box.height(result.h);
        syncAll();
        syncVideo();
      });
    }

    setupCornerDrag(anchors.se, (ap) => ({
      x: box.x(),
      y: box.y(),
      w: ap.x + 5 - box.x(),
      h: ap.y + 5 - box.y(),
    }));
    setupCornerDrag(anchors.sw, (ap) => ({
      x: ap.x - 5,
      y: box.y(),
      w: box.x() + box.width() - (ap.x - 5),
      h: ap.y + 5 - box.y(),
    }));
    setupCornerDrag(anchors.ne, (ap) => ({
      x: box.x(),
      y: ap.y - 5,
      w: ap.x + 5 - box.x(),
      h: box.y() + box.height() - (ap.y - 5),
    }));
    setupCornerDrag(anchors.nw, (ap) => ({
      x: ap.x - 5,
      y: ap.y - 5,
      w: box.x() + box.width() - (ap.x - 5),
      h: box.y() + box.height() - (ap.y - 5),
    }));

    syncAll();
    syncVideo();
    layer.draw();
    if (bannerLayer) bannerLayer.moveToTop();
  }

  // ── Banner system ─────────────────────────────────────────────────────────
  topBannerBtn?.addEventListener("click", () => {
    konvaContainer.style.display = "block";
    stage.width(container.clientWidth);
    stage.height(container.clientHeight);
    if (bannerLayer) bannerLayer.moveToTop();
    addBanner({ position: "top", text: "Add your text here" });
  });

  function addBanner(options = {}) {
    initBannerLayer();
    konvaContainer.style.display = "block";
    stage.width(container.clientWidth);
    stage.height(container.clientHeight);

    const vb = getVisibleVideoRect();
    const bannerH = options.height || 60;
    const text = options.text || "Your text here";
    const bgColor = options.bg_color || "#ffffff";
    const textColor = options.text_color || "#000000";
    const fontSize = options.font_size || 22;

    let isAttached = true;
    const attachSide = options.position === "bottom" ? "bottom" : "top";

    function getAttachedPosition() {
      const vb = getVisibleVideoRect();

      return {
        x: vb.x,
        y: attachSide === "bottom" ? vb.y + vb.h : vb.y - bannerH,
        w: vb.w,
      };
    }

    const initPos = getAttachedPosition(vb);

    const group = new Konva.Group({
      x: initPos.x,
      y: initPos.y,
      draggable: false,
    });

    const rect = new Konva.Rect({
      x: 0,
      y: 0,
      width: initPos.w,
      height: bannerH,
      fill: bgColor,
      shadowColor: "rgba(0,0,0,0.15)",
      shadowBlur: 8,
      shadowOffsetY: 2,
    });

    const label = new Konva.Text({
      x: 0,
      y: 0,
      width: initPos.w,
      height: bannerH,
      text,
      fontSize,
      fontFamily: "Arial, sans-serif",
      fontStyle: "bold",
      fill: textColor,
      align: "center",
      verticalAlign: "middle",
      padding: 10,
      wrap: "word",
    });
    function resizeBannerText() {
      let size = 32;

      label.fontSize(size);

      while (
        label.measureSize(label.text()).width > rect.width() - 20 &&
        size > 10
      ) {
        size--;
        label.fontSize(size);
      }
    }

    const dragHint = new Konva.Text({
      x: 0,
      y: 0,
      width: initPos.w,
      height: bannerH,
      text: "⠿",
      fontSize: 18,
      fill: "rgba(0,0,0,0.15)",
      align: "right",
      verticalAlign: "middle",
      padding: 10,
      listening: false,
    });

    const detachBorder = new Konva.Rect({
      x: -2,
      y: -2,
      width: initPos.w + 4,
      height: bannerH + 4,
      stroke: "#63b3ed",
      strokeWidth: 2,
      dash: [6, 3],
      cornerRadius: 2,
      listening: false,
      visible: false,
    });

    group.add(rect);
    group.add(label);
    resizeBannerText();
    group.add(dragHint);
    group.add(detachBorder);
    bannerLayer.add(group);
    bannerLayer.moveToTop();

    let clickTimer = null;

    group.on("click tap", () => {
      clickTimer = setTimeout(() => {
        if (isAttached) {
          isAttached = false;
          group.draggable(true);
          detachBorder.visible(true);
          document.body.style.cursor = "move";
        } else {
          isAttached = true;
          group.draggable(false);
          detachBorder.visible(false);
          document.body.style.cursor = "default";
          snapToVideo();
        }
        bannerLayer.draw();
      }, 200);
    });

    group.on("dblclick dbltap", () => {
      clearTimeout(clickTimer);
      const groupPos = group.getAbsolutePosition();
      const stageBox = konvaContainer.getBoundingClientRect();

      const input = document.createElement("textarea");
      Object.assign(input.style, {
        position: "fixed",
        top: `${stageBox.top + groupPos.y}px`,
        left: `${stageBox.left + groupPos.x}px`,
        width: `${rect.width()}px`,
        height: `${bannerH}px`,
        fontSize: `${fontSize}px`,
        fontFamily: "Impact, Arial Black, Arial, sans-serif",
        fontWeight: "bold",
        textAlign: "center",
        background: bgColor,
        color: textColor,
        border: "2px solid #63b3ed",
        borderRadius: "2px",
        padding: "8px",
        resize: "none",
        zIndex: "9999",
        outline: "none",
        boxSizing: "border-box",
      });

      input.value = label.text();
      document.body.appendChild(input);
      input.focus();
      input.select();

      function commitEdit() {
        label.text(input.value);

        resizeBannerText();

        bannerLayer.draw();
        input.remove();
      }

      input.addEventListener("blur", commitEdit);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          commitEdit();
        }
        if (e.key === "Escape") input.remove();
      });
    });

    group.on("dragend", () => {
      const pos = group.position();
      const idx = edits.findIndex((e) => e._bannerGroup === group);
      if (idx !== -1) {
        edits[idx].x = pos.x;
        edits[idx].y = pos.y;
      }
    });

    function snapToVideo() {
      const vb = getVisibleVideoRect();
      const pos = getAttachedPosition(vb);
      group.x(pos.x);
      group.y(pos.y);
      rect.width(pos.w);
      label.width(pos.w);
      dragHint.width(pos.w);
      detachBorder.width(pos.w + 4);
      bannerLayer.batchDraw();
    }

    function startTracking() {
      function tick() {
        if (isAttached) snapToVideo();
        requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    }
    startTracking();

    group.on("destroy", () => {
      /* tracking stops naturally */
    });

    bannerLayer.draw();

    edits = edits.filter(
      (e) => e.type !== "top_banner" && e.type !== "bottom_banner",
    );
    edits.push({
      type: attachSide === "bottom" ? "bottom_banner" : "top_banner",
      text,
      bg_color: bgColor,
      text_color: textColor,
      font_size: fontSize,
      x: initPos.x,
      y: initPos.y,
      _bannerGroup: group,
    });

    return group;
  }

  function updateCanvas() {
    container.querySelectorAll(".generated").forEach((el) => el.remove());
    container.style.overflow = "";
    cropDisplayRect = null;
    video.style.cssText = "";

    requestAnimationFrame(() => {
      const cropEdit = edits.find((e) => e.type === "crop");

      if (cropEdit) {
        if (!video.videoWidth || !video.videoHeight) {
          video.addEventListener("loadedmetadata", updateCanvas, {
            once: true,
          });
          return;
        }
        showCroppedPreview(cropEdit);
      } else {
        container.style.background = "";
        layer.visible(false);
      }

      const hasBanners = edits.some(
        (e) => e.type === "top_banner" || e.type === "bottom_banner",
      );
      if (hasBanners && bannerLayer) {
        konvaContainer.style.display = "block";
        bannerLayer.moveToTop();
        bannerLayer.batchDraw();
      }

      const vb = cropDisplayRect
        ? cropDisplayRect
        : (() => {
            const vr = video.getBoundingClientRect();
            const cr = container.getBoundingClientRect();
            return {
              left: vr.left - cr.left,
              top: vr.top - cr.top,
              w: vr.width,
              h: vr.height,
            };
          })();

      renderOverlays(vb);
    });
  }

  let exportFakeInterval = null;
  let exportDisplayPercent = 0;

  function startExportFakeProgress(bar, pct) {
    exportDisplayPercent = 0;
    exportFakeInterval = setInterval(() => {
      const target = Math.min(
        95,
        exportDisplayPercent +
          (exportDisplayPercent < 60
            ? 3
            : exportDisplayPercent < 85
              ? 1.2
              : 0.3),
      );
      if (exportDisplayPercent < target) {
        exportDisplayPercent = Math.min(target, exportDisplayPercent + 1);
        if (bar) bar.style.width = `${exportDisplayPercent}%`;
        if (pct) pct.textContent = `${Math.round(exportDisplayPercent)}%`;
      }
    }, 80);
  }

  function stopExportFakeProgress() {
    clearInterval(exportFakeInterval);
    exportFakeInterval = null;
  }

  if (expBtn) {
    expBtn.addEventListener("click", async () => {
      if (!edits.length) {
        alert("No edits to export.");
        return;
      }

      const overlay = document.getElementById("exportOverlay");
      const bar = document.getElementById("exportProgressBar");
      const pct = document.getElementById("exportPercent");
      const label = document.getElementById("exportLabel");

      if (overlay) overlay.style.display = "flex";
      if (label) label.textContent = "Exporting…";
      startExportFakeProgress(bar, pct);

      try {
        const exportEdits = edits.map(({ _bannerGroup, ...rest }) => rest);
        const res = await fetch(`/edit-video/${getFilename()}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ edits: exportEdits }),
        });

        if (!res.ok) {
          let detail = res.statusText;
          try {
            const err = await res.json();
            detail = err.details || err.error || detail;
          } catch (_) {}
          throw new Error(`Server error ${res.status}: ${detail}`);
        }

        const data = await res.json();
        if (!data.output_file)
          throw new Error("No output file returned from server");

        stopExportFakeProgress();
        if (bar) bar.style.width = "100%";
        if (pct) pct.textContent = "100%";
        if (label) label.textContent = "Download starting…";

        await new Promise((r) => setTimeout(r, 500));

        const a = document.createElement("a");
        a.href = `/download/${data.output_file}`;
        a.download = data.output_file;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);

        if (overlay) overlay.style.display = "none";
      } catch (err) {
        console.error(err);
        stopExportFakeProgress();
        if (overlay) overlay.style.display = "none";
        alert(`Export failed: ${err.message}`);
      }
    });
  }

  const cropBtn = document.getElementById("CropBtn");
  if (cropBtn) {
    cropBtn.addEventListener("click", () => {
      stage.width(container.clientWidth);
      stage.height(container.clientHeight);
      konvaContainer.style.display = "block";
      document.getElementById("cropControls").style.display = "block";
      layer.visible(true);
      showCropBox(16 / 9);
    });
  }

  function getVisibleVideoRect() {
    // Cropped preview — already an exact rect, no letterboxing to account for.
    if (cropDisplayRect) {
      return {
        x: cropDisplayRect.left,
        y: cropDisplayRect.top,
        w: cropDisplayRect.w,
        h: cropDisplayRect.h,
      };
    }

    // No crop yet: use the real visible video content rect, not the
    // <video> element's outer box. This is what makes top-banner snapping
    // work correctly for any source aspect ratio (16:9, 1:1, 4:3, 9:16…)
    // instead of only looking right when the video happens to fill its box.
    return getIntrinsicVideoRect();
  }

  const CHUNK_SIZE = 8 * 1024 * 1024;
  const MAX_PARALLEL = 4;
  async function uploadFileInChunks(file) {
    const total = Math.ceil(file.size / CHUNK_SIZE);
    const statusEl = document.getElementById("uploadStatus");

    // --- Show overlay ---
    const overlay = document.getElementById("uploadOverlay");
    const bar = document.getElementById("progressBar");
    const pct = document.getElementById("uploadPercent");
    const label = document.getElementById("uploadLabel");
    if (overlay) overlay.style.display = "flex";

    let completed = 0;
    let finalFilename = null;

    async function uploadChunk(i) {
      const fd = new FormData();
      fd.append(
        "chunk",
        file.slice(i * CHUNK_SIZE, Math.min((i + 1) * CHUNK_SIZE, file.size)),
      );
      fd.append("filename", file.name);
      fd.append("chunkIndex", i);
      fd.append("totalChunks", total);
      const res = await fetch("/upload-chunk", { method: "POST", body: fd });
      if (!res.ok) throw new Error(`Chunk ${i} upload failed (${res.status})`);
      const data = await res.json();
      completed++;

      // --- Update overlay ---
      const percent = Math.round((completed / total) * 100);
      if (bar) bar.style.width = `${percent}%`;
      if (pct) pct.textContent = `${percent}%`;
      if (statusEl) statusEl.innerText = `Uploading… ${percent}%`;

      if (data.status === "complete") finalFilename = data.filename;
    }

    const queue = Array.from({ length: total }, (_, i) => i);
    async function worker() {
      while (queue.length) {
        const i = queue.shift();
        await uploadChunk(i);
      }
    }

    try {
      await Promise.all(
        Array.from({ length: Math.min(MAX_PARALLEL, total) }, () => worker()),
      );
      if (!finalFilename)
        throw new Error("Server returned no filename after upload");

      // --- Swap spinner for checkmark before redirect ---
      if (label) label.textContent = "Processing…";
      if (bar) bar.style.width = "100%";
      if (pct) pct.textContent = "100%";

      window.location.href = `/canvas/${finalFilename}`;
    } catch (err) {
      if (overlay) overlay.style.display = "none";
      throw err; // lets the existing error handler in uploadForm show the message
    }
  }
});