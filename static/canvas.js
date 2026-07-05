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
  konvaContainer.style.display = "none";

  if (!video || !container) return;

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

  // Returns the correct vb for banner positioning:
  // - if cropDisplayRect is set, use it
  // - else if a crop edit exists, the cropped video fills the full container
  // - else fall back to the raw video bounds
  function getCropDisplayVb() {
    if (cropDisplayRect) {
      return {
        x: cropDisplayRect.left,
        y: cropDisplayRect.top,
        w: cropDisplayRect.w,
        h: cropDisplayRect.h,
      };
    }
    const cropEdit = edits.find((e) => e.type === "crop");
    if (cropEdit) {
      return {
        x: 0,
        y: 0,
        w: container.clientWidth,
        h: container.clientHeight,
      };
    }
    return getVideoBoundsOnCanvas();
  }

  function getCropData() {
    const vb = getVideoRenderBox();
    return {
      nx: Math.max(0, (cropRectangle.x() - vb.left) / vb.w),
      ny: Math.max(0, (cropRectangle.y() - vb.top) / vb.h),
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

    const bannerBtn = document.getElementById("topBannerBtn");
    if (bannerBtn) bannerBtn.style.display = "inline-block";

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
    });
  }

  const ratio169Btn = document.getElementById("ratio169");
  const ratio11Btn = document.getElementById("ratio11");

  function showCropBox(ratio) {
    currentAspectRatio = ratio;
    const vb = getVideoRenderBox();
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
    const startX = vb.left + (vb.w - boxW) / 2;
    const startY = vb.top + (vb.h - boxH) / 2;
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
        const res = await fetch(`/edit-json/${getFilename()}`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ prompt }),
        });
        if (!res.ok) throw new Error(`Server error ${res.status}`);
        const newEdits = await res.json();
        if (!Array.isArray(newEdits))
          throw new Error("Invalid server response");
      } catch (err) {
        console.error("Prompt error:", err);
        alert(`Error: ${err.message}`);
      } finally {
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

  function showCroppedPreview(crop) {
    const { nx, ny, nw, nh } = crop;
    const cw = container.clientWidth;
    const ch = container.clientHeight;

    let currentVideoW = cw;
    let currentVideoH = ch;
    let currentVideoLeft = cw / 2 - (nx + nw / 2) * cw;
    let currentVideoTop = ch / 2 - (ny + nh / 2) * ch;

    function applyVideoStyle() {
      video.style.position = "absolute";
      video.style.width = `${currentVideoW}px`;
      video.style.height = `${currentVideoH}px`;
      video.style.left = `${currentVideoLeft}px`;
      video.style.top = `${currentVideoTop}px`;
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
    layer.destroyChildren();
    if (bannerLayer) bannerLayer.moveToTop();

    const initW = nw * cw;
    const initH = nh * ch;
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

      const scaleX = bw / (nw * cw);
      const scaleY = bh / (nh * ch);
      currentVideoW = cw * scaleX;
      currentVideoH = ch * scaleY;
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
  document.getElementById("topBannerBtn")?.addEventListener("click", () => {
    if (!cropApplied) return;
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

    const vb = getCropDisplayVb();
    const bannerH = options.height || 60;
    const text = options.text || "Your text here";
    const bgColor = options.bg_color || "#ffffff";
    const textColor = options.text_color || "#000000";
    const fontSize = options.font_size || 22;

    let isAttached = true;
    const attachSide = options.position === "bottom" ? "bottom" : "top";

    function getAttachedPosition(vb) {
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
      const vb = getCropDisplayVb();
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

  if (expBtn) {
    expBtn.addEventListener("click", async () => {
      if (!edits.length) {
        alert("No edits to export.");
        return;
      }
      try {
        showStatus("Exporting video…");
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
        showStatus("Download starting…");
        const a = document.createElement("a");
        a.href = `/download/${data.output_file}`;
        a.download = data.output_file;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(hideStatus, 4000);
      } catch (err) {
        console.error(err);
        alert(`Export failed: ${err.message}`);
        hideStatus();
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
      showCropBox(16 / 9);
    });
  }

  const CHUNK_SIZE = 8 * 1024 * 1024;
  const MAX_PARALLEL = 4;
  async function uploadFileInChunks(file) {
    const total = Math.ceil(file.size / CHUNK_SIZE);
    const statusEl = document.getElementById("uploadStatus");
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
      if (statusEl)
        statusEl.innerText = `Uploading… ${Math.round((completed / total) * 100)}%`;
      if (data.status === "complete") finalFilename = data.filename;
    }

    const queue = Array.from({ length: total }, (_, i) => i);
    async function worker() {
      while (queue.length) {
        const i = queue.shift();
        await uploadChunk(i);
      }
    }

    await Promise.all(
      Array.from({ length: Math.min(MAX_PARALLEL, total) }, () => worker()),
    );

    if (!finalFilename)
      throw new Error("Server returned no filename after upload");
    window.location.href = `/canvas/${finalFilename}`;
  }
  if (uploadForm) {
    uploadForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const file = videoFileInput && videoFileInput.files[0];
      const statusEl = document.getElementById("uploadStatus");
      if (!file) {
        alert("Please select a file");
        return;
      }
      try {
        await uploadFileInChunks(file);
        if (statusEl) statusEl.innerText = "Upload complete!";
      } catch (err) {
        if (statusEl) statusEl.innerText = `Upload failed: ${err.message}`;
        console.error("Upload error:", err);
      }
    });
  }
});
