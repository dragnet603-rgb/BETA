/**
 * Autoquence — client-side export engine (no FFmpeg).
 *
 * OUTPUT: every export is a fixed 9:16 frame (1080x1920) in FIT mode:
 * the picture is always FULLY VISIBLE, fitted and centered — mismatched
 * source aspect ratios are letterboxed (bars filled with the scene
 * background color) instead of being zoom-cropped.
 *
 * RENDERS THE FINAL VIDEO FROM THE SAME THING YOU SEE ON THE CANVAS:
 *   1. background fill
 *   2. video frame (with crop mapping identical to renderVideo())
 *   3. Konva banner layer snapshot  -> pixel-identical banners
 *   4. shapes / images              -> replicated from _renderShape/_renderImage CSS
 *   5. free text                    -> replicated from _renderText CSS
 *
 * ENCODING:
 *   - Primary:  WebCodecs VideoEncoder/AudioEncoder + Mp4Muxer -> real MP4.
 *   - Fallback: MediaRecorder (realtime capture) -> MP4 on modern Chrome,
 *               otherwise WebM.
 *
 * The engine talks to canvas.js exclusively through window.__AQ_CANVAS_BRIDGE__.
 */
(function () {
  "use strict";

  const FPS = 30;

  // Every export is always 9:16 regardless of source aspect.
  const OUTPUT_W = 1080;
  const OUTPUT_H = 1920;

  // ─────────────────────────────────────────────────────────────
  // Small helpers
  // ─────────────────────────────────────────────────────────────
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function evenize(n) {
    return Math.max(2, Math.floor(n / 2) * 2);
  }

  function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
  }

  function supportsWebCodecs() {
    return typeof window.VideoEncoder === "function" &&
           typeof window.AudioData !== "undefined" &&
           typeof window.VideoFrame === "function";
  }

  function supportsMediaRecorder() {
    return typeof window.MediaRecorder === "function";
  }

  async function pickVideoCodec(width, height, fps, bitrate) {
    const candidates = [
      "avc1.640033", // High 5.1
      "avc1.640028", // High 4.0
      "avc1.4d0028", // Main 4.0
      "avc1.42001f", // Baseline 3.1
    ];
    for (const codec of candidates) {
      try {
        const support = await VideoEncoder.isConfigSupported({
          codec, width, height, framerate: fps, bitrate,
          avc: { format: "avc" }, // mp4-muxer requires the 'avc' format
        });
        if (support && support.supported) return codec;
      } catch (_) { /* try next */ }
    }
    throw new Error("No supported H.264 encoder found.");
  }

  // Seek a dedicated hidden <video> to an exact timestamp and wait until
  // the frame is actually presented.
  function makeSeekableVideo(src) {
    const v = document.createElement("video");
    v.muted = true;
    v.playsInline = true;
    v.preload = "auto";
    v.src = src;
    return v;
  }

  function waitForEvent(target, name, timeoutMs) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error('Timed out waiting for "' + name + '".'));
      }, timeoutMs);
      function cleanup() {
        clearTimeout(timer);
        target.removeEventListener(name, ok);
        target.removeEventListener("error", bad);
      }
      function ok(e) { cleanup(); resolve(e); }
      function bad()  { cleanup(); reject(new Error('Media error during "' + name + '".')); }
      target.addEventListener(name, ok, { once: true });
      target.addEventListener("error", bad, { once: true });
    });
  }

  async function seekVideo(video, t, timeoutMs) {
    if (Math.abs(video.currentTime - t) < 1e-4 && video.readyState >= 2) return;
    video.currentTime = t;
    // Soft timeout: a single stalled seek must not stall the whole export —
    // proceed with whatever frame the element currently presents.
    try {
      await waitForEvent(video, "seeked", timeoutMs || 8000);
    } catch (_) { /* keep going with the current frame */ }
    // Prefer precise presentation callbacks when available, but race them
    // against a short deadline instead of polling with sleeps.
    if (typeof video.requestVideoFrameCallback === "function") {
      await Promise.race([
        new Promise((resolve) => {
          try {
            const id = video.requestVideoFrameCallback(() => resolve());
            // If the callback never fires, resolve via the race deadline below.
            if (id === undefined) resolve();
          } catch (_) { resolve(); }
        }),
        sleep(14),
      ]);
    }
  }

  // Double-buffered frame source: two hidden <video> elements so that the
  // seek for frame N+1 overlaps the draw/encode of frame N. Tracks which
  // source timestamp each buffer currently holds to skip redundant seeks
  // for duplicate frames (e.g. slow-motion / rounded timestamps).
  function makeFrameSource(url) {
    const vids = [makeSeekableVideo(url), makeSeekableVideo(url)];
    const heldTime = [NaN, NaN];
    let cur = 0;
    let pending = null; // { slot, t, promise }

    async function ensure(slot, t) {
      if (Math.abs(heldTime[slot] - t) < 1e-4 && vids[slot].readyState >= 2) return;
      await seekVideo(vids[slot], t);
      heldTime[slot] = t;
    }

    return {
      ready: function () {
        return Promise.all(vids.map(function (v) {
          return waitForEvent(v, "loadedmetadata", 20000);
        }));
      },
      video: function () { return vids[cur]; },
      // Start seeking time t into the background buffer. Returns the seek
      // promise (or null when no seek is needed — buffer already holds t).
      prefetch: function (t) {
        if (t == null) return null;
        const bg = 1 - cur;
        if (Math.abs(heldTime[bg] - t) < 1e-4) return null;
        pending = { slot: bg, t: t, promise: ensure(bg, t).catch(function () {}) };
        return pending.promise;
      },
      // Swap to the buffer holding time t (awaiting a pending prefetch,
      // reusing an already-seeked buffer, or seeking in place as fallback).
      advance: async function (t) {
        const bg = 1 - cur;
        if (pending && Math.abs(pending.t - t) < 1e-4) {
          await pending.promise;
          heldTime[pending.slot] = t;
          cur = pending.slot;
        } else if (Math.abs(heldTime[bg] - t) < 1e-4 && vids[bg].readyState >= 2) {
          cur = bg;
        } else {
          await ensure(cur, t);
        }
        pending = null;
      },
      destroy: function () {
        for (const v of vids) {
          v.removeAttribute("src");
          try { v.load(); } catch (_) {}
        }
      },
    };
  }

  // Await encoder queue drainage using the "dequeue" event (with a polling
  // safety net) instead of a fixed sleep spin loop.
  function encoderBackpressure(encoder, maxQueue) {
    if (encoder.encodeQueueSize <= maxQueue) return Promise.resolve();
    return new Promise((resolve) => {
      let done = false;
      const finish = function () {
        if (done) return;
        done = true;
        clearInterval(poll);
        encoder.removeEventListener("dequeue", onDequeue);
        resolve();
      };
      const onDequeue = function () {
        if (encoder.encodeQueueSize <= maxQueue || encoder.state === "closed") finish();
      };
      const poll = setInterval(function () {
        if (encoder.encodeQueueSize <= maxQueue || encoder.state === "closed") finish();
      }, 16);
      encoder.addEventListener("dequeue", onDequeue);
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Layout / output sizing (FIT MODE)
  //
  // The output frame corresponds EXACTLY to the preview's video viewport
  // (getVideoRect()). The picture is ALWAYS fully visible inside the
  // 9:16 frame — fitted and centered, with letterbox/pillarbox bars
  // filled by the scene background color.
  // ─────────────────────────────────────────────────────────────
  function computeOutput(bridge, scene, meta) {
    const vw = scene.video.naturalWidth || meta.videoWidth;
    const vh = scene.video.naturalHeight || meta.videoHeight;
    if (!vw || !vh) throw new Error("Video dimensions unavailable.");

    // Output resolution comes from the composition model (single source of
    // truth, set in canvas.js createEmptyScene); fall back to the engine
    // defaults for legacy scenes that don't carry it.
    //
    // HARD GUARANTEE: every export is fixed 9:16 (1080x1920) regardless of
    // scene.canvas dimensions — matching the server FFmpeg path which forces
    // scale=1080:1920 + pad + setsar=1. This keeps client and server exports
    // byte-consistent and guarantees the "9:16" contract even if a resize
    // action ever set a non-9:16 canvas.
    const OUT_W = 1080;
    const OUT_H = 1920;

    // Base source region (source pixels): the committed crop selection,
    // or the full frame when uncropped.
    const crop = scene.video.crop;
    let base;
    if (crop && crop.applied) {
      base = {
        x: clamp(crop.x, 0, 1) * vw,
        y: clamp(crop.y, 0, 1) * vh,
        w: clamp(crop.w, 0.01, 1) * vw,
        h: clamp(crop.h, 0.01, 1) * vh,
      };
    } else {
      base = { x: 0, y: 0, w: vw, h: vh };
    }

    const anchor = bridge.getAnchorRect();
    if (!anchor || anchor.w <= 0 || anchor.h <= 0) {
      throw new Error("Preview anchor rect unavailable.");
    }

    // FIT mode: the preview letterboxes the crop region inside the
    // viewport (never zoom-crops), so export the ENTIRE base region and
    // fit it inside the output frame. The background fills the bars.
    const srcRect = { x: base.x, y: base.y, w: base.w, h: base.h };

    // Map the preview video rect onto the output frame with FIT
    // semantics (min — never crop). placedW/H is the letterboxed video
    // area; offX/offY center it inside the 9:16 frame.
    const k = Math.min(OUT_W / anchor.w, OUT_H / anchor.h);
    const placedW = anchor.w * k;
    const placedH = anchor.h * k;
    const offX = Math.round((OUT_W - placedW) / 2);
    const offY = Math.round((OUT_H - placedH) / 2);

    return {
      width: OUT_W,
      height: OUT_H,
      srcRect,
      anchor,
      scale: k,     // preview px -> output px
      offX, offY,
      placedW, placedH,
    };
  }


  // ─────────────────────────────────────────────────────────────
  // Static compositing plan (built once per export)
  //
  // draw order == CSS stacking in the preview:
  //   video < shapes/images (z+10) < Konva banners (z 999) < free text (z+1000)
  // ─────────────────────────────────────────────────────────────
  function buildDrawPlan(bridge, scene, geom) {
    const { anchor, scale: k, offX, offY } = geom;

    const underLayer = []; // shapes/images drawn below banners
    const overLayer = [];  // free texts drawn above banners

    const sorted = [...scene.elements].sort((a, b) => (a.zIndex || 0) - (b.zIndex || 0));
    for (const el of sorted) {
      if (el.type === "banner") continue; // drawn via the Konva snapshot

      // Text parented to a banner is already drawn by that banner (Konva).
      if (el.type === "text" && el.parentId) {
        const parent = scene.elements.find((e) => e.id === el.parentId);
        if (parent && parent.type === "banner") continue;
      }

      const r = bridge.getElementRect(el);
      const op = {
        el: el,
        x: offX + (r.x - anchor.x) * k,
        y: offY + (r.y - anchor.y) * k,
        w: r.w * k,
        h: r.h * k,
        _k: k,
      };

      if (el.type === "shape")                            underLayer.push(Object.assign({}, op, { kind: "shape" }));
      else if (el.type === "image" || el.type === "logo") underLayer.push(Object.assign({}, op, { kind: "image" }));
      else if (el.type === "text")                        overLayer.push(Object.assign({}, op, { kind: "text" }));
      else console.warn("[EXPORT ENGINE] Unknown element type:", el.type);
    }

    return {
      underLayer: underLayer,
      overLayer: overLayer,
      hasBanners: scene.elements.some((e) => e.type === "banner"),
    };
  }

  // ── shape (mirrors _renderShape) ──
  function drawShapeOp(ctx, op) {
    const p = op.el.props || {};
    ctx.save();
    ctx.globalAlpha = Number(p.opacity === undefined ? 1 : p.opacity);

    const radius = p.shape === "circle"
      ? Math.min(op.w, op.h) / 2
      : Number(p.radius || 0);

    pathRoundRect(ctx, op.x, op.y, op.w, op.h, radius);
    ctx.fillStyle = p.fill || p.color || "#ffffff";
    ctx.fill();

    if (p.borderWidth) {
      const bw = Number(p.borderWidth);
      // CSS border-box draws the border INSIDE; approximate with an inset stroke.
      pathRoundRect(
        ctx, op.x + bw / 2, op.y + bw / 2, Math.max(0, op.w - bw), Math.max(0, op.h - bw),
        Math.max(0, radius - bw / 2)
      );
      ctx.lineWidth = bw;
      ctx.strokeStyle = p.borderColor || "#000000";
      ctx.stroke();
    }
    ctx.restore();
  }

  function pathRoundRect(ctx, x, y, w, h, r) {
    const rr = Math.max(0, Math.min(r, w / 2, h / 2));
    ctx.beginPath();
    if (typeof ctx.roundRect === "function") {
      ctx.roundRect(x, y, w, h, rr);
    } else {
      ctx.moveTo(x + rr, y);
      ctx.arcTo(x + w, y,     x + w, y + h, rr);
      ctx.arcTo(x + w, y + h, x,     y + h, rr);
      ctx.arcTo(x,     y + h, x,     y,     rr);
      ctx.arcTo(x,     y,     x + w, y,     rr);
      ctx.closePath();
    }
  }

  // ── image/logo, object-fit: contain (mirrors _renderImage) ──
  function drawImageOp(ctx, op, imgCache) {
    const p = op.el.props || {};
    const src = p.src || p.url || p.asset || null;
    const img = src ? imgCache.get(src) : null;
    if (!img || !img.complete || !img.naturalWidth) return;

    ctx.save();
    ctx.globalAlpha = Number(p.opacity === undefined ? 1 : p.opacity);

    // object-fit: contain inside the rect
    const s = Math.min(op.w / img.naturalWidth, op.h / img.naturalHeight);
    const dw = img.naturalWidth * s;
    const dh = img.naturalHeight * s;
    ctx.drawImage(img, op.x + (op.w - dw) / 2, op.y + (op.h - dh) / 2, dw, dh);
    ctx.restore();
  }

  // ── free text (mirrors _renderText incl. wrap + flex alignment) ──
  function drawTextOp(ctx, op) {
    const p = op.el.props || {};
    const fontFamily = window.__AQ_CANVAS_BRIDGE__.cssFontFamily(p.fontFamily || p.font);
    // Free-text default size comes from the shared typography engine
    // (window.__AQ_TYPO__, set in canvas.js): defaultFs is in OUTPUT px,
    // so no k-scaling needed here — op._k already converts explicit
    // preview-px sizes to output px.
    const typo = window.__AQ_TYPO__ || { text: { defaultFs: 30, lineHeight: 1.25 } };
    const fontSizePx = (p.fontSize !== undefined && p.fontSize !== null)
      ? Number(p.fontSize) * op._k
      : Number(typo.text.defaultFs);
    const fontWeight = p.fontWeight || p.font_weight || "bold";
    const textAlign  = p.textAlign || p.alignment || "center";
    const color      = p.color || p.textColor || p.text_color || "#ffffff";
    const lineHeight = Number(p.lineHeight || typo.text.lineHeight);
    const padding    = Number(p.padding === undefined ? 4 : p.padding) * op._k;

    ctx.save();

    // Background pill
    const bgColor = p.backgroundColor || p.background_color || null;
    let bgA;
    if (bgColor) {
      bgA = Number(p.backgroundOpacity !== undefined ? p.backgroundOpacity :
                   (p.background_opacity !== undefined ? p.background_opacity : 0.7));
    } else {
      bgA = Number((p.backgroundOpacity ?? 0) || (p.background_opacity ?? 0) || 0);
    }
    if (bgA > 0) {
      ctx.fillStyle = bgColor ? hexAlpha(bgColor, bgA) : ("rgba(0,0,0," + bgA + ")");
      pathRoundRect(ctx, op.x, op.y, op.w, op.h,
        Number(p.borderRadius === undefined ? 4 : p.borderRadius) * op._k);
      ctx.fill();
    }

    if (p.shadow) {
      ctx.shadowColor = "rgba(0,0,0,0.9)";
      ctx.shadowOffsetX = 1 * op._k;
      ctx.shadowOffsetY = 1 * op._k;
      ctx.shadowBlur   = 4 * op._k;
    }
    ctx.globalAlpha *= Number(p.opacity === undefined ? 1 : p.opacity);

    ctx.fillStyle = color;
    ctx.textBaseline = "alphabetic";
    try {
      if ("letterSpacing" in ctx && p.letterSpacing) {
        ctx.letterSpacing = (Number(p.letterSpacing) * op._k) + "px";
      }
    } catch (_) {}
    ctx.font = fontWeight + " " + fontSizePx + "px " + fontFamily;

    const boxX = op.x + padding;
    const boxY = op.y + padding;
    const boxW = Math.max(1, op.w - padding * 2);
    const boxH = Math.max(fontSizePx, op.h - padding * 2);

    const lineH = fontSizePx * lineHeight;
    const lines = [];
    const paragraphs = String(p.text || p.content || "").split("\n");
    for (const para of paragraphs) {
      for (const l of wrapWords(ctx, para, boxW)) lines.push(l);
    }
    if (!lines.length) lines.push("");

    const totalH = lines.length * lineH;

    // vertical-align (flex alignItems)
    let ty;
    if (p.verticalAlign === "top")         ty = boxY;
    else if (p.verticalAlign === "bottom") ty = boxY + boxH - totalH;
    else                                   ty = boxY + (boxH - totalH) / 2;

    ctx.textAlign = textAlign === "center" ? "center" : (textAlign === "right" ? "right" : "left");
    const tx = textAlign === "center" ? boxX + boxW / 2 : (textAlign === "right" ? boxX + boxW : boxX);

    for (let i = 0; i < lines.length; i++) {
      // Approximate alphabetic baseline centered inside each line box,
      // like the flex row layout in _renderText.
      const baseline = ty + lineH * i + (lineH - fontSizePx) / 2 + fontSizePx * 0.82;
      ctx.fillText(lines[i], tx, baseline);
    }

    ctx.restore();
  }

  function wrapWords(ctx, text, maxWidth) {
    if (!text) return [""];
    const words = text.split(/\s+/).filter(Boolean);
    if (!words.length) return [""];
    const lines = [];
    let cur = "";
    for (const word of words) {
      const attempt = cur ? cur + " " + word : word;
      if (!cur || ctx.measureText(attempt).width <= maxWidth) {
        cur = attempt;
        // Hard-break absurdly long single words
        while (ctx.measureText(cur).width > maxWidth && cur.length > 1) {
          let cut = cur.length - 1;
          while (cut > 1 && ctx.measureText(cur.slice(0, cut)).width > maxWidth) cut--;
          lines.push(cur.slice(0, cut));
          cur = cur.slice(cut);
        }
      } else {
        lines.push(cur);
        cur = word;
      }
    }
    if (cur) lines.push(cur);
    return lines;
  }

  function hexAlpha(hex, a) {
    hex = String(hex || "#000").replace("#", "");
    if (hex.length === 3) hex = hex.split("").map(function (c) { return c + c; }).join("");
    const n = parseInt(hex, 16);
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  // ─────────────────────────────────────────────────────────────
  // One composite frame at output resolution
  // ─────────────────────────────────────────────────────────────

  // Compute output-frame placement rects for each banner snapshot.
  //  - Top banners hug the TOP EDGE of the fitted VIDEO PICTURE (the
  //    letterboxed rect), with no baked-bar offset — matching the preview's
  //    bannerSnapY() and the server FFmpeg path (banners drawn pre-pad, so
  //    they track the picture).
  //  - Bottom banners are pinned to the picture's bottom edge the same way.
  //  - Banners the user manually dragged keep their position relative to
  //    the fitted video area (preview-accurate custom placement).
  function placeBannerOps(geom, banners, bars) {
    const ops = [];
    for (const b of banners) {
      const s = b.snap;
      if (Math.abs(s.dragOffsetY || 0) > 0.5) {
        // Custom placement: track the video rect like the preview does.
        ops.push({
          canvas: s.canvas,
          x: geom.offX + (s.x - geom.anchor.x) * geom.scale,
          y: geom.offY + (s.y - geom.anchor.y) * geom.scale,
          w: (s.canvas.width / s.pr) * geom.scale,
          h: (s.canvas.height / s.pr) * geom.scale,
        });
        continue;
      }
      const isBot = String(s.position || "top").toLowerCase().includes("bottom");
      // Hug the VIDEO PICTURE rect (the letterboxed area), not the output
      // frame: offX/offY is where the fitted picture starts and placedW/H
      // is its size. Baked-in bar offsets are IGNORED here (barOff = 0) so a
      // top banner sits on the very top edge of the picture and a bottom
      // banner on the very bottom — matching the preview's bannerSnapY()
      // and the server FFmpeg path. The server stays consistent because it
      // draws banners BEFORE the final scale/pad, so they ride along.
      const w = geom.placedW;
      const h = s.canvas.height * (w / s.canvas.width);
      const barOff = 0; // always flush to the picture edge
      const y = isBot
        ? geom.offY + geom.placedH - h - barOff
        : geom.offY + barOff;
      // Clamp fully inside the picture (matches bannerSnapY's clamp).
      const yFinal = Math.max(
        geom.offY,
        Math.min(y, geom.offY + geom.placedH - h)
      );
      ops.push({
        canvas: s.canvas,
        x: geom.offX,
        y: Math.round(yFinal),
        w: w,
        h: h,
      });
    }
    return ops;
  }

  // Pre-render everything that does NOT change between frames (background,
  // static shapes/images below the video, Konva banner snapshot, static
  // texts) into two cached canvases: one drawn under the video, one over.
  // Per-frame cost then drops to three drawImage blits + the video itself.
  function buildStaticLayers(plan, snapshot, snapshotPr, geom, imgCache, banners, bars) {
    const scene = window.__AQ_CANVAS_BRIDGE__.getScene();
    const bgColor = scene.canvas.background || "#000000";

    const under = document.createElement("canvas");
    under.width = geom.width; under.height = geom.height;
    const uctx = under.getContext("2d", { alpha: false });
    uctx.fillStyle = bgColor;
    uctx.fillRect(0, 0, geom.width, geom.height);
    for (const op of plan.underLayer) {
      if (op.kind === "shape") drawShapeOp(uctx, op);
      else drawImageOp(uctx, op, imgCache);
    }

    const over = document.createElement("canvas");
    over.width = geom.width; over.height = geom.height;
    const octx = over.getContext("2d");
    if (banners && banners.length) {
      // Preferred: individual banner canvases pinned to the frame edges.
      for (const op of placeBannerOps(geom, banners, bars)) {
        octx.drawImage(op.canvas, op.x, op.y, op.w, op.h);
      }
    } else if (plan.hasBanners && snapshot) {
      // Fallback: whole-layer snapshot aligned to the video rect.
      const a = geom.anchor;
      const destX = geom.offX - a.x * geom.scale;
      const destY = geom.offY - a.y * geom.scale;
      const destW = (snapshot.width / snapshotPr) * geom.scale;
      const destH = (snapshot.height / snapshotPr) * geom.scale;
      octx.drawImage(snapshot, destX, destY, destW, destH);
    }
    for (const op of plan.overLayer) drawTextOp(octx, op);

    return { under: under, over: over };
  }

  function drawComposite(ctx, plan, snapshot, snapshotPr, geom, sourceEl, staticLayers) {
    const W = geom.width, H = geom.height;

    if (staticLayers) {
      // Fast path: cached static layers, only the video changes per frame.
      ctx.drawImage(staticLayers.under, 0, 0);
      ctx.drawImage(
        sourceEl,
        geom.srcRect.x, geom.srcRect.y, geom.srcRect.w, geom.srcRect.h,
        geom.offX, geom.offY, geom.placedW, geom.placedH
      );
      ctx.drawImage(staticLayers.over, 0, 0);
      return;
    }

    // 1. Background — fills the WHOLE 9:16 frame; only visible in the
    //    unlikely event the video placement leaves a rounding gap.
    const scene = window.__AQ_CANVAS_BRIDGE__.getScene();
    ctx.fillStyle = scene.canvas.background || "#000000";
    ctx.fillRect(0, 0, W, H);

    // 2. Video frame — the visible source region maps onto its fitted
    //    rect, which is letterboxed inside the frame in FIT mode (bars
    //    were painted by the background fill in step 1).
    ctx.drawImage(
      sourceEl,
      geom.srcRect.x, geom.srcRect.y, geom.srcRect.w, geom.srcRect.h,
      geom.offX, geom.offY, geom.placedW, geom.placedH
    );

    // 3. Shapes / images below banners
    for (const op of plan.underLayer) {
      if (op.kind === "shape") drawShapeOp(ctx, op);
      else drawImageOp(ctx, op, plan.imgCache);
    }

    // 4. Konva banner snapshot — blit the whole stage so banners that
    //    extend above/below the video are NOT clipped, aligned so the
    //    anchor lands exactly on its fitted rect.
    if (plan.hasBanners && snapshot) {
      const a = geom.anchor;
      const destX = geom.offX - a.x * geom.scale;
      const destY = geom.offY - a.y * geom.scale;
      const destW = (snapshot.width / snapshotPr) * geom.scale;
      const destH = (snapshot.height / snapshotPr) * geom.scale;
      ctx.drawImage(snapshot, destX, destY, destW, destH);
    }

    // 5. Free texts above everything
    for (const op of plan.overLayer) drawTextOp(ctx, op);
  }

  async function prepareAssets(bridge, scene, geom) {
    // Fonts must be ready before BOTH the Konva snapshot AND 2D text drawing.
    await document.fonts.ready;
    const families = new Set();
    for (const el of scene.elements) {
      const p = el.props || {};
      const f = p.fontFamily || p.font || null;
      if (f) families.add(f);
    }
    for (const f of families) { try { await bridge.ensureFont(f); } catch (_) {} }
    await document.fonts.ready;

    // Force a clean final render so the Konva layer reflects the current
    // state (auto-shrink, positions, fonts) before we snapshot it.
    bridge.forceRender();

    const snapshotPr = geom.scale; // oversample: crisp at output resolution
    const snapshot = bridge.snapshotKonvaLayer(snapshotPr);

    // Per-banner snapshots: preferred path for the export. Each banner is
    // rendered standalone so top/bottom banners can be pinned to the OUTPUT
    // frame edges (matching the server FFmpeg path) instead of tracking the
    // letterboxed video rect. The whole-layer snapshot remains as fallback.
    const banners = [];
    if (typeof bridge.snapshotBannerGroup === "function") {
      for (const el of scene.elements) {
        if (el.type !== "banner") continue;
        try {
          const s = bridge.snapshotBannerGroup(el, snapshotPr);
          if (s && s.canvas && s.canvas.width && s.canvas.height) {
            banners.push({ el: el, snap: s });
          }
        } catch (_) { /* fall back to the whole-layer snapshot for this run */ }
      }
      if (banners.length !== scene.elements.filter((e) => e.type === "banner").length) {
        banners.length = 0; // partial capture -> use the fallback everywhere
      }
    }

    // Preload images referenced by image/logo elements
    const imgCache = new Map();
    const loads = [];
    for (const el of scene.elements) {
      const p = el.props || {};
      const src = p.src || p.url || p.asset || null;
      if ((el.type === "image" || el.type === "logo") && src && !imgCache.has(src)) {
        const img = new Image();
        loads.push(new Promise((res) => {
          img.onload = res;
          img.onerror = res;
          setTimeout(res, 5000); // never hang on a broken asset
        }));
        img.crossOrigin = "anonymous";
        img.src = src;
        imgCache.set(src, img);
      }
    }
    await Promise.all(loads);

    return { snapshot: snapshot, snapshotPr: snapshotPr, imgCache: imgCache, banners: banners };
  }

  // ─────────────────────────────────────────────────────────────
  // Timeline math (trim + speed)
  // ─────────────────────────────────────────────────────────────
  function computeTimeline(scene, duration) {
    const trim = scene.canvas.trim || null;
    let sp = Number(scene.canvas.speed) || 1.0;
    if (!(sp > 0)) sp = 1.0;

    let start = 0;
    let end = duration;
    if (trim) {
      start = Math.max(0, Number(trim.start) || 0);
      const tEnd = trim.end === undefined || trim.end === null ? duration : Number(trim.end);
      end = clamp(Number.isFinite(tEnd) ? tEnd : duration, start + 1 / FPS, duration);
    }
    const outDuration = (end - start) / sp;
    const totalFrames = Math.max(1, Math.round(outDuration * FPS));
    return { start: start, end: end, speed: sp, outDuration: outDuration, totalFrames: totalFrames };
  }

  function srcTimeAt(tl, frameIdx) {
    return tl.start + (frameIdx / FPS) * tl.speed;
  }

  // ─────────────────────────────────────────────────────────────
  // AUDIO — decode, trim, rate-change, AAC encode.
  // Returns null when audio can't be produced (export stays silent).
  // ─────────────────────────────────────────────────────────────
  async function encodeAudio(sourceUrl, tl) {
    if (typeof window.AudioEncoder !== "function") return null;
    let ac = null;
    try {
      const resp = await fetch(sourceUrl);
      if (!resp.ok) return null;
      const bytes = await resp.arrayBuffer();
      if (!bytes.byteLength) return null;

      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ac = new AC();
      const decoded = await ac.decodeAudioData(bytes);

      const sr = decoded.sampleRate;
      const chCount = Math.min(2, decoded.numberOfChannels);
      const outSamples = Math.max(1, Math.ceil(tl.outDuration * sr));

      const OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      if (!OAC) { await ac.close(); ac = null; return null; }
      const oac = new OAC(chCount, outSamples, sr);
      const node = oac.createBufferSource();
      node.buffer = decoded;
      node.playbackRate.value = tl.speed;
      node.connect(oac.destination);
      node.start(0, tl.start, tl.end - tl.start);
      const rendered = await oac.startRendering();

      await ac.close(); ac = null;

      let cfg = { codec: "mp4a.40.2", sampleRate: sr, numberOfChannels: chCount, bitrate: 128000 };
      let sup = null;
      try { sup = await AudioEncoder.isConfigSupported(cfg); } catch (_) {}
      if (!sup || !sup.supported) {
        for (const altRate of [48000, 44100]) {
          cfg = Object.assign({}, cfg, { sampleRate: altRate });
          try {
            sup = await AudioEncoder.isConfigSupported(cfg);
            if (sup && sup.supported) break;
          } catch (_) {}
        }
        if (!sup || !sup.supported) return null;
      }

      const chunks = [];
      let lastMeta = null;
      const enc = new AudioEncoder({
        output: function (chunk, meta) { chunks.push(chunk); if (meta) lastMeta = meta; },
        error: function (e) { console.warn("[EXPORT ENGINE] audio encoder:", e); },
      });
      enc.configure(cfg);

      const left = rendered.getChannelData(0);
      const right = chCount > 1 ? rendered.getChannelData(1) : null;
      const CHUNK = 1024;
      for (let off = 0; off < outSamples; off += CHUNK) {
        const n = Math.min(CHUNK, outSamples - off);
        const data = new Float32Array(n * chCount);
        data.set(left.subarray(off, off + n), 0);
        if (right) data.set(right.subarray(off, off + n), n);
        const ad = new AudioData({
          format: "f32-planar",
          sampleRate: cfg.sampleRate,
          numberOfFrames: n,
          numberOfChannels: chCount,
          timestamp: Math.round((off / sr) * 1e6),
          data: data,
        });
        enc.encode(ad);
        ad.close();
        while (enc.encodeQueueSize > 16) await sleep(4);
      }
      await enc.flush();
      enc.close();

      if (!chunks.length) return null;
      return { chunks: chunks, meta: lastMeta, sampleRate: cfg.sampleRate, numberOfChannels: chCount };
    } catch (err) {
      console.warn("[EXPORT ENGINE] Audio export skipped:", err);
      if (ac) { try { await ac.close(); } catch (_) {} }
      return null;
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Primary path: WebCodecs -> real MP4
  // ─────────────────────────────────────────────────────────────
  async function exportViaWebCodecs(state, onProgress) {
    const MuxerLib = window.Mp4Muxer;
    if (!MuxerLib) throw new Error("mp4-muxer vendor bundle not loaded.");

    const geom = state.geom, tl = state.tl;

    // bits-per-pixel factor tuned for crisp H.264 at 1080x1920@30:
    // ~7.5 Mbps for the standard output frame (old value: ~4.35 Mbps,
    // which made motion-heavy exports look soft/blocky).
    const bitrate = Math.round(clamp(geom.width * geom.height * FPS * 0.12, 8000000, 24000000));
    const codec = await pickVideoCodec(geom.width, geom.height, FPS, bitrate);

    // Probe audio first so the muxer config knows whether to include it.
    onProgress && onProgress(2);
    const audioPkt = await encodeAudio(state.videoUrl, tl);
    if (!audioPkt) {
      state.warnings.push("Audio could not be exported (source audio undecodable or unsupported) — the MP4 will be silent.");
    }

    const target = new MuxerLib.ArrayBufferTarget();
    const muxerConfig = {
      target: target,
      video: { codec: "avc", width: geom.width, height: geom.height },
      fastStart: "in-memory",
    };
    if (audioPkt) {
      muxerConfig.audio = {
        codec: "aac",
        sampleRate: audioPkt.sampleRate,
        numberOfChannels: audioPkt.numberOfChannels,
      };
    }
    const muxer = new MuxerLib.Muxer(muxerConfig);

    // Mux the (already-encoded) audio track up front so finalize() isn't
    // serialized behind the video encode.
    if (audioPkt) {
      for (const c of audioPkt.chunks) muxer.addAudioChunk(c, audioPkt.meta);
    }

    let encodeError = null;
    const venc = new VideoEncoder({
      output: function (chunk, meta) { muxer.addVideoChunk(chunk, meta); },
      error: function (e) { encodeError = e; },
    });
    venc.configure({
      codec: codec,
      width: geom.width,
      height: geom.height,
      framerate: FPS,
      bitrate: bitrate,
      latencyMode: "quality",
      avc: { format: "avc" },
    });

    // Double-buffered decoder: seek frame N+1 while frame N draws/encodes.
    const frameSrc = makeFrameSource(state.videoUrl);
    await frameSrc.ready();

    const canvas = document.createElement("canvas");
    canvas.width = geom.width;
    canvas.height = geom.height;
    const ctx = canvas.getContext("2d", { alpha: false });

    // Everything that never changes frame-to-frame is rendered once.
    const staticLayers = buildStaticLayers(
      state.plan, state.assets.snapshot, state.assets.snapshotPr,
      geom, state.plan.imgCache, state.assets.banners, state.bars
    );

    try {
      // Prime the pipeline with the first frame.
      await frameSrc.advance(srcTimeAt(tl, 0));

      for (let i = 0; i < tl.totalFrames; i++) {
        if (encodeError) throw encodeError;

        // Kick off the seek for the NEXT frame so it overlaps this frame's
        // draw + encode work.
        const nextT = i + 1 < tl.totalFrames ? srcTimeAt(tl, i + 1) : null;
        const prefetchP = frameSrc.prefetch(nextT);

        drawComposite(ctx, state.plan, state.assets.snapshot, state.assets.snapshotPr, geom, frameSrc.video(), staticLayers);

        const frame = new VideoFrame(canvas, {
          timestamp: Math.round((i * 1e6) / FPS),
          duration: Math.round(1e6 / FPS),
        });
        venc.encode(frame, { keyFrame: i % (FPS * 2) === 0 });
        frame.close();

        // Wait for the prefetch (usually already done by now), swap buffers.
        if (prefetchP) await prefetchP;
        if (nextT != null) await frameSrc.advance(nextT);

        // Event-driven backpressure keeps the hardware encoder saturated
        // without a fixed sleep spin.
        await encoderBackpressure(venc, 16);

        if (i % 5 === 0 || i === tl.totalFrames - 1) {
          onProgress && onProgress(3 + (i / (tl.totalFrames - 1 || 1)) * 62);
        }
      }

      await venc.flush();
    } finally {
      try { if (venc.state !== "closed") venc.close(); } catch (_) {}
      frameSrc.destroy();
    }

    onProgress && onProgress(68);

    muxer.finalize();
    return new Blob([target.buffer], { type: "video/mp4" });
  }

  // ─────────────────────────────────────────────────────────────
  // Fallback path: MediaRecorder realtime capture
  // ─────────────────────────────────────────────────────────────
  async function exportViaMediaRecorder(state, onProgress) {
    const geom = state.geom, tl = state.tl;

    const mimeCandidates = [
      'video/mp4;codecs="avc1.42E01E,mp4a.40.2"',
      "video/mp4",
      'video/webm;codecs="vp9,opus"',
      "video/webm",
    ];
    let mimeType = null;
    for (const m of mimeCandidates) {
      if (MediaRecorder.isTypeSupported(m)) { mimeType = m; break; }
    }
    if (!mimeType) throw new Error("MediaRecorder has no supported container.");

    const canvas = document.createElement("canvas");
    canvas.width = geom.width;
    canvas.height = geom.height;
    const ctx = canvas.getContext("2d", { alpha: false });

    // Same cached static layers as the WebCodecs path — banners pinned to
    // the output frame edges (matching the server FFmpeg export).
    const staticLayers = buildStaticLayers(
      state.plan, state.assets.snapshot, state.assets.snapshotPr,
      geom, state.plan.imgCache, state.assets.banners, state.bars
    );

    const stream = canvas.captureStream(FPS);

    // Live playback element with its audio routed into the recording
    const live = makeSeekableVideo(state.videoUrl);
    let audioCtx = null;
    try {
      await waitForEvent(live, "loadedmetadata", 20000);
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) {
        audioCtx = new AC();
        const srcNode = audioCtx.createMediaElementSource(live);
        const dest = audioCtx.createMediaStreamDestination();
        srcNode.connect(dest);
        for (const t of dest.stream.getAudioTracks()) stream.addTrack(t);
        // NOT connected to speakers: avoids double playback noise.
      }
    } catch (_) { /* proceed without audio track */ }

    const rec = new MediaRecorder(stream, { mimeType: mimeType, videoBitsPerSecond: 12000000 });
    const parts = [];
    rec.ondataavailable = function (e) { if (e.data && e.data.size) parts.push(e.data); };
    const stopped = new Promise((resolve, reject) => {
      rec.onstop = resolve;
      rec.onerror = function () { reject(new Error("MediaRecorder failed.")); };
    });

    await seekVideo(live, tl.start);
    rec.start(500);
    live.playbackRate = tl.speed;
    try { await live.play(); } catch (_) {
      live.muted = true;
      await live.play();
    }

    const outMs = ((tl.end - tl.start) / tl.speed) * 1000 + 3000;
    const deadline = performance.now() + outMs;
    await new Promise((resolve) => {
      function tick() {
        drawComposite(ctx, state.plan, state.assets.snapshot, state.assets.snapshotPr, geom, live, staticLayers);
        onProgress && onProgress(3 + clamp((live.currentTime - tl.start) / (tl.end - tl.start || 1), 0, 1) * 62);
        if (live.currentTime >= tl.end || live.ended || performance.now() > deadline) resolve();
        else requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    });

    live.pause();
    if (audioCtx) { try { audioCtx.close(); } catch (_) {} }
    rec.stop();
    await stopped;

    const isMp4 = mimeType.indexOf("video/mp4") === 0;
    if (!isMp4) {
      state.warnings.push("Browser lacked WebCodecs/MP4 recording — exported as WebM instead.");
    }
    return new Blob(parts, { type: isMp4 ? "video/mp4" : "video/webm" });
  }

  // ─────────────────────────────────────────────────────────────
  // Public entry
  // ─────────────────────────────────────────────────────────────
  async function exportScene(options) {
    const onProgress = options && options.onProgress;
    const bridge = window.__AQ_CANVAS_BRIDGE__;
    if (!bridge) throw new Error("Canvas bridge is not initialized.");

    const scene = bridge.getScene();
    const mainVideo = bridge.getVideoEl();
    if (!mainVideo || !mainVideo.currentSrc) throw new Error("No video loaded.");

    const warnings = [];
    mainVideo.pause();

    if (!mainVideo.videoWidth) await waitForEvent(mainVideo, "loadedmetadata", 15000);
    const duration = Number.isFinite(mainVideo.duration)
      ? mainVideo.duration
      : (scene.video.duration || 0);
    if (!(duration > 0)) throw new Error("Could not determine video duration.");

    const geom = computeOutput(bridge, scene, {
      videoWidth: mainVideo.videoWidth,
      videoHeight: mainVideo.videoHeight,
    });
    const tl = computeTimeline(scene, duration);

    onProgress && onProgress(1);
    const assets = await prepareAssets(bridge, scene, geom);

    const state = {
      bridge: bridge,
      scene: scene,
      geom: geom,
      tl: tl,
      warnings: warnings,
      videoUrl: mainVideo.currentSrc,
      plan: buildDrawPlan(bridge, scene, geom),
      assets: assets,
      // Baked-in letterbox bars kept for parity/debugging only.
      // Banners are placed flush to the video edges (bar offset 0), so
      // this is not used to offset placement; it mirrors what
      // the preview reports for diagnostics.
      bars: (bridge.contentBars && bridge.contentBars()) || { top: 0, bottom: 0 },
    };
    state.plan.imgCache = assets.imgCache;

    let blob, type, ext;
    if (supportsWebCodecs() && window.Mp4Muxer) {
      blob = await exportViaWebCodecs(state, onProgress);
      type = "video/mp4"; ext = ".mp4";
    } else if (supportsMediaRecorder()) {
      warnings.push("WebCodecs unavailable — using realtime capture (plays through the clip once).");
      blob = await exportViaMediaRecorder(state, onProgress);
      const isMp4 = (blob.type || "").indexOf("video/mp4") === 0;
      type = isMp4 ? "video/mp4" : "video/webm";
      ext = isMp4 ? ".mp4" : ".webm";
    } else {
      throw new Error("This browser supports neither WebCodecs nor MediaRecorder.");
    }

    if (!(blob instanceof Blob) || !blob.size) throw new Error("Encoding produced no data.");

    return {
      blob: blob,
      type: type,
      filename: getBaseName(bridge.getFilename()) + "_edited" + ext,
      warnings: warnings.filter(Boolean),
    };
  }

  function getBaseName(filename) {
    return String(filename || "video").replace(/\.[^.]+$/, "") || "video";
  }

  window.__AQ_CLIENT_EXPORT__ = {
    exportScene: exportScene,
    isSupported: function () {
      return !!window.__AQ_CANVAS_BRIDGE__ &&
             typeof window.Mp4Muxer !== "undefined" &&
             (supportsWebCodecs() || supportsMediaRecorder());
    },
  };
})();







