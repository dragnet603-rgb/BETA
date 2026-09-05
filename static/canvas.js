/**
 * AUTOQUENCE V3 — FULL ARCHITECTURAL REWORK
 * ==========================================
 *
 * CANONICAL SCENE MODEL (single source of truth):
 *
 *   scene = {
 *     version: Number,
 *     canvas:  { width, height, aspectRatio, background },
 *     video:   { naturalWidth, naturalHeight, duration, filename,
 *                crop: { x:0-1, y:0-1, w:0-1, h:0-1, applied:bool } },
 *     elements: [ Element ],
 *     refs: { lastCreatedId, lastReferencedId }
 *   }
 *
 *   Element = {
 *     id:       String,
 *     type:     "banner" | "text" | "shape" | "image" | "logo",
 *     role:     String | null,
 *     parentId: String | null,
 *     x:        0-1  (relative to parent or canvas),
 *     y:        0-1,
 *     width:    0-1,
 *     height:   0-1,
 *     zIndex:   Number,
 *     props:    { ... type-specific styling ... }
 *   }
 *
 * COORDINATE SYSTEM:
 *   All element positions are NORMALIZED (0-1) relative to their parent.
 *   Conversion to pixels only happens in the renderer.
 *   Video crop is normalized relative to the source video frame.
 *
 * RENDERING:
 *   renderScene()        — full render (video + elements)
 *   renderVideo()        — only when crop/aspect changes (guarded by key)
 *   renderElements()     — reconciles DOM/Konva nodes with scene.elements
 *
 * ACTION PIPELINE:
 *   AI returns scene + actions → _adoptServerScene() → renderScene()
 *   All actions mutate scene.elements only (except crop_video/resize_video)
 *
 * INVARIANT:
 *   NO element action may mutate scene.video or scene.canvas.crop.
 */

document.addEventListener("DOMContentLoaded", () => {

  // ─────────────────────────────────────────────────────────────
  // DOM
  // ─────────────────────────────────────────────────────────────
  const videoEl      = document.getElementById("videoPreview");
  const form         = document.getElementById("promptForm");
  const container    = document.querySelector(".canvas");
  const expBtn       = document.getElementById("Export");
  const playPauseBtn = document.getElementById("playPauseBtn");
  const konvaStage   = document.getElementById("konvaStage");
  const topBannerBtn = document.getElementById("topBannerBtn");

  if (!videoEl || !container) return;

  // ─────────────────────────────────────────────────────────────
  // KONVA STAGE
  // ─────────────────────────────────────────────────────────────
  const stage = new Konva.Stage({
    container: "konvaStage",
    width:  container.clientWidth,
    height: container.clientHeight,
  });
  const cropLayer  = new Konva.Layer();
  const elemLayer  = new Konva.Layer();
  stage.add(cropLayer);
  stage.add(elemLayer);
  cropLayer.visible(false);
  konvaStage.style.display = "none";

  // ─────────────────────────────────────────────────────────────
  // CANONICAL SCENE STATE
  // ─────────────────────────────────────────────────────────────
  let scene = createEmptyScene();

  function createEmptyScene() {
    return {
      version: 0,
      canvas: {
        width:       1080,
        height:      1920,
        aspectRatio: null,
        background:  null,
        speed:       1.0,
        trim:        null,
      },
      video: {
        naturalWidth:  null,
        naturalHeight: null,
        duration:      null,
        filename:      getFilename(),
        crop: { x: 0, y: 0, w: 1, h: 1, applied: false },
      },
      elements: [],
      refs: { lastCreatedId: null, lastReferencedId: null },
    };
  }

  // Guard: only re-apply video geometry when crop actually changes
  let _lastCropKey = null;
  function _cropKey() {
    const c = scene.video.crop;
    return `${c.applied}:${c.x},${c.y},${c.w},${c.h}`;
  }

  // ─────────────────────────────────────────────────────────────
  // ANIMATION STATE
  // _visualCrop — temporary interpolated crop used ONLY while the
  // crop/framing animation runs. Never committed to scene; when the
  // animation ends (or is cancelled) it is cleared and renderVideo()
  // re-applies the authoritative scene.video.crop.
  // _animGen — generation token: bumped whenever animations are
  // cancelled (undo / redo / drag / new prompt) so in-flight animation
  // batches become no-ops and the latest scene state always wins.
  // ─────────────────────────────────────────────────────────────
  let _visualCrop = null;
  let _cropAnimating = false;
  let _animGen = 0;

  function currentCrop() {
    return _visualCrop || scene.video.crop;
  }

  /** Cancel all running edit animations and re-render the authoritative scene. */
  function _cancelAnimations() {
    _animGen++;
    if (window.AQAnim) window.AQAnim.cancelAll();
    _visualCrop = null;
    _cropAnimating = false;
    renderScene();
  }

  // ─────────────────────────────────────────────────────────────
  // HISTORY
  // ─────────────────────────────────────────────────────────────
  const MAX_HIST = 50;
  let history   = [];
  let histIdx   = -1;

  function saveHistory() {
    history = history.slice(0, histIdx + 1);
    history.push(JSON.parse(JSON.stringify(scene)));
    if (history.length > MAX_HIST) history = history.slice(-MAX_HIST);
    histIdx = history.length - 1;
    _refreshUndoUI();
  }

  function undo() {
    if (histIdx <= 0) return;
    _cancelAnimations(); // kill in-flight AI animations; latest state wins
    histIdx--;
    scene = JSON.parse(JSON.stringify(history[histIdx]));
    _lastCropKey = null;
    renderScene();
    _refreshUndoUI();
  }

  function redo() {
    if (histIdx >= history.length - 1) return;
    _cancelAnimations();
    histIdx++;
    scene = JSON.parse(JSON.stringify(history[histIdx]));
    _lastCropKey = null;
    renderScene();
    _refreshUndoUI();
  }

  function _refreshUndoUI() {
    const u = document.getElementById("undoBtn");
    const r = document.getElementById("redoBtn");
    if (u) u.disabled = histIdx <= 0;
    if (r) r.disabled = histIdx >= history.length - 1;
  }

  // ─────────────────────────────────────────────────────────────
  // HELPERS
  // ─────────────────────────────────────────────────────────────
  function genId(prefix = "el") {
    return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2,6)}`;
  }

  function getFilename() {
    const src = (videoEl?.querySelector("source") || videoEl)?.src || "";
    return src.split("/").pop().split("?")[0];
  }

 

  /**
   * showMsg(txt, opts) — AI response bubble.
   * opts.type:    "success" | "error" | "info"   (visual variant)
   * opts.sticky:  true → never auto-hide (clarifications, choices, errors)
   * opts.ms:      auto-hide delay (default 9000ms)
   */
  function showMsg(txt, opts = {}) {
    if (!txt) return;
    const b = document.getElementById("autoquence-response");
    if (!b) return;

    b.textContent = "";
    const span = document.createElement("span");
    span.className = "aq-response-text";
    span.textContent = txt;
    b.appendChild(span);

    // Close button
    const x = document.createElement("button");
    x.className = "aq-response-close";
    x.setAttribute("aria-label", "Dismiss");
    x.textContent = "×";
    x.addEventListener("click", () => _hideResponseBox());
    b.appendChild(x);

    b.classList.remove("aq-success", "aq-error", "aq-info", "aq-thinking", "aq-visible");
    clearInterval(_thinkTimer);
    _thinkTimer = null;
    b.classList.add(opts.type === "error" ? "aq-error" : opts.type === "success" ? "aq-success" : "aq-info");

    // Force reflow so the entrance animation replays every time
    void b.offsetWidth;
    b.style.display = "block";
    b.classList.add("aq-visible");

    clearTimeout(b._t);
    if (!opts.sticky) {
      b._t = setTimeout(() => _hideResponseBox(), opts.ms || 6000);
    }
  }

  function _hideResponseBox() {
    const b = document.getElementById("autoquence-response");
    if (!b) return;
    clearTimeout(b._t);
    b.classList.remove("aq-visible");
    setTimeout(() => { b.style.display = "none"; }, 40); // match CSS exit transition
  }

  /**
   * showThinking(text) — "thinking…" state shown while the AI works.
   * Rotates through status lines so the wait feels alive.
   */
  const THINKING_LINES = [
    "Thinking…",
    "Understanding your prompt…",
    "Untangling",
  ];
  let _thinkTimer = null;
  function showThinking(text) {
    const b = document.getElementById("autoquence-response");
    if (!b) return;
    clearTimeout(b._t);
    clearInterval(_thinkTimer);

    b.textContent = "";
    const dot = document.createElement("span");
    dot.className = "aq-thinking-dot";
    const span = document.createElement("span");
    span.className = "aq-response-text";
    span.textContent = text || THINKING_LINES[0];
    b.appendChild(dot);
    b.appendChild(span);

    let i = 1;
    _thinkTimer = setInterval(() => {
      i = (i + 2) % THINKING_LINES.length;
      span.textContent = text || THINKING_LINES[i];
    }, 2200);

    b.classList.remove("aq-success", "aq-error");
    b.classList.add("aq-info", "aq-thinking");
    void b.offsetWidth;
    b.style.display = "block";
    b.classList.add("aq-visible");
  }

  function hideThinking() {
    clearInterval(_thinkTimer);
    _thinkTimer = null;
    const b = document.getElementById("autoquence-response");
    if (b?.classList.contains("aq-thinking")) _hideResponseBox();
  }

  // ─────────────────────────────────────────────────────────────
  // EDIT OVERLAY PHASES
  // The overlay cycles through "generation" phases while the AI
  // works. Each transition plays a letter-scramble decode so the
  // text itself feels like it's being generated.
  // ─────────────────────────────────────────────────────────────
  const OVERLAY_PHASES = [
    "Analyzing your video…",
    "Reading your prompt…",
    "Planning edits…",
    "Applying changes…",
  ];

  const SCRAMBLE_CHARS = "!<>-_\\/[]{}—=+*^?#";
  const _reduceMotion =
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let _phaseTimer = null;
  let _phaseIndex = 0;
  let _scrambleRaf = null;

  function _setOverlayText(text) {
    const el = document.getElementById("editOverlayText");
    if (el) el.textContent = text;
  }

  /** Scramble-decode `text` into #editOverlayText over ~450ms. */
  function _scrambleTo(text) {
    if (_scrambleRaf) cancelAnimationFrame(_scrambleRaf);
    if (_reduceMotion) { _setOverlayText(text); return; }

    const el = document.getElementById("editOverlayText");
    if (!el) return;

    const started = performance.now();
    const DURATION = 450;

    const tick = (now) => {
      const t = Math.min(1, (now - started) / DURATION);
      // Number of characters locked in (left-to-right reveal).
      const settled = Math.floor(text.length * t);
      let out = text.slice(0, settled);

      for (let i = settled; i < text.length; i++) {
        out += SCRAMBLE_CHARS[(Math.random() * SCRAMBLE_CHARS.length) | 0];
      }

      el.textContent = out;
      if (t < 1) {
        _scrambleRaf = requestAnimationFrame(tick);
      } else {
        el.textContent = text;
        _scrambleRaf = null;
      }
    };

    _scrambleRaf = requestAnimationFrame(tick);
  }

  function showEditOverlay() {
    document.getElementById("editOverlay")?.classList.add("active");
    container.classList.add("processing");

    // Start cycling through generation phases.
    _phaseIndex = 0;
    _setOverlayText(OVERLAY_PHASES[0]);
    clearInterval(_phaseTimer);
    if (!_reduceMotion) {
      _phaseTimer = setInterval(() => {
        _phaseIndex = (_phaseIndex + 1) % OVERLAY_PHASES.length;
        _scrambleTo(OVERLAY_PHASES[_phaseIndex]);
      }, 1800);
    }
  }
  function hideEditOverlay() {
    document.getElementById("editOverlay")?.classList.remove("active");
    container.classList.remove("processing");

    // Stop phases and reset for next time.
    clearInterval(_phaseTimer);
    _phaseTimer = null;
    if (_scrambleRaf) { cancelAnimationFrame(_scrambleRaf); _scrambleRaf = null; }
    _setOverlayText(OVERLAY_PHASES[0]);
  }

  // ─────────────────────────────────────────────────────────────
  // FONTS
  // ─────────────────────────────────────────────────────────────
  const _loadedFonts = new Set();
  const GFONTS = {
    "Inter":       "Inter:wght@400;700;900",
    "Montserrat":  "Montserrat:wght@400;700;900",
    "Poppins":     "Poppins:wght@400;700;900",
    "Roboto":      "Roboto:wght@400;700;900",
    "Oswald":      "Oswald:wght@400;700",
    "Raleway":     "Raleway:wght@400;700;900",
    "Open Sans":   "Open+Sans:wght@400;700;900",
    "Lato":        "Lato:wght@400;700;900",
    "Nunito":      "Nunito:wght@400;700;900",
    "Bebas Neue":  "Bebas+Neue",
    "Anton":       "Anton",
  };
  const SYSTEM_FONTS = new Set([
    "Arial","Arial Black","Impact","Georgia","Verdana","Trebuchet MS",
    "Times New Roman","Courier New","Tahoma","sans-serif","serif","monospace",
  ]);

  async function ensureFont(name) {
    if (!name) return;
    const base = name.split(",")[0].trim().replace(/['"]/g, "");
    if (_loadedFonts.has(base)) return;
    if (SYSTEM_FONTS.has(base)) { _loadedFonts.add(base); return; }
    const gkey = GFONTS[base];
    if (gkey) {
      const url = `https://fonts.googleapis.com/css2?family=${gkey}&display=swap`;
      if (!document.querySelector(`link[data-font="${base}"]`)) {
        const lnk = Object.assign(document.createElement("link"),
          { rel: "stylesheet", href: url });
        lnk.setAttribute("data-font", base);
        document.head.appendChild(lnk);
      }
      try { await document.fonts.load(`bold 10px "${base}"`); } catch(_) {}
    }
    _loadedFonts.add(base);
  }

  /** Return CSS font-family string, properly quoted */
  function cssFontFamily(name) {
    if (!name) return "Arial, sans-serif";
    const base = String(name).split(",")[0].trim().replace(/['"]/g, "");
    if (SYSTEM_FONTS.has(base)) return base + ", Arial, sans-serif";
    return `"${base}", Arial, sans-serif`;
  }
  

  // ─────────────────────────────────────────────────────────────
  // COORDINATE UTILITIES
  // ─────────────────────────────────────────────────────────────
  /**
   * getVideoRect() — returns the pixel rect of the visible VIDEO AREA
   * inside the container (FIT/contain mode).
   *
   * The video is always fully visible (never zoom-cropped): an uncropped
   * video is letterboxed inside the container, and a committed crop shows
   * the whole crop selection fitted and centered. Bars around the video
   * are filled by the container background (scene.canvas.background).
   * Elements (text/shape/banner) are positioned relative to THIS rect,
   * so they track the video, not the canvas.
   */
  function getVideoRect() {
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    const vw = scene.video.naturalWidth  || videoEl.videoWidth  || cw;
    const vh = scene.video.naturalHeight || videoEl.videoHeight || ch;
    if (!vw || !vh) return { x: 0, y: 0, w: cw, h: ch };
    // While the crop rectangle preview is open we show the FULL frame
    if (_cropPreviewing) {
      return _containRect(cw, ch, vw, vh);
    }
    const crop = currentCrop();
    if (crop && crop.applied) {
      // FIT the committed crop selection inside the container (centered).
      // IMPORTANT: this must return the rect of the VISIBLE SELECTION —
      // exactly what renderVideo() paints after clipping — not the origin
      // of the scaled full frame. renderVideo() centers the selection at
      // (cw/2, ch/2) and clips to it, so the visible picture occupies
      // exactly cropW*s × cropH*s centered in the container. Returning
      // anything else (e.g. the full frame's top/left) makes banners snap
      // to the canvas edge instead of the video picture edge.
      const cropW = crop.w * vw;
      const cropH = crop.h * vh;
      const s = Math.min(cw / cropW, ch / cropH);
      return {
        x: cw / 2 - (cropW / 2) * s,
        y: ch / 2 - (cropH / 2) * s,
        w: cropW * s,
        h: cropH * s,
      };
    }
    return _containRect(cw, ch, vw, vh);
  }

  function _containRect(cw, ch, vw, vh) {
    const cr = vw / vh, br = cw / ch;
    let w, h;
    if (cr > br) { w = cw; h = cw / cr; }
    else         { h = ch; w = ch * cr; }
    return { x: (cw - w) / 2, y: (ch - h) / 2, w, h };
  }

  /**
   * detectContentBars() — content-aware edge detection.
   *
   * getVideoRect() knows where the VIDEO AREA starts, but some source
   * videos carry BLACK BARS BAKED INTO their own pixels (e.g. a 16:9 clip
   * with its own letterbox, or padded phone footage). This scans the
   * currently displayed frame (respecting a committed crop) and returns
   * the thickness of those baked-in bars as fractions of the displayed
   * video height:
   *
   *   { top: 0.0–0.4, bottom: 0.0–0.4 }   (0 = no bar → no snapping)
   *
   * A row counts as "bar" when its average luma is near-black (< 24) —
   * bars are compared against the row's own content, not a fixed color,
   * so slightly-off-black padding still reads as a bar.
   *
   * The result is cached per (video source, dimensions, crop state) so
   * re-renders don't rescan pixels every frame. If the frame can't be
   * read (no data yet, tainted canvas, …) the result is all zeros and
   * banner placement behaves exactly as before.
   */
  const _contentBarCache = { key: null, top: 0, bottom: 0 };
  function detectContentBars() {
    const v = videoEl;
    if (!v) return { top: 0, bottom: 0 };
    const crop = currentCrop();
    const cropKey = crop && crop.applied
      ? [crop.x, crop.y, crop.w, crop.h].join(",")
      : "";
    // Include a coarse playhead window in the cache key. A single capture
    // measures only the frame currently on screen; without the time
    // component a frozen scan (from a dark first frame, a fade, etc.) would
    // be reused forever. Bucketing by ~30s keeps scans cheap while
    // guaranteeing we re-measure as the video plays / when the user seeks.
    const timeBucket = Math.floor((Number.isFinite(v.currentTime) ? v.currentTime : 0) / 30);
    const key = [
      v.currentSrc || v.src || "",
      v.videoWidth, v.videoHeight,
      cropKey,
      timeBucket,
    ].join("|");

    if (_contentBarCache.key === key) return _contentBarCache;

    const res = { top: 0, bottom: 0 };
    try {
      const vw = v.videoWidth, vh = v.videoHeight;
      if (vw && vh && v.readyState >= 2) {
        const W = 96;
        const H = Math.max(1, Math.round(W * vh / vw));
        const c = document.createElement("canvas");
        c.width = W; c.height = H;
        const ctx = c.getContext("2d", { willReadFrequently: true });
        let sx = 0, sy = 0, sw = vw, sh = vh;
        if (crop && crop.applied) {
          sx = crop.x * vw; sy = crop.y * vh;
          sw = crop.w * vw; sh = crop.h * vh;
        }
        ctx.drawImage(v, sx, sy, sw, sh, 0, 0, W, H);
        const data = ctx.getImageData(0, 0, W, H).data;
        const rowLum = y => {
          let s = 0;
          for (let x = 0; x < W; x++) {
            const i = (y * W + x) * 4;
            s += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
          }
          return s / W;
        };
        const BAR_LUMA = 16;                 // TRUE black = bar (higher
                                             // thresholds false-positive on
                                             // dark scenes and push the
                                             // banner down / overlap content)
        const MIN_BAR   = 0.03;              // ignore slivers < 3% of height
        const MAX_SCAN  = Math.floor(H * 0.4); // never eat more than 40%
        const EDGE_JUMP = 40;                // luma must RISE by this much at
                                             // the bar->content boundary; a
                                             // dark scene or fade-in is a
                                             // gradient, not a bar, so it
                                             // must NOT push the banner down
        // Measure a top bar as the run of near-black rows that ends in a
        // sharp luma jump into real content. This is what separates a baked
        // letterbox / black bar (hard edge) from merely dark footage.
        let top = 0;
        while (top < MAX_SCAN && rowLum(top) < BAR_LUMA) top++;
        if (top > 0 && top < H) {
          // The scan stopped because rowLum(top) reached BAR_LUMA; confirm a
          // hard edge (row above is near-black, first content row is clearly
          // brighter). Without the jump it's a dark gradient/fade -> no bar.
          const lastDark = Math.max(0, top - 1);
          const firstLight = Math.min(H - 1, top);
          if (rowLum(firstLight) - rowLum(lastDark) < EDGE_JUMP) top = 0;
        }
        let bot = 0;
        while (bot < MAX_SCAN && rowLum(H - 1 - bot) < BAR_LUMA) bot++;
        if (bot > 0 && bot < H) {
          const lastDark = Math.max(0, H - bot);
          const firstLight = Math.min(H - 1, H - bot - 1);
          if (rowLum(firstLight) - rowLum(lastDark) < EDGE_JUMP) bot = 0;
        }
        res.top    = top / H >= MIN_BAR ? top / H : 0;
        res.bottom = bot / H >= MIN_BAR ? bot / H : 0;
      }
    } catch (err) {
      // Tainted canvas or transient failure → no snapping, legacy behavior.
      console.warn("[BARS] content scan failed:", err);
    }

    // Only cache once we actually scanned a frame. If the video has no
    // data yet we return zeros WITHOUT caching, so the next render (after
    // loadeddata) rescans and snaps correctly.
    if (v.videoWidth && v.videoHeight && v.readyState >= 2) {
      _contentBarCache.key = key;
      _contentBarCache.top = res.top;
      _contentBarCache.bottom = res.bottom;
    }
    return _contentBarCache.key === key ? _contentBarCache : res;
  }

  /**
   * bannerSnapY() — the Y a banner should be painted at.
   *
   * FLUSH EDGE: banners are always pinned flush to the edge of the rendered
   * VIDEO PICTURE rect: a top banner sits on vr.y (the very top of the
   * picture) and a bottom banner on vr.y + vr.h - bannerH (the very bottom),
   * even when the source pixels carry black bars. This matches the client
   * export engine and the server FFmpeg path (both use bar offset 0), so the
   * banner is pixel-identical across preview and export.
   */
  function bannerSnapY(isCropped, isBot, vr, bannerH) {
    // Always flush to the video edge, matching the client export engine and
    // the server FFmpeg path (both set the baked-bar offset to 0). A top
    // banner touches the very top of the rendered video and a bottom banner
    // touches the very bottom, even when the source carries black bars.
    if (isBot) {
      return Math.max(vr.y, vr.y + vr.h - bannerH);
    }
    return vr.y;
  }


  /**
   * getWorldRect(element) — returns pixel { x, y, w, h } in container coords
   * for any element, resolving parent chain.
   */
  function getWorldRect(el) {
    const parentRect = el.parentId
      ? (() => {
          const p = scene.elements.find(e => e.id === el.parentId);
          return p ? getWorldRect(p) : getVideoRect();
        })()
      : getVideoRect();

    return {
      x: parentRect.x + el.x * parentRect.w,
      y: parentRect.y + el.y * parentRect.h,
      w: el.width  * parentRect.w,
      h: el.height * parentRect.h,
    };
  }

  /** Convert pixel {x,y} (within container) to normalized coords relative to parentId */
  function pixelToNorm(px, py, parentId) {
    const p = parentId
      ? (() => { const pe = scene.elements.find(e => e.id === parentId); return pe ? getWorldRect(pe) : getVideoRect(); })()
      : getVideoRect();
    return {
      x: (px - p.x) / p.w,
      y: (py - p.y) / p.h,
    };
  }

  // ─────────────────────────────────────────────────────────────
  // VIDEO RENDERER  (ISOLATED — only runs when crop key changes)
  // ─────────────────────────────────────────────────────────────
 function renderVideo(cropOverride) {
  if (!cropOverride) {
    const key = _cropKey();
    if (key === _lastCropKey) return;
    _lastCropKey = key;
  }

  const crop = cropOverride || currentCrop();
  const cw = container.clientWidth;
  const ch = container.clientHeight;

  if (!crop.applied || _cropPreviewing) {
    Object.assign(videoEl.style, {
      position: "",
      width: "",
      height: "",
      left: "",
      top: "",
      // FIT mode: the video is always fully visible (letterboxed when
      // its aspect differs from the canvas). Bars are the container
      // background — matching exports, which fit the picture inside a
      // fixed 9:16 frame and pad with the background color.
      objectFit: "contain",
      transform: "",
      margin: "",
      clipPath: "",
    });

    container.style.overflow = "";
    return;
  }

  const vw = videoEl.videoWidth || scene.video.naturalWidth;
  const vh = videoEl.videoHeight || scene.video.naturalHeight;

  if (!vw || !vh) return;

  // Crop rectangle in ORIGINAL VIDEO coordinates
  const cropX = crop.x * vw;
  const cropY = crop.y * vh;
  const cropW = crop.w * vw;
  const cropH = crop.h * vh;

  // Scale so the crop selection FITS inside the canvas (contain — the
  // whole selection is always visible, letterboxed with background bars).
  // A clip-path below chops everything OUTSIDE the selection (the rest of
  // the rendered source frame) — matching exports, which fit the picture
  // inside the fixed 9:16 frame and pad with the background color.
  const scale = Math.min(
    cw / cropW,
    ch / cropH
  );

  const renderedW = vw * scale;
  const renderedH = vh * scale;

  // Center the CENTER of the selected crop
  // exactly in the canvas.
  const cropCenterX = cropX + cropW / 2;
  const cropCenterY = cropY + cropH / 2;

  const left =
    cw / 2 -
    cropCenterX * scale;

  const top =
    ch / 2 -
    cropCenterY * scale;

  // Clip away everything OUTSIDE the selection (in rendered-video pixels)
  const clipTop    = cropY * scale;
  const clipLeft   = cropX * scale;
  const clipRight  = (vw - cropX - cropW) * scale;
  const clipBottom = (vh - cropY - cropH) * scale;

  Object.assign(videoEl.style, {
    position: "absolute",
    width: `${renderedW}px`,
    height: `${renderedH}px`,
    left: `${left}px`,
    top: `${top}px`,
    objectFit: "fill",
    transform: "none",
    clipPath: `inset(${clipTop}px ${clipRight}px ${clipBottom}px ${clipLeft}px)`,
  });

  container.style.overflow = "hidden";

  console.log("[VIDEO CROP]", {
    crop,
    sourceCrop: {
      x: cropX,
      y: cropY,
      w: cropW,
      h: cropH,
    },
    rendered: {
      width: renderedW,
      height: renderedH,
      left,
      top,
    }
  });
}

  // ─────────────────────────────────────────────────────────────
  // ELEMENT NODE MAPS  (scene element id → rendered node)
  // ─────────────────────────────────────────────────────────────
  const domNodes   = new Map(); // id → HTMLElement
  const konvaNodes = new Map(); // id → Konva.Group

  // ─────────────────────────────────────────────────────────────
  // MAIN RENDERER
  // ─────────────────────────────────────────────────────────────
  function renderScene() {
    // Background
    container.style.background = scene.canvas.background || "";

    // Video geometry (guarded)
    renderVideo();

    // Elements
    renderElements();

    // Konva stage visibility: show if banners exist OR crop is active
    const hasKonva = scene.elements.some(e => e.type === "banner");
    if (hasKonva || _cropBoxActive) {
      konvaStage.style.display = "block";
      stage.width(container.clientWidth);
      stage.height(container.clientHeight);
      if (hasKonva) {
        elemLayer.moveToTop();
        elemLayer.batchDraw();
      }
    } else {
      konvaStage.style.display = "none";
    }
  }

  function renderElements() {
    const liveIds = new Set(scene.elements.map(e => e.id));

    // Remove orphaned DOM nodes (snapshot keys first to avoid mutation-while-iterating)
    for (const id of [...domNodes.keys()]) {
      if (!liveIds.has(id)) {
        domNodes.get(id)?.remove();
        domNodes.delete(id);
        console.log("[RENDER] Removed DOM node:", id);
      }
    }
    // Remove orphaned Konva nodes
    let konvaChanged = false;
    for (const id of [...konvaNodes.keys()]) {
      if (!liveIds.has(id)) {
        konvaNodes.get(id)?.destroy();
        konvaNodes.delete(id);
        konvaChanged = true;
        console.log("[RENDER] Destroyed Konva node:", id);
      }
    }
    if (konvaChanged) elemLayer.batchDraw();

    // Render sorted by zIndex
    const sorted = [...scene.elements].sort((a, b) => (a.zIndex || 0) - (b.zIndex || 0));
    for (const el of sorted) _renderElement(el);
  }

  function _renderElement(el) {
    switch (el.type) {
      case "text":              _renderText(el);   break;
      case "banner":            _renderBanner(el); break;
      case "shape":             _renderShape(el);  break;
      case "image": case "logo":_renderImage(el);  break;
      default: console.warn("[RENDER] Unknown type:", el.type);
    }
  }

  // ── Text ────────────────────────────────────────────────────
  function _renderText(el) {
    const p    = el.props || {};

    // Text parented to a banner is drawn BY the banner (Konva).
    // Never render it as a separate DOM node — that caused the
    // long-standing overlap bug.
    if (el.parentId) {
      const parentEl = scene.elements.find(e => e.id === el.parentId);
      if (parentEl && parentEl.type === "banner") {
        const stale = domNodes.get(el.id);
        if (stale) { stale.remove(); domNodes.delete(el.id); }
        return;
      }
    }

    const rect = getWorldRect(el);

    let node = domNodes.get(el.id);
    if (!node) {
      node = document.createElement("div");
      node.dataset.elId = el.id;
      node.className = "aq-el aq-text";
      node.style.cursor = "move";
      container.appendChild(node);
      domNodes.set(el.id, node);
      _makeDraggable(node, el);
    }

    const fontFamily = cssFontFamily(p.fontFamily || p.font || "Arial");
    // Default font size reduced to 16px — was 28px which was too large
    const fontSize   = p.fontSize || p.font_size || outPxToPreviewPx(AQ_TYPO.text.defaultFs);
    const color      = p.color || p.textColor || p.text_color || "#ffffff";
    const fontWeight = p.fontWeight || p.font_weight || "bold";
    const textAlign  = p.textAlign || p.alignment || "center";
    const text       = p.text || p.content || "";

    const bgColor    = p.backgroundColor || p.background_color || null;
    const bgOpacity  = p.backgroundOpacity ?? p.background_opacity ?? (bgColor ? 0.7 : 0);

    Object.assign(node.style, {
      position:      "absolute",
      left:          `${rect.x}px`,
      top:           `${rect.y}px`,
      width:         `${rect.w}px`,
      height:        rect.h ? `${rect.h}px` : "auto",
      fontSize:      `${fontSize}px`,
      fontFamily,
      fontWeight,
      color,
      textAlign,
      // Tighter line height (was 1.3) and no extra letter spacing
      lineHeight:    String(p.lineHeight || 1.2),
      letterSpacing: p.letterSpacing ? `${p.letterSpacing}px` : "0px",
      padding:       `${p.padding || 4}px`,
      background:    bgColor ? hexAlpha(bgColor, bgOpacity) : (bgOpacity ? `rgba(0,0,0,${bgOpacity})` : "transparent"),
      borderRadius:  p.borderRadius ? `${p.borderRadius}px` : "4px",
      boxSizing:     "border-box",
      // Text z-index must beat konvaStage (z-index: 999) so text sits above banners
      zIndex:        String((el.zIndex || 0) + 1000),
      opacity:       String(p.opacity ?? 1),
      textShadow:    p.shadow ? "1px 1px 4px rgba(0,0,0,0.9)" : "",
      pointerEvents: "auto",
      userSelect:    "none",
      display:       "flex",
      alignItems:    p.verticalAlign === "bottom" ? "flex-end" : (p.verticalAlign === "top" ? "flex-start" : "center"),
      justifyContent: textAlign === "center" ? "center" : (textAlign === "right" ? "flex-end" : "flex-start"),
    });

    node.textContent = text;

    // Async font reload
    ensureFont(p.fontFamily || p.font).then(() => {
      node.style.fontFamily = cssFontFamily(p.fontFamily || p.font);
    });
  }

  // ============================================================
  // TYPOGRAPHY ENGINE
  //
  // Single source of truth for text sizing. ALL sizes are expressed
  // at OUTPUT resolution (scene.canvas.width, 1080-wide), then scaled
  // into preview px by kOut. Preview, the WebCodecs export engine and
  // the FFmpeg fallback all consume the same measured layout, so text
  // renders identically everywhere.
  // ============================================================
  const AQ_TYPO = {
    // "Middle" caption size at 1080-wide output — readable, not chunky.
    banner: { defaultFs: 56, minFs: 24, lineHeight: 1.15,
              padFrac: 0.45, minPad: 10, capFrac: 0.4 },
    text:   { defaultFs: 34, lineHeight: 1.25, pad: 8 },
  };
  window.__AQ_TYPO__ = AQ_TYPO;

  /** preview px -> output px factor (container and output are both 9:16) */
  function kOutFactor() {
    return (scene.canvas.width || 1080) / Math.max(1, container.clientWidth || 1080);
  }
  function outPxToPreviewPx(out) { return out / kOutFactor(); }
  function previewPxToOutPx(px)  { return px * kOutFactor(); }

  // Offscreen measuring node (never added to a layer — measurement only).
  const _measureNode = new Konva.Text({ listening: false, visible: false });

  /**
   * layoutBannerText(text, fontFamily, fontStyle, reqFsOut, cacheObj)
   *
   * Measures wrapped banner text at OUTPUT resolution and returns
   * { fontSizeOut, padOut, lineHeight, heightOut, lines, textWidthOut }.
   * Auto-shrinks until the banner fits capFrac of the output height.
   * Result is cached on cacheObj (keyed by text/font/size) so drag and
   * re-renders don't re-measure.
   */
  function layoutBannerText(text, fontFamily, fontStyle, reqFsOut, cacheObj) {
    const T = AQ_TYPO.banner;
    const OUT_W = scene.canvas.width || 1080;
    const OUT_H = scene.canvas.height || 1920;

    const key = `${text}|${fontFamily}|${fontStyle}|${Math.round(reqFsOut)}`;
    if (cacheObj && cacheObj._layoutKey === key && cacheObj._layout) {
      return cacheObj._layout;
    }

    const node = _measureNode;
    node.fontFamily(fontFamily);
    node.fontStyle(fontStyle);
    node.wrap("word");
    node.align("center");
    node.padding(0);

    // ── 6-WORDS-PER-LINE RULE ─────────────────────────────────
    // Split the text into lines of at most MAX_WORDS_PER_LINE words;
    // the next words start on the next line. Explicit \n breaks are
    // honored — each paragraph is chunked separately.
    const MAX_WORDS_PER_LINE = 8;
    const chunks = [];
    for (const para of String(text || " ").split("\n")) {
      const words = para.split(/\s+/).filter(Boolean);
      if (!words.length) { chunks.push(""); continue; }
      for (let i = 0; i < words.length; i += MAX_WORDS_PER_LINE) {
        chunks.push(words.slice(i, i + MAX_WORDS_PER_LINE).join(" "));
      }
    }
    if (!chunks.length) chunks = [" "];

    // Natural single-line width of a chunk at a given font size.
    function lineWidth(line, fs) {
      node.fontSize(fs);
      node.text(line || " ");
      node.width(null);
      node.height(null);
      node.getClientRect({ skipTransform: true });
      const w = node.getClientRect({ skipTransform: true }).width;
      return Number.isFinite(w) ? w : 0;
    }

    function layoutAt(fs) {
      const pad = Math.max(T.minPad, Math.round(T.padFrac * fs));
      const usable = Math.max(10, OUT_W - pad * 2);
      const widest = Math.max(...chunks.map(c => lineWidth(c, fs)));
      const height = Math.ceil(chunks.length * fs * T.lineHeight + pad * 2);
      return { pad, usable, widest, height };
    }

    let fs = Math.max(T.minFs, Math.round(reqFsOut));
    let m = layoutAt(fs);
    const capH = Math.max(60, Math.round(OUT_H * T.capFrac));

    // Shrink until the widest line fits the usable width AND the
    // banner fits the height cap.
    while ((m.widest > m.usable || m.height > capH) && fs > T.minFs) {
      fs = Math.max(T.minFs, Math.floor(fs * 0.85));
      m = layoutAt(fs);
    }

    let lines, heightOut, padOut, usable;
    if (m.widest > m.usable) {
      // ── FALLBACK ──
      // Still overflowing at the minimum size (very long words in a
      // 6-word chunk): let Konva word-wrap normally so nothing is
      // ever clipped. The 6-word rule degrades gracefully.
      node.fontSize(fs);
      node.text(text || " ");
      node.width(Math.max(10, OUT_W - 2 * T.minPad));
      node.lineHeight(T.lineHeight);
      node.height(null);
      node.getClientRect({ skipTransform: true });
      const h = node.getClientRect({ skipTransform: true }).height;
      lines = (node.textArr || []).map(l => l.text);
      padOut = T.minPad;
      usable = Math.max(10, OUT_W - 2 * T.minPad);
      heightOut = Math.ceil(
        (Number.isFinite(h) && h > 0 ? h : fs * T.lineHeight) + padOut * 2
      );
    } else {
      lines = chunks;
      padOut = m.pad;
      usable = m.usable;
      heightOut = m.height;
    }

    const layout = {
      fontSizeOut: fs,
      padOut: padOut,
      lineHeight: T.lineHeight,
      heightOut: heightOut,
      lines: lines,
      textWidthOut: usable,
    };
    if (cacheObj) { cacheObj._layoutKey = key; cacheObj._layout = layout; }
    return layout;
  }

  // ── Banner (Konva) ──────────────────────────────────────────
  //
  // Banner sits in the letterbox space directly above or below the video.
  // Height auto-expands to fit text. Text NEVER overflows because we
  // measure the FULL wrapped height with Konva's height() getter
  // (getTextHeight() only returns a single line's height and would
  // under-size the banner for multi-line text).
  //
  // ── Banner (Konva) ──────────────────────────────────────────
function _renderBanner(el) {

  const p = el.props || {};

  // ── CHILD TEXT ADOPTION ─────────────────────────────────────
  // The AI usually creates an empty banner + a separate "text"
  // element with parent_id pointing here. The banner must treat
  // that child's text as its own so measurement/auto-shrink and
  // export all work on the real sentence.
  // ============================================================
  const childTextEls = scene.elements.filter(
    e => e.type === "text" && e.parentId === el.id
  );
  const childProps =
    childTextEls.length
      ? (childTextEls[childTextEls.length - 1].props || {})
      : null;

  const bgColor =
    p.backgroundColor ||
    p.bg_color ||
    p.fill ||
    "#ffffff";

  const textColor =
    (childProps && (childProps.color || childProps.textColor || childProps.text_color)) ||
    p.color ||
    p.textColor ||
    p.text_color ||
    "#000000";

  const text =
    p.text ||
    p.content ||
    (childProps && (childProps.text || childProps.content)) ||
    "";

  const isBot =
    (p.position || "top")
      .toLowerCase()
      .includes("bottom");

  // The banner is always drawn with sharp (square) corners. It no longer
  // rounds its edges to match the canvas's rounded corners when it reaches
  // the rim of the canvas — the banner stays square everywhere, including
  // when it is flush against the top/bottom or inside a canvas corner.
  function applyBannerCorners() {
    bg.cornerRadius(0);
  }

  const LINE_H = AQ_TYPO.banner.lineHeight;

  const rawFont =
    p.fontFamily ||
    p.font ||
    (childProps && (childProps.fontFamily || childProps.font)) ||
    "Arial";

  const fontFamily = cssFontFamily(rawFont);

  const fontWeight =
    p.fontWeight ||
    p.font_weight ||
    "bold";

  const fontStyle =
    fontWeight === "bold"
      ? "bold"
      : "normal";

  // Requested banner font size, expressed at OUTPUT resolution.
  // Explicit sizes in props are preview px (legacy AI actions); the
  // default comes from the typography engine.
  const kOut = kOutFactor();
  const reqSizeRaw =
    p.fontSize ||
    p.font_size ||
    (childProps && (childProps.fontSize || childProps.font_size)) ||
    null;
  const REQ_FS_OUT = reqSizeRaw
    ? Math.max(1, Number(reqSizeRaw)) * kOut
    : AQ_TYPO.banner.defaultFs;

  // ==========================================================
  // VIDEO
  // ==========================================================

  const vr = getVideoRect();

  const bannerW = vr.w;
  const bannerX = vr.x;
  // Cleaned-band banner: a banner the server created onto the baked band
  // carries backgroundColor "transparent" and explicit y/height.
  // Pin it to the band and shrink the text to fit, like a normal banner.
  const isBandBanner =
    String(bgColor || "").toLowerCase() === "transparent" &&
    typeof el.y === "number" && typeof el.height === "number" &&
    el.height > 0.004 && el.height < 1;
  const bandHpx = isBandBanner ? Math.max(4, Math.round(el.height * vr.h)) : 0;

  function fitLayout(reqFs) {
    let req = reqFs;
    let lay = layoutBannerText(text, fontFamily, fontStyle, req, el);
    if (bandHpx > 0) {
      let guard = 0;
      while (Math.ceil(lay.heightOut / kOut) > bandHpx && req > 8 && guard++ < 12) {
        const over = Math.ceil(lay.heightOut / kOut) / bandHpx;
        req = Math.max(8, req / Math.max(1.05, over * 1.05));
        lay = layoutBannerText(text, fontFamily, fontStyle, req, el);
      }
    }
    return lay;
  }


  // ==========================================================
  // CROPPED?
  // ==========================================================

  const isCropped =
    (scene.video.crop && scene.video.crop.applied === true) ||
    p.isCropped === true ||
    p.cropped === true ||
    p.crop === true ||
    el.isCropped === true ||
    el.cropped === true;

  // ==========================================================
  // STAGE
  // ==========================================================

  konvaStage.style.display = "block";

  stage.width(container.clientWidth);
  stage.height(container.clientHeight);

  // ==========================================================
  // GROUP
  // ==========================================================

  let grp = konvaNodes.get(el.id);

  if (!grp) {

    grp = new Konva.Group({
      draggable: true
    });

    grp.add(
      new Konva.Rect({
        name: "bg"
      })
    );

    grp.add(
      new Konva.Text({
        name: "label"
      })
    );

    elemLayer.add(grp);

    konvaNodes.set(el.id, grp);

    grp.on("dragend", () => {

      el._dragOffsetY =
        grp.y() -
        (el._bannerBaseY || grp.y());

      applyBannerCorners();

    });

    grp.on("dragmove", applyBannerCorners);
  }

  const bg =
    grp.findOne(".bg");

  const lbl =
    grp.findOne(".label");

  // ==========================================================
  // TEXT CONFIG
  // ==========================================================

  lbl.text(text);

  lbl.fill(textColor);

  lbl.fontFamily(fontFamily);

  lbl.fontStyle(fontStyle);

  // fontSize is set by the typography engine (layoutBannerText) below.

  lbl.wrap("word");

  lbl.align("center");

  lbl.verticalAlign("middle");

  lbl.lineHeight(LINE_H);

  lbl.listening(false);

  // ==========================================================
  // MEASURE + AUTO-SHRINK
  //
  // Cap: banner never taller than 40% of the container
  // (matches the export rule in app.py).
  // ==========================================================

  // ==========================================================
  // LAYOUT — measured ONCE at output resolution by the typography
  // engine. The preview renders the same layout scaled down by kOut,
  // and the wrapped lines are sent verbatim to the FFmpeg export
  // fallback, so all three renderers produce identical text.
  // ==========================================================

  const L = fitLayout(REQ_FS_OUT);

  const PAD = L.padOut / kOut;             // preview px
  const BASE_FS = L.fontSizeOut / kOut;    // preview px
  const textWidth = Math.max(1, bannerW - PAD * 2);
  const bannerH = bandHpx > 0 ? bandHpx : Math.max(2, Math.ceil(L.heightOut / kOut));

  // Export payload values (output px) — consumed by _buildExportEdits.
  el._bannerFontSize = L.fontSizeOut;
  el._textLines = L.lines;
  el._lineHeight = L.lineHeight;
  el._padOut = L.padOut;

  lbl.fontSize(BASE_FS);

  // ==========================================================
  // POSITION
  // ==========================================================

  let bannerY;

  // FLUSH EDGE: flush with the top/bottom edge of the video rect. bannerSnapY
  // pins a top banner to the very top (vr.y) and a bottom banner to the very
  // bottom (vr.y + vr.h - bannerH) — baked-in black bars are ignored, exactly
  // like the client export engine and the server FFmpeg path.
  bannerY = bandHpx > 0 ? (vr.y + el.y * vr.h) : bannerSnapY(isCropped, isBot, vr, bannerH);

  // DIAGNOSTIC — explains exactly where the banner is being painted and
  // why (remove once snapping is confirmed working).
  if (!window.__bannerDiagThrottled) {
    window.__bannerDiagThrottled = true;
    console.log("[BANNER-SNAP]", {
      container:  { w: container.clientWidth, h: container.clientHeight },
      videoDims:  { natW: scene.video.naturalWidth, natH: scene.video.naturalHeight,
                    vw: videoEl.videoWidth, vh: videoEl.videoHeight, ready: videoEl.readyState },
      bars:       detectContentBars(),
      vr,
      isCropped, isBot, bannerH, bannerY,
    });
  }

  // ==========================================================
  // DRAG
  // ==========================================================

  const dragOffset =
    el._dragOffsetY || 0;

  const paintY =
    bannerY +
    dragOffset;

  el._bannerBaseY =
    bannerY;

  // ==========================================================
  // GROUP
  // ==========================================================

  grp.x(bannerX);

  grp.y(paintY);

  // ==========================================================
  // BACKGROUND
  // ==========================================================

  bg.x(0);

  bg.y(0);

  bg.width(bannerW);

  bg.height(bannerH);

  bg.fill(bgColor);

  // Keep the banner's corners square (no rounding to match the canvas rim).
  applyBannerCorners();

  // ==========================================================
  // TEXT
  // ==========================================================

  lbl.x(PAD);

  lbl.y(PAD);

  lbl.width(textWidth);

  lbl.height(
    Math.max(
      1,
      bannerH -
      PAD * 2
    )
  );

  // ==========================================================
  // Z INDEX
  // ==========================================================

  grp.zIndex(
    el.zIndex || 1
  );

  // ==========================================================
  // STORE
  // ==========================================================

  el._pixelHeight =
    bannerH;

  el._pixelY =
    bannerY;

  // ==========================================================
  // DRAW
  // ==========================================================

  elemLayer.batchDraw();

  // ==========================================================
  // DEBUG
  // ==========================================================

  console.log(
    "[BANNER]",
    {
      cropped: isCropped,
      chars: text.length,
      fontSizePreview: BASE_FS,
      fontSizeOut: L.fontSizeOut,
      lines: L.lines.length,
      textWidth,
      heightOut: L.heightOut,
      bannerHeight: bannerH,
      videoY: vr.y,
      bannerY
    }
  );

  // ==========================================================
  // FONT LOADED
  // ==========================================================

  ensureFont(rawFont).then(() => {
    // The real font just arrived — re-measure at output resolution with
    // the final metrics and re-apply the whole layout.
    el._layoutKey = null;
    el._layout = null;

    const L2 = fitLayout(REQ_FS_OUT);
    const PAD2 = L2.padOut / kOut;
    const FS2 = L2.fontSizeOut / kOut;
    const bannerH2 = bandHpx > 0 ? bandHpx : Math.max(2, Math.ceil(L2.heightOut / kOut));

    let by2;

    by2 = bandHpx > 0 ? (vr.y + el.y * vr.h) : bannerSnapY(isCropped, isBot, vr, bannerH2);

    el._bannerBaseY = by2;
    el._pixelHeight = bannerH2;
    el._pixelY = by2;
    el._bannerFontSize = L2.fontSizeOut;
    el._textLines = L2.lines;
    el._lineHeight = L2.lineHeight;
    el._padOut = L2.padOut;

    grp.y(by2 + (el._dragOffsetY || 0));

    bg.height(bannerH2);

    lbl.fontSize(FS2);
    lbl.x(PAD2);
    lbl.y(PAD2);
    lbl.width(Math.max(1, bannerW - PAD2 * 2));
    lbl.height(Math.max(1, bannerH2 - PAD2 * 2));

    elemLayer.batchDraw();

    console.log(
      "[BANNER FONT LOADED]",
      {
        fontSizeOut: L2.fontSizeOut,
        lines: L2.lines.length,
        bannerHeight: bannerH2
      }
    );
  });
}

  // ── Shape ────────────────────────────────────────────────────
  function _renderShape(el) {
    const p    = el.props || {};
    const rect = getWorldRect(el);

    let node = domNodes.get(el.id);
    if (!node) {
      node = document.createElement("div");
      node.dataset.elId = el.id;
      node.className = "aq-el aq-shape";
      node.style.cursor = "move";
      container.appendChild(node);
      domNodes.set(el.id, node);
      _makeDraggable(node, el);
    }

    Object.assign(node.style, {
      position:     "absolute",
      left:         `${rect.x}px`,
      top:          `${rect.y}px`,
      width:        `${rect.w}px`,
      height:       `${rect.h}px`,
      background:   p.fill || p.color || "#ffffff",
      opacity:      String(p.opacity ?? 1),
      borderRadius: p.shape === "circle" ? "50%" : (p.radius ? `${p.radius}px` : "0"),
      border:       p.borderWidth ? `${p.borderWidth}px solid ${p.borderColor || "#000"}` : "none",
      zIndex:       String((el.zIndex || 3) + 10),
      boxSizing:    "border-box",
      pointerEvents:"auto",
      userSelect:   "none",
    });
  }

  // ── Image / Logo ─────────────────────────────────────────────
  function _renderImage(el) {
    const p    = el.props || {};
    const src  = p.src || p.url || p.asset || null;
    const rect = getWorldRect(el);

    let node = domNodes.get(el.id);
    if (!node || node.tagName !== "IMG") {
      if (node) node.remove();
      node = document.createElement("img");
      node.dataset.elId = el.id;
      node.className = "aq-el aq-image";
      node.style.cursor = "move";
      container.appendChild(node);
      domNodes.set(el.id, node);
      _makeDraggable(node, el);
    }

    Object.assign(node.style, {
      position:     "absolute",
      left:         `${rect.x}px`,
      top:          `${rect.y}px`,
      width:        `${rect.w}px`,
      height:       `${rect.h}px`,
      objectFit:    "contain",
      opacity:      String(p.opacity ?? 1),
      zIndex:       String((el.zIndex || 4) + 10),
      pointerEvents:"auto",
      userSelect:   "none",
    });

    if (src && node.src !== src) node.src = src;
  }

  // ─────────────────────────────────────────────────────────────
  // DRAGGING  (DOM elements — mouse + touch)
  // ─────────────────────────────────────────────────────────────
  function _makeDraggable(node, el) {
    let startClientX, startClientY, startElX, startElY, dragging = false;

    function dragStart(clientX, clientY) {
      dragging = true;
      // User interaction wins: cancel any running AI edit animation so
      // it can never fight the drag or overwrite the user's changes.
      if (window.AQAnim && window.AQAnim.hasActive()) _cancelAnimations();
      startClientX = clientX;
      startClientY = clientY;
      const rect = getWorldRect(el);
      startElX = rect.x;
      startElY = rect.y;
    }

    function dragMove(clientX, clientY) {
      if (!dragging) return;
      const dx = clientX - startClientX;
      const dy = clientY - startClientY;
      const newPx = startElX + dx;
      const newPy = startElY + dy;

      const pvb = el.parentId
        ? (() => { const pe = scene.elements.find(e2 => e2.id === el.parentId); return pe ? getWorldRect(pe) : getVideoRect(); })()
        : getVideoRect();

      el.x = (newPx - pvb.x) / pvb.w;
      el.y = (newPy - pvb.y) / pvb.h;

      renderElements();
    }

    function dragEnd() {
      if (!dragging) return;
      dragging = false;
      console.log(`[DRAG] ${el.type} ${el.id} → x=${el.x.toFixed(3)} y=${el.y.toFixed(3)}`);
    }

    // ── Mouse ──
    node.addEventListener("mousedown", e => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      dragStart(e.clientX, e.clientY);

      function onMove(ev) { dragMove(ev.clientX, ev.clientY); }
      function onUp()     { dragEnd(); document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); }

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup",   onUp);
    });

    // ── Touch ──
    node.addEventListener("touchstart", e => {
      if (e.touches.length !== 1) return;
      e.preventDefault();
      e.stopPropagation();
      dragStart(e.touches[0].clientX, e.touches[0].clientY);
    }, { passive: false });

    node.addEventListener("touchmove", e => {
      if (e.touches.length !== 1) return;
      e.preventDefault();
      dragMove(e.touches[0].clientX, e.touches[0].clientY);
    }, { passive: false });

    node.addEventListener("touchend",    dragEnd);
    node.addEventListener("touchcancel", dragEnd);
  }

  // ─────────────────────────────────────────────────────────────
  // UTILITY
  // ─────────────────────────────────────────────────────────────
  function hexAlpha(hex, alpha) {
    const h = (hex || "").replace("#", "");
    if (h.length === 6) {
      const r = parseInt(h.slice(0,2),16), g = parseInt(h.slice(2,4),16), b = parseInt(h.slice(4,6),16);
      return `rgba(${r},${g},${b},${alpha})`;
    }
    return hex;
  }

  function parseRatio(str) {
    if (!str || !String(str).includes(":")) return null;
    const [a,b] = String(str).split(":").map(Number);
    return (a && b) ? a/b : null;
  }

  // ─────────────────────────────────────────────────────────────
  // POSITION PRESETS → normalized x/y
  // ─────────────────────────────────────────────────────────────
  // x/y is the TOP-LEFT corner of the element box (0-1 relative to parent/viewport).
  // For full-width text (width=1.0): x=0.0 is always correct.
  // For "center": y=0.44 so a 0.12-height element is visually centered (0.44+0.06=0.50).
  const POSITION_MAP = {
    "top-left":      { x: 0.01, y: 0.02 },
    "top-center":    { x: 0.0,  y: 0.02 },
    "top-right":     { x: 0.0,  y: 0.02 },
    "top":           { x: 0.0,  y: 0.02 },
    "center-left":   { x: 0.0,  y: 0.44 },
    "center":        { x: 0.0,  y: 0.44 },
    "center-right":  { x: 0.0,  y: 0.44 },
    "middle":        { x: 0.0,  y: 0.44 },
    "bottom-left":   { x: 0.0,  y: 0.82 },
    "bottom-center": { x: 0.0,  y: 0.82 },
    "bottom-right":  { x: 0.0,  y: 0.82 },
    "bottom":        { x: 0.0,  y: 0.82 },
  };

  function resolvePosition(posStr, width = 1.0) {
    if (!posStr) return null;
    const key = String(posStr).toLowerCase().replace(/\s+/g, "-");
    const p   = POSITION_MAP[key] || POSITION_MAP[key.replace("-center","")];
    if (p) return { x: p.x, y: p.y };
    return null;
  }

  // ─────────────────────────────────────────────────────────────
  // SCENE MUTATION — ELEMENT FINDERS
  // ─────────────────────────────────────────────────────────────
  function byId(id) {
    return scene.elements.find(e => e.id === id) || null;
  }

  /**
   * resolveTarget — smart target resolution for AI actions.
   * Priority: explicit ID → semantic alias → role → type → text content → lastReferenced
   */
  function resolveTarget(action) {
    const PRONOUNS = new Set(["it","that","this","last","previous","last_created","the_last"]);
    const t = action.target || action.target_role || "";

    // 1. Explicit valid ID
    if (t && !PRONOUNS.has(t.toLowerCase())) {
      const byIdEl = byId(t);
      if (byIdEl) { scene.refs.lastReferencedId = byIdEl.id; return byIdEl; }
    }

    // 2. Pronoun
    if (PRONOUNS.has(t.toLowerCase())) {
      const el = byId(scene.refs.lastReferencedId || scene.refs.lastCreatedId);
      if (el) return el;
    }

    // 3. target_role
    if (action.target_role) {
      const byRole = scene.elements.filter(e => e.role === action.target_role);
      if (byRole.length) { scene.refs.lastReferencedId = byRole[0].id; return byRole[0]; }
    }

    // 4. Semantic string match
    if (t) {
      const low = t.toLowerCase();
      // banner aliases
      if (["banner","top banner","top_banner","the bar","the strip","the top bar","the top banner"].includes(low)) {
        const b = scene.elements.find(e => e.type === "banner" && !(e.props?.position || "top").toLowerCase().includes("bottom"));
        if (b) { scene.refs.lastReferencedId = b.id; return b; }
      }
      if (["bottom banner","bottom_banner","the bottom bar","bottom bar"].includes(low)) {
        const b = scene.elements.find(e => e.type === "banner" && (e.props?.position || "").toLowerCase().includes("bottom"));
        if (b) { scene.refs.lastReferencedId = b.id; return b; }
      }
      if (["text","the text","caption","the caption"].includes(low)) {
        const texts = scene.elements.filter(e => e.type === "text");
        if (texts.length === 1) { scene.refs.lastReferencedId = texts[0].id; return texts[0]; }
      }
      // "text inside the banner"
      if (low.includes("banner") && low.includes("text")) {
        const bt = scene.elements.find(e => e.type === "text" && e.parentId);
        if (bt) { scene.refs.lastReferencedId = bt.id; return bt; }
      }
      // fabricated-ID prefix: "top_banner_abc" → match first top banner
      if (low.startsWith("top_banner") || low.startsWith("banner_top")) {
        const b = scene.elements.find(e => e.type === "banner" && !(e.props?.position || "top").toLowerCase().includes("bottom"));
        if (b) { scene.refs.lastReferencedId = b.id; return b; }
      }
      if (low.startsWith("bottom_banner") || low.startsWith("bot_banner") || low.startsWith("banner_bot")) {
        const b = scene.elements.find(e => e.type === "banner" && (e.props?.position || "").toLowerCase().includes("bottom"));
        if (b) { scene.refs.lastReferencedId = b.id; return b; }
      }
      // fuzzy: role / type / text content / ID type prefix
      const fuzz = scene.elements.find(e =>
        (e.role || "").toLowerCase() === low ||
        e.type.toLowerCase() === low ||
        low.startsWith(e.type.toLowerCase() + "_") ||  // "text_abc" → type "text"
        (e.props?.text || e.props?.content || "").toLowerCase().includes(low)
      );
      if (fuzz) { scene.refs.lastReferencedId = fuzz.id; return fuzz; }
    }

    // 5. Single-element-of-type fallback
    const allBanners = scene.elements.filter(e => e.type === "banner");
    if (allBanners.length === 1) { scene.refs.lastReferencedId = allBanners[0].id; return allBanners[0]; }

    const allTexts = scene.elements.filter(e => e.type === "text");
    if (allTexts.length === 1) { scene.refs.lastReferencedId = allTexts[0].id; return allTexts[0]; }

    // 6. lastReferenced
    const fallback = byId(scene.refs.lastReferencedId || scene.refs.lastCreatedId);
    return fallback || null;
  }

  function _setRef(id) { scene.refs.lastReferencedId = id; }
  function _setCreated(id) { scene.refs.lastCreatedId = id; scene.refs.lastReferencedId = id; }

  // ─────────────────────────────────────────────────────────────
  // ACTION VALIDATOR  — strips video keys from element actions
  // ─────────────────────────────────────────────────────────────
  const ELEM_ONLY_ACTIONS = new Set([
    "style_element","update_element","change_text","move_element",
    "resize_element","align_element","bring_forward","send_backward","delete_element",
  ]);
  const VIDEO_KEYS = new Set([
    "aspect_ratio","crop","nx","ny","nw","nh","canvas_width","canvas_height","dimensions",
  ]);

  function _guardAction(action) {
    if (!ELEM_ONLY_ACTIONS.has(action.action)) return;
    const p = action.properties || {};
    for (const k of VIDEO_KEYS) {
      if (k in p) {
        console.error(`[VALIDATOR] BLOCKED: ${action.action} tried to set video key "${k}"`);
        delete p[k];
      }
    }
  }

  // ─────────────────────────────────────────────────────────────
  // ACTION EXECUTOR
  // ─────────────────────────────────────────────────────────────
  function executeAction(raw) {
    if (!raw?.action) return;
    const a = { ...raw, properties: { ...(raw.properties || {}) } };

    // Flatten properties to top level (so action.font works same as action.properties.font)
    Object.entries(a.properties).forEach(([k, v]) => { if (!(k in a)) a[k] = v; });

    _guardAction(a);

    const cropBefore = JSON.stringify(scene.video.crop);
    console.group(`[ACTION] ${a.action}`);
    console.log("  target:", a.target || a.target_role || "(none)");
    console.log("  props:", a.properties);

    switch (a.action) {
      case "add_text":       _execAddText(a);       break;
      case "add_banner":     _execAddBanner(a);     break;
      case "add_shape":      _execAddShape(a);      break;
      case "add_image":      _execAddImage(a);      break;
      case "add_logo":       _execAddLogo(a);       break;
      case "change_text":    _execChangeText(a);    break;
      case "style_element":
      case "update_element": _execStyle(a);         break;
      case "move_element":   _execMove(a);          break;
      case "resize_element": _execResize(a);        break;
      case "delete_element": _execDelete(a);        break;
      case "align_element":  _execAlign(a);         break;
      case "set_parent":     _execSetParent(a);     break;
      case "crop_video":     _execCropVideo(a);     break;
      case "resize_video":   _execResizeVideo(a);   break;
      case "set_speed":      _execSetSpeed(a);      break;
      case "trim_video":     _execTrim(a);          break;
      case "set_background": _execBackground(a);    break;
      case "bring_forward":  _execBringFwd(a);      break;
      case "send_backward":  _execSendBwd(a);       break;
      default: console.warn(`[ACTION] Unknown action: ${a.action}`);
    }

    // Invariant check
    const cropAfter = JSON.stringify(scene.video.crop);
    if (cropAfter !== cropBefore && !["crop_video","resize_video"].includes(a.action)) {
      console.error(`[INVARIANT] "${a.action}" unexpectedly changed video.crop!`);
    } else {
      console.log("  video.crop:", cropAfter === cropBefore ? "UNCHANGED ✓" : `changed → ${cropAfter}`);
    }
    console.groupEnd();
  }

  // ─────────────────────────────────────────────────────────────
  // ACTION ANIMATION LAYER
  //
  // By the time any of this runs, the scene graph ALREADY holds the
  // final desired state (executeAction mutated it synchronously) and
  // renderScene() has rendered it. These functions only interpolate
  // the on-screen nodes from their pre-action visuals to the final
  // state, so:
  //   Konva/DOM preview (after settle) === scene graph === export.
  //
  // Cancellation: every batch captures the current _animGen. Undo,
  // redo, dragging or a new prompt bump the generation via
  // _cancelAnimations() — in-flight waves resolve as no-ops and all
  // tweens are killed, so a late finishing animation can never
  // overwrite newer state.
  // ─────────────────────────────────────────────────────────────

  /** Snapshot computed visuals of every currently-rendered element node. */
  function _snapNodeStylesAll() {
    const snap = {};
    for (const el of scene.elements) {
      const node = domNodes.get(el.id);
      if (node) {
        const cs = getComputedStyle(node);
        snap[el.id] = {
          kind: "dom",
          left: cs.left, top: cs.top, width: cs.width, height: cs.height,
          fontSize: cs.fontSize, opacity: cs.opacity,
          text: node.textContent,   // for ChatGPT-style text reveal diffs
        };
        continue;
      }
      const grp = konvaNodes.get(el.id);
      if (grp) {
        const bg = grp.findOne(".bg");
        const lbl = grp.findOne(".label");
        snap[el.id] = {
          kind: "konva",
          x: grp.x(), y: grp.y(),
          w: bg ? bg.width() : 0, h: bg ? bg.height() : 0,
          fs: lbl ? lbl.fontSize() : 0,
          txt: lbl ? lbl.text() : "",
        };
      }
    }
    return snap;
  }

  /** Which element identities an action touches (for dependency waves). */
  function _actionIds(a) {
    const ids = [];
    if (a.id) ids.push(`id:${a.id}`);
    if (a.target) ids.push(`t:${a.target}`);
    if (a.target_role) ids.push(`r:${a.target_role}`);
    if (!ids.length) ids.push(`anon:${Math.random()}`);
    return ids;
  }

  /**
   * Group plan actions into sequential dependency waves. Actions that
   * touch disjoint elements run in parallel (same wave); actions that
   * follow another action on the same element run in a later wave.
   * crop_video / resize_video always get an exclusive wave because the
   * whole video rect (and therefore every element position) changes.
   */
  function _planWaves(actions) {
    const waves = [];
    const waveOf = Object.create(null);
    let maxWave = -1;
    for (const a of actions) {
      const name = a.action || "";
      const isCrop = name === "crop_video" || name === "resize_video";
      const isCreate = /^add_/.test(name);
      const ids = _actionIds(a);
      let w;
      if (isCrop) {
        maxWave += 2;               // leave a gap: nothing shares this wave
        w = maxWave;
        // Everything already scheduled must finish before the crop wave.
        for (const k of Object.keys(waveOf)) waveOf[k] = Math.max(waveOf[k], w);
      } else if (isCreate) {
        w = 0;                      // creations can start immediately
      } else {
        w = 0;
        for (const id of ids) {
          if (waveOf[id] !== undefined) w = Math.max(w, waveOf[id] + 1);
        }
      }
      maxWave = Math.max(maxWave, w);
      for (const id of ids) waveOf[id] = Math.max(waveOf[id] ?? -1, w);
      (waves[w] = waves[w] || []).push(a);
    }
    const out = [];
    for (let i = 0; i <= maxWave; i++) if (waves[i]) out.push(waves[i]);
    return out;
  }


  /** ChatGPT-style reveal pacing: ~18ms/char, clamped to 240–600ms. */
  function _typeDuration(text) {
    return Math.max(240, Math.min(600, String(text || "").length * 18));
  }

  /** Entrance animation for newly created elements (opacity + subtle scale). */
  function _animateEntrance(id, done) {
    const node = domNodes.get(id);
    if (node) {
      const el = byId(id);
      const isText = !!(el && el.type === "text");
      const fullText = isText ? String(node.textContent || "") : "";
      const typeMs = isText && fullText ? _typeDuration(fullText) : 0;

      const targetOpacity = parseFloat(getComputedStyle(node).opacity || "1");
      node.style.willChange = "opacity, transform";
      node.style.transformOrigin = "center center";
      node.style.opacity = "0";
      if (typeMs) {
        node.textContent = "";            // will be typed in character-by-character
      } else {
        node.style.transform = "scale(0.85)";
      }
      window.AQAnim.tween({
        duration: typeMs || 320,
        easing: typeMs ? (t => t) : window.AQAnim.easeOut, // linear for typing
        onUpdate(v) {
          // Quick fade-in (first quarter), then pure typing.
          node.style.opacity = String(Math.min(1, v * (typeMs ? 4 : 1)) * targetOpacity);
          if (typeMs) {
            node.textContent = fullText.slice(0, Math.ceil(v * fullText.length));
          } else {
            node.style.transform = `scale(${0.85 + 0.15 * v})`;
          }
        },
        onComplete() {
          node.style.transform = "";
          node.style.willChange = "";
          const e2 = byId(id);
          if (e2) _renderElement(e2);
          done();
        },
        onCancel() {
          const e2 = byId(id);
          if (e2) _renderElement(e2);
          done();
        },
      });
      return true;
    }
    const grp = konvaNodes.get(id);
    if (grp) {
      const el = byId(id);
      const lbl = grp.findOne(".label");
      const fullText = (el && el.type === "banner" && lbl)
        ? String(el.props?.text || el.props?.content || "")
        : "";
      const typeMs = fullText ? _typeDuration(fullText) : 0;
      const yFinal = grp.y();
      const yStart = yFinal + 10;
      grp.opacity(0);
      grp.y(yStart);
      if (typeMs) lbl.text("");
      elemLayer.batchDraw();
      window.AQAnim.tween({
        duration: typeMs || 320,
        easing: typeMs ? (t => t) : window.AQAnim.easeOut,
        onUpdate(v) {
          grp.opacity(v);
          grp.y(yStart - 10 * v);
          if (typeMs) lbl.text(fullText.slice(0, Math.ceil(v * fullText.length)));
          elemLayer.batchDraw();
        },
        onComplete() {
          const e2 = byId(id);
          if (e2) _renderElement(e2); else { grp.opacity(1); elemLayer.batchDraw(); }
          done();
        },
        onCancel() {
          const e2 = byId(id);
          if (e2) _renderElement(e2); else { grp.opacity(1); grp.y(yFinal); elemLayer.batchDraw(); }
          done();
        },
      });
      return true;
    }
    return false;
  }

  /** Smoothly interpolate a video crop / framing change. */
  function _animateCrop(from, to, done) {
    _cropAnimating = true;
    _visualCrop = { applied: true, x: from.x, y: from.y, w: from.w, h: from.h };
    renderVideo(_visualCrop);
    renderElements();
    window.AQAnim.tween({
      duration: 480,
      easing: window.AQAnim.easeInOut,
      onUpdate(v) {
        _visualCrop = {
          applied: true,
          x: from.x + (to.x - from.x) * v,
          y: from.y + (to.y - from.y) * v,
          w: from.w + (to.w - from.w) * v,
          h: from.h + (to.h - from.h) * v,
        };
        renderVideo(_visualCrop);
        renderElements();
      },
      onComplete() { _finishCropAnim(done); },
      onCancel()   { _finishCropAnim(done); },
    });
  }

  function _finishCropAnim(done) {
    _visualCrop = null;
    _cropAnimating = false;
    _lastCropKey = null;
    renderVideo();
    renderElements();
    if (done) done();
  }

  /**
   * Diff-driven tween for an existing element: pin the node at its
   * pre-action visuals, then interpolate to the (already rendered)
   * final visuals. Works for DOM nodes (text/shape/image) and Konva
   * banner groups.
   */
  function _animateElementDiff(id, before, done) {
    const el = byId(id);
    if (!el) return false;

    const node = domNodes.get(id);
    if (node) {
      if (!before || before.kind !== "dom") return false;
      const cs = getComputedStyle(node);
      const to = {
        left: cs.left, top: cs.top, width: cs.width, height: cs.height,
        fontSize: cs.fontSize, opacity: cs.opacity,
      };
      const PX = new Set(["left", "top", "width", "height", "fontSize"]);
      const from = {};
      const toV = {};
      let changed = false;
      for (const k of Object.keys(to)) {
        const f = parseFloat(before[k]);
        const t = parseFloat(to[k]);
        if (Number.isFinite(f) && Number.isFinite(t) && Math.abs(t - f) > 0.5) {
          from[k] = f; toV[k] = t; changed = true;
        }
      }

      // ChatGPT-style typewriter when the AI changed the text content
      // (e.g. change_text). The node currently holds the FINAL text;
      // pin the old text and reveal the new one character-by-character.
      const beforeText = before.text != null ? String(before.text) : null;
      const curText = String(node.textContent || "");
      const textChanged = beforeText !== null && curText !== beforeText;
      const typeMs = textChanged ? _typeDuration(curText) : 0;

      if (!changed && !textChanged) return false;

      // Pin at the pre-action visuals
      for (const k of Object.keys(from)) node.style[k] = before[k];
      if (textChanged) node.textContent = beforeText;

      window.AQAnim.tween({
        duration: Math.max(380, typeMs),
        easing: window.AQAnim.easeOut,
        onUpdate(v) {
          for (const k of Object.keys(from)) {
            node.style[k] = (from[k] + (toV[k] - from[k]) * v) + (PX.has(k) ? "px" : "");
          }
          if (textChanged) {
            node.textContent = curText.slice(0, Math.ceil(v * curText.length));
          }
        },
        onComplete() { const e2 = byId(id); if (e2) _renderElement(e2); done(); },
        onCancel()   { const e2 = byId(id); if (e2) _renderElement(e2); done(); },
      });
      return true;
    }

    const grp = konvaNodes.get(id);
    if (grp) {
      if (!before || before.kind !== "konva") return false;
      const bg = grp.findOne(".bg");
      const lbl = grp.findOne(".label");
      if (!bg || !lbl) return false;
      const to = { x: grp.x(), y: grp.y(), w: bg.width(), h: bg.height(), fs: lbl.fontSize() };
      const from = { x: before.x, y: before.y, w: before.w, h: before.h, fs: before.fs };
      let changed = false;
      for (const k of Object.keys(to)) {
        if (Math.abs((to[k] || 0) - (from[k] || 0)) > 0.5) { changed = true; break; }
      }

      // Banner label text changed → typewriter the new banner text.
      const beforeTxt = before.txt != null ? String(before.txt) : null;
      const curTxt = String(lbl.text() || "");
      const txtChanged = beforeTxt !== null && curTxt !== beforeTxt;
      const typeMs = txtChanged ? _typeDuration(curTxt) : 0;

      if (!changed && !txtChanged) return false;
      if (txtChanged) lbl.text(beforeTxt);

      window.AQAnim.tween({
        duration: Math.max(380, typeMs),
        easing: window.AQAnim.easeOut,
        onUpdate(v) {
          grp.x(from.x + (to.x - from.x) * v);
          grp.y(from.y + (to.y - from.y) * v);
          bg.width(from.w + (to.w - from.w) * v);
          bg.height(from.h + (to.h - from.h) * v);
          lbl.fontSize(from.fs + (to.fs - from.fs) * v);
          if (txtChanged) lbl.text(curTxt.slice(0, Math.ceil(v * curTxt.length)));
          elemLayer.batchDraw();
        },
        onComplete() { const e2 = byId(id); if (e2) _renderElement(e2); else elemLayer.batchDraw(); done(); },
        onCancel()   { const e2 = byId(id); if (e2) _renderElement(e2); else elemLayer.batchDraw(); done(); },
      });
      return true;
    }
    return false;
  }

  /** Dispatch one action to its visual animator. Returns true if animated. */
  function _animateActionVisual(a, ctx, done) {
    const name = a.action || "";
    if (name === "crop_video" || name === "resize_video") {
      const to = scene.video.crop;
      const from = ctx.cropFrom;
      const same = !to || !to.applied || !from || !from.applied ||
        (from.x === to.x && from.y === to.y && from.w === to.w && from.h === to.h);
      if (same) return false;
      _animateCrop(from, to, () => { ctx.cropFrom = { ...to }; done(); });
      return true;
    }
    const id = a.id || a.target || null;
    if (!id) return false;
    if (ctx.createdIds.has(id)) return _animateEntrance(id, done);
    if (ctx.nodeStyles[id]) return _animateElementDiff(id, ctx.nodeStyles[id], done);
    return false;
  }

  /**
   * Run the full visual plan: dependency waves, sequentially; actions
   * inside a wave run in parallel. `gen` is the animation generation at
   * batch start — if it no longer matches, every wave is a no-op.
   */
  function _runPlanAnimations(gen, actions, beforeIds, nodeStyles, cropFrom, onAllDone) {
    let chain = Promise.resolve();
    if (!window.AQAnim) {
      if (onAllDone) onAllDone();
      return;
    }
    const createdIds = new Set(
      scene.elements.map(e => e.id).filter(id => !beforeIds.has(id))
    );
    const ctx = { createdIds, nodeStyles, cropFrom: { ...cropFrom } };
    const waves = _planWaves(actions);

    for (const waveActions of waves) {
      chain = chain.then(() => new Promise(resolve => {
        if (gen !== _animGen) { resolve(); return; }
        let finished = false;
        let pending = waveActions.length;
        const finish = () => { if (!finished) { finished = true; resolve(); } };
        const doneOne = () => { pending--; if (pending <= 0) finish(); };
        if (pending <= 0) { resolve(); return; }

        for (const a of waveActions) {
          let started = false;
          try { started = _animateActionVisual(a, ctx, doneOne); }
          catch (e) { console.error("[ANIM] action animation failed", a, e); }
          if (!started) doneOne();
        }
        // Safety valve: a stuck tween must never block later waves forever.
        setTimeout(finish, 4000);
      }));
    }

    // Fire once every wave has settled (or was skipped as stale).
    chain.then(() => { if (onAllDone) onAllDone(); });
  }

  /** Fade out nodes that are about to be deleted, then run `cb`. */
  function _fadeOutThen(ids, cb) {
    const writers = [];
    for (const id of ids) {
      const node = domNodes.get(id);
      const grp = konvaNodes.get(id);
      if (node) writers.push(v => { node.style.opacity = String(1 - v); });
      else if (grp) writers.push(v => { grp.opacity(1 - v); elemLayer.batchDraw(); });
    }
    if (!writers.length || !window.AQAnim) { cb(); return; }
    window.AQAnim.tween({
      duration: 160,
      easing: window.AQAnim.easeOut,
      onUpdate(v) { writers.forEach(w => w(v)); },
      onComplete: cb,
      onCancel: cb,
    });
  }

  // ─── add_text ────────────────────────────────────────────────
  function _execAddText(a) {
    const id  = a.id || genId("text");
    const p   = a.properties || {};
    const txt = a.content || a.text || p.content || p.text || "";

    // Resolve parent
    let parentId = a.parentId || a.parent_id || p.parentId || p.parent_id || null;
    if (!parentId) {
      // Semantic: "inside the banner"
      const posStr = (a.position || p.position || "").toLowerCase();
      if (posStr.includes("inside") || posStr.includes("in the banner") || posStr.includes("within")) {
        const banners = scene.elements.filter(e => e.type === "banner");
        if (banners.length === 1) parentId = banners[0].id;
        else if (banners.length > 1) parentId = banners[banners.length - 1].id;
      }
    }

    // Position
    let ex = 0.0, ey = 0.0, ew = 1.0, eh = 0.15;
    const hasExplicitXY = a.x !== undefined || a.y !== undefined ||
                          p.x !== undefined || p.y !== undefined;
    if (parentId) {
      ex = 0.0; ey = 0.0; ew = 1.0; eh = 1.0; // fill parent
    } else {
      // Explicit numeric coordinates beat the lossy position keyword
      // (e.g. the server snapped the text into the baked banner band).
      const posPx = hasExplicitXY ? null : resolvePosition(a.position || p.position || "center");
      if (posPx) { ex = posPx.x; ey = posPx.y; }
      else if (a.x !== undefined) ex = a.x;
      else if (p.x !== undefined) ex = p.x;
      if (a.y !== undefined) ey = a.y;
      else if (p.y !== undefined) ey = p.y;
      ew = a.width || p.width || 1.0;
      eh = a.height || p.height || 0.12;
    }

    // Fit-to-box clamp: text snapped into a baked banner band must
    // never overflow the band rect.
    let defFs = Math.round(outPxToPreviewPx(AQ_TYPO.text.defaultFs));
    if (!parentId && hasExplicitXY) {
      const boxHpx = getVideoRect().h * eh;
      defFs = Math.max(8, Math.min(defFs, Math.round(boxHpx * 0.4)));
    }

    const el = {
      id: id, type: "text", role: a.role || p.role || "text",
      parentId, x: ex, y: ey, width: ew, height: eh,
      zIndex: scene.elements.length + 5,
      props: {
        text:            txt,
        content:         txt,
        color:           a.textColor || a.text_color || p.textColor || p.text_color || p.color || (parentId ? undefined : "#ffffff"),
        // Default fontSize reduced from 28 to 16
        fontSize:        a.fontSize  || a.font_size  || p.fontSize  || p.font_size  || defFs,
        fontFamily:      a.fontFamily|| a.font       || p.fontFamily|| p.font       || "Inter",
        fontWeight:      a.fontWeight|| a.font_weight|| p.fontWeight|| p.font_weight|| "bold",
        textAlign:       a.textAlign || a.alignment  || p.textAlign || p.alignment  || "center",
        backgroundColor: a.backgroundColor || a.background_color || p.backgroundColor || p.background_color || null,
        backgroundOpacity: a.backgroundOpacity ?? a.background_opacity ?? p.backgroundOpacity ?? p.background_opacity ?? 0,
        padding:         a.padding   || p.padding    || 4,
        verticalAlign:   a.verticalAlign || p.verticalAlign || "middle",
        opacity:         a.opacity   || p.opacity    || 1,
      },
    };

    scene.elements.push(el);
    _setCreated(id);
    console.log(`  → created text ${id} "${txt}" parentId=${parentId}`);
  }

  // ─── add_banner ──────────────────────────────────────────────
  // A transparent/none background signals a "cleaned-band" banner: an
  // invisible holder that pins text to a baked bar already present in the
  // video pixels. Shared by the integrity checks below.
  function bgIsTransparent(bg) {
    const s = String(bg || "").toLowerCase();
    return s === "transparent" || s === "none" ||
           s === "#00000000" || s === "00000000" || s === "00";
  }

  function _execAddBanner(a) {
    const p        = a.properties || {};
    // "auto" is treated as "top" — the renderer snaps the banner to the
    // first row of visible picture content regardless, so no async
    // busyness analysis is needed any more (top is the default).
    let   position = a.position || p.position || "top";
    const wantsAuto = String(position).toLowerCase().trim() === "auto";
    if (wantsAuto) position = "top";

    const isBot    = String(position).toLowerCase().includes("bottom");
    const id       = a.id || genId(isBot ? "bot_banner" : "top_banner");

    // Replace existing banner at SAME position only (not the other one).
    // NEVER remove a cleaned-band (transparent) banner — it's the invisible
    // holder pinning text to the baked bar. Removing it would silently drop
    // that text whenever the user adds another banner in the same slot.
    const removed = [];
    scene.elements = scene.elements.filter(e => {
      if (e.type !== "banner") return true;
      if (bgIsTransparent(e.props?.backgroundColor ?? e.props?.bg_color)) return true;
      const bIsBot = (e.props?.position || e.role || "top").toLowerCase().includes("bottom");
      if (bIsBot === isBot) {
        removed.push(e.id);
        // Also remove child elements that belong to this banner
        return false;
      }
      return true;
    });
    // Remove children of removed banners
    for (const rid of removed) {
      konvaNodes.get(rid)?.destroy(); konvaNodes.delete(rid);
      domNodes.get(rid)?.remove();   domNodes.delete(rid);
      // Remove children
      const children = scene.elements.filter(e => e.parentId === rid);
      for (const child of children) {
        scene.elements = scene.elements.filter(e => e.id !== child.id);
        domNodes.get(child.id)?.remove(); domNodes.delete(child.id);
        konvaNodes.get(child.id)?.destroy(); konvaNodes.delete(child.id);
      }
    }

    // Banner geometry: full width, thin strip at top or bottom
    const bannerH = a.height != null ? a.height : (p.height != null ? p.height : 0.1);

    // Resolved banner background color (hoisted out of the object literal —
    // a `const` declaration is not valid as an object member and will throw
    // a SyntaxError that breaks the entire script).
    const _reqBg = a.bgColor || a.bg_color || p.bgColor || p.bg_color || p.backgroundColor || p.background_color;
    // A transparent/none background signals the "cleaned-band" banner: the bar
    // already exists in the video pixels, so we must NOT substitute a solid
    // color (that painted an unwanted bar on top of the video and broke the
    // band-snapping logic). Treat these sentinel values like the server does
    // (app.py middleware/list): transparent, none, #00000000, 00000000, "00".
    const _reqBgNorm = String(_reqBg || "").toLowerCase();
    const _isTransparentBg =
      _reqBgNorm === "transparent" ||
      _reqBgNorm === "none" ||
      _reqBgNorm === "#00000000" ||
      _reqBgNorm === "00000000" ||
      _reqBgNorm === "00";

    const el = {
      id, type: "banner", role: isBot ? "bottom_banner" : "top_banner",
      parentId: null,
      x: 0, y: (a.y != null ? a.y : (p.y != null ? p.y : (isBot ? 1 - bannerH : 0))),
      width: 1, height: bannerH,
      zIndex: 100,
      props: {
        position:        isBot ? "bottom" : "top",
        text:            a.text || p.text || a.content || p.content || "",
        content:         a.text || p.text || a.content || p.content || "",
        backgroundColor: _isTransparentBg
                          ? "transparent"
                          : (_reqBg ? _reqBg : (isBot ? "#000000" : "#ffffff")),
        color:           a.textColor || a.text_color || p.textColor || p.text_color || p.color || (isBot ? "#ffffff" : "#000000"),
        fontSize:        a.fontSize || a.font_size || p.fontSize || p.font_size || Math.round(outPxToPreviewPx(AQ_TYPO.banner.defaultFs)),
        fontFamily:      a.fontFamily || a.font || p.fontFamily || p.font || "Arial",
        fontWeight:      a.fontWeight || a.font_weight || p.fontWeight || p.font_weight || "bold",
      },
    };

    scene.elements.push(el);
    _setCreated(id);
    window.__bannerDiagThrottled = false; // re-arm the one-shot diagnostic
    console.log(`  → created banner ${id} pos=${position} bg=${el.props.backgroundColor}`);
  }

  // ─── add_shape ───────────────────────────────────────────────
  function _execAddShape(a) {
    const p = a.properties || {};
    // If role sounds like a banner, delegate
    if ((a.role || p.role || "").toLowerCase().includes("banner")) {
      _execAddBanner({ ...a, type: "banner" }); return;
    }
    const id = a.id || genId("shape");
    const posPx = resolvePosition(a.position || p.position || "center");
    const el = {
      id, type: "shape", role: a.role || p.role || "shape",
      parentId: null,
      x: a.x ?? p.x ?? (posPx?.x ?? 0.375),
      y: a.y ?? p.y ?? (posPx?.y ?? 0.4),
      width:  a.width  ?? p.width  ?? 0.25,
      height: a.height ?? p.height ?? 0.15,
      zIndex: scene.elements.length + 3,
      props: {
        shape:       a.shape || p.shape || "rectangle",
        fill:        a.fill  || p.fill  || a.color || p.color || "#ffffff",
        opacity:     a.opacity ?? p.opacity ?? 1,
        borderColor: a.borderColor || p.borderColor || a.border_color || p.border_color || null,
        borderWidth: a.borderWidth || p.borderWidth || a.border_width || p.border_width || 0,
        radius:      a.radius || p.radius || 0,
      },
    };
    scene.elements.push(el);
    _setCreated(id);
  }

  // ─── add_image / add_logo ────────────────────────────────────
  function _execAddImage(a) {
    const p  = a.properties || {};
    const id = a.id || genId("image");
    const posPx = resolvePosition(a.position || p.position || "center");
    const el = {
      id, type: "image", role: a.role || p.role || "image",
      parentId: null,
      x: a.x ?? p.x ?? (posPx?.x ?? 0.375),
      y: a.y ?? p.y ?? (posPx?.y ?? 0.375),
      width:  a.width  ?? p.width  ?? 0.25,
      height: a.height ?? p.height ?? 0.25,
      zIndex: scene.elements.length + 4,
      props: { src: a.src || p.src || a.url || p.url || null, opacity: a.opacity ?? p.opacity ?? 1 },
    };
    scene.elements.push(el);
    _setCreated(id);
  }
  function _execAddLogo(a) {
    _execAddImage({ ...a, type: "logo" });
    const last = scene.elements[scene.elements.length - 1];
    if (last) { last.type = "logo"; last.role = a.role || "logo"; }
  }

  // ─── change_text ─────────────────────────────────────────────
  function _execChangeText(a) {
    const el = resolveTarget(a);
    if (!el) return;
    const newText = a.text ?? a.new_text ?? a.content ?? (a.properties?.text) ?? (a.properties?.content);
    if (newText === undefined) return;
    el.props = el.props || {};
    el.props.text = newText; el.props.content = newText;
    _setRef(el.id);
    console.log(`  → change_text ${el.id} = "${newText}"`);
  }

  // ─── style_element / update_element ──────────────────────────
  function _execStyle(a) {
    const el = resolveTarget(a);
    if (!el) { console.warn("[ACTION] style_element: target not found", a.target); return; }
    el.props = el.props || {};

    const beforeSnap = JSON.stringify(el.props);
    const src = a.properties || a;

    const SKIP = new Set(["action","target","target_role","id","reason","properties"]);
    const BLOCKED = VIDEO_KEYS;

    for (const [k, v] of Object.entries(src)) {
      if (SKIP.has(k)) continue;
      if (BLOCKED.has(k)) { console.error(`[VALIDATOR] style_element blocked "${k}"`); continue; }

      // Normalize key aliases
      const key = _normKey(k, el.type);
      el.props[key] = v;
    }

    // Sync text/content
    if (el.props.text    && !el.props.content) el.props.content = el.props.text;
    if (el.props.content && !el.props.text)    el.props.text    = el.props.content;

    // A banner's text is drawn by its parented text child's color (see the
    // _renderBanner precedence: childProps.color wins over p.color). So a
    // text-color change aimed at a banner must also land on the child —
    // otherwise the baked-band holder shows no visible change.
    if (el.type === "banner" && el.props.color !== undefined) {
      const kids = scene.elements.filter(e => e.type === "text" && e.parentId === el.id);
      for (const kid of kids) {
        kid.props = kid.props || {};
        kid.props.color = el.props.color;
      }
    }

    const afterSnap = JSON.stringify(el.props);
    _setRef(el.id);
    console.log(`  → style ${el.id} ${beforeSnap !== afterSnap ? "CHANGED" : "no change"}`);
  }

  function _normKey(k, elType) {
    // font aliases
    if (k === "font_family" || k === "fontFamily") return "fontFamily";
    if (k === "font")                               return "fontFamily";
    if (k === "font_size")                          return "fontSize";
    if (k === "font_weight")                        return "fontWeight";
    if (k === "text_color" || k === "textColor") return "color";
    if (k === "bg_color" || k === "background_color") return "backgroundColor";
    if (k === "background_opacity") return "backgroundOpacity";
    if (k === "border_color") return "borderColor";
    if (k === "border_width") return "borderWidth";
    if (k === "border_radius") return "borderRadius";
    if (k === "text_align" || k === "alignment") return "textAlign";
    if (k === "vertical_align") return "verticalAlign";
    if (k === "line_height" || k === "line_spacing") return "lineHeight";
    if (k === "letter_spacing") return "letterSpacing";
    return k;
  }

  // ─── move_element ─────────────────────────────────────────────
  function _execMove(a) {
    const el = resolveTarget(a);
    if (!el) return;
    const p = a.properties || {};

    const newPos = a.position || p.position;

    // Explicit normalized x/y
    if (a.x !== undefined)   el.x = a.x;
    else if (p.x !== undefined) el.x = p.x;

    if (a.y !== undefined)   el.y = a.y;
    else if (p.y !== undefined) el.y = p.y;

    // Named position
    if (newPos && (a.x === undefined && p.x === undefined)) {
      const posPx = resolvePosition(newPos, el.width);
      if (posPx) { el.x = posPx.x; el.y = posPx.y; }
    }

    // Banner-specific: move to top or bottom
    if (el.type === "banner" && newPos) {
      const lp = newPos.toLowerCase();
      if (lp.includes("bottom")) {
        el.y = 1 - el.height;
        el.props.position = "bottom";
        el.role = "bottom_banner";
      } else if (lp.includes("top")) {
        el.y = 0;
        el.props.position = "top";
        el.role = "top_banner";
      }
    }

    // Delta move (e.g. "move down 10%")
    if (a.deltaX !== undefined) el.x = Math.max(0, el.x + a.deltaX);
    if (a.deltaY !== undefined) el.y = Math.max(0, el.y + a.deltaY);

    _setRef(el.id);
    console.log(`  → move ${el.id} x=${el.x?.toFixed(3)} y=${el.y?.toFixed(3)}`);
  }

  // ─── resize_element ───────────────────────────────────────────
  function _execResize(a) {
    const el = resolveTarget(a);
    if (!el) return;
    const p = a.properties || {};

    function applyProp(key, src, elObj, propKey) {
      const v = a[key] ?? p[key];
      if (v === undefined) return;
      if (typeof v === "object" && v.operation) {
        const cur = (propKey ? el.props?.[propKey] : elObj[key]) ?? (key === "fontSize" ? outPxToPreviewPx(AQ_TYPO.text.defaultFs) : 0.2);
        const amt = v.amount || 0;
        const newVal = v.operation === "increase"
          ? (v.unit === "percent" ? cur * (1 + amt / 100) : cur + amt)
          : (v.unit === "percent" ? cur * (1 - amt / 100) : Math.max(1, cur - amt));
        if (propKey) { el.props = el.props || {}; el.props[propKey] = newVal; }
        else elObj[key] = newVal;
      } else {
        if (propKey) { el.props = el.props || {}; el.props[propKey] = v; }
        else elObj[key] = v;
      }
    }

    applyProp("width",    el, el, null);
    applyProp("height",   el, el, null);
    applyProp("fontSize", el, el, "fontSize");
    applyProp("font_size",el, el, "fontSize");
    applyProp("scale",    el, el, null);

    _setRef(el.id);
    console.log(`  → resize ${el.id} w=${el.width} h=${el.height}`);
  }

  // ─── delete_element ────────────────────────────────────────────
  function _execDelete(a) {
    const el = resolveTarget(a);
    if (!el) return;
    scene.elements = scene.elements.filter(e => e.id !== el.id);
    if (scene.refs.lastCreatedId    === el.id) scene.refs.lastCreatedId    = null;
    if (scene.refs.lastReferencedId === el.id) scene.refs.lastReferencedId = null;
  }

  // ─── align_element ────────────────────────────────────────────
  function _execAlign(a) {
    const el = resolveTarget(a);
    if (!el) return;
    const alignment = a.alignment || a.position || (a.properties?.alignment) || (a.properties?.position) || "center";
    const posPx = resolvePosition(alignment, el.width);
    if (posPx) { el.x = posPx.x; el.y = posPx.y; }
    el.props = el.props || {};
    el.props.textAlign = alignment.includes("right") ? "right" : alignment.includes("left") ? "left" : "center";
    _setRef(el.id);
  }

  // ─── set_parent ──────────────────────────────────────────────
  function _execSetParent(a) {
    const el = resolveTarget(a);
    if (!el) return;
    const parentId = a.parentId || a.parent_id || (a.properties?.parentId) || (a.properties?.parent_id) || null;
    el.parentId = parentId;
    if (parentId) {
      el.x = 0.0; el.y = 0.0; el.width = 1.0; el.height = 1.0;
    }
    _setRef(el.id);
  }

  // ─── crop_video (THE ONLY ACTION THAT MODIFIES VIDEO GEOMETRY) ─
  function _execCropVideo(a) {
    console.log("[VIDEO] _execCropVideo:", a);
    const p = a.properties || {};
    const ratioStr = a.aspect_ratio || a.ratio || p.aspect_ratio || p.ratio;

    if (ratioStr) {
      _applyCropFromRatio(ratioStr); return;
    }
    const nx = a.nx ?? p.nx;
    if (nx !== undefined) {
      scene.video.crop = {
        applied: true,
        x: nx, y: a.ny ?? p.ny ?? 0,
        w: a.nw ?? p.nw ?? 1, h: a.nh ?? p.nh ?? 1,
      };
    }
  }

  function _applyCropFromRatio(ratioStr) {
    const ratio = parseRatio(ratioStr);
    if (!ratio) { console.warn("[VIDEO] Invalid ratio:", ratioStr); return; }

    function compute() {
      const vw = videoEl.videoWidth  || scene.video.naturalWidth;
      const vh = videoEl.videoHeight || scene.video.naturalHeight;
      if (!vw || !vh) { videoEl.addEventListener("loadedmetadata", compute, { once: true }); return; }

      let cw, ch;
      if (vw / vh > ratio) { ch = vh; cw = vh * ratio; }
      else                  { cw = vw; ch = vw / ratio; }

      const cx = (vw - cw) / 2 / vw;
      const cy = (vh - ch) / 2 / vh;

      scene.video.crop = { applied: true, x: cx, y: cy, w: cw/vw, h: ch/vh };
      scene.canvas.aspectRatio = ratioStr;
      _lastCropKey = null;
      renderScene();
      console.log(`[VIDEO] Crop set from ratio ${ratioStr}: x=${cx.toFixed(3)} y=${cy.toFixed(3)} w=${(cw/vw).toFixed(3)} h=${(ch/vh).toFixed(3)}`);
      // Show the crop preview so the user can reposition if they want.
      // If the crop entrance animation is still running, wait for it to
      // finish so the overlay doesn't cut the transition short.
      const tryOpenCrop = () => {
        if (_cropAnimating) { setTimeout(tryOpenCrop, 120); return; }
        openCropAdjust(ratioStr);
      };
      setTimeout(tryOpenCrop, 60);
    }

    compute();
  }

  // ─── resize_video ─────────────────────────────────────────────
  function _execResizeVideo(a) {
    const p = a.properties || {};
    const ratio = a.aspect_ratio || p.aspect_ratio;
    if (ratio) { _applyCropFromRatio(ratio); return; }
    const preset = a.preset || p.preset;
    if (preset) {
      const map = {"9:16":"9:16","vertical":"9:16","16:9":"16:9","landscape":"16:9","1:1":"1:1","square":"1:1"};
      const r = map[String(preset).toLowerCase()];
      if (r) _applyCropFromRatio(r);
    }
  }

  // ─── set_speed ────────────────────────────────────────────────
  function _execSetSpeed(a) {
    const s = Number(a.speed || (a.properties?.speed) || 1);
    if (s > 0) { scene.canvas.speed = s; videoEl.playbackRate = s; }
  }

  // ─── trim_video ───────────────────────────────────────────────
  function _execTrim(a) {
    const p = a.properties || {};
    scene.canvas.trim = {
      start: a.start ?? p.start ?? 0,
      end:   a.end   ?? p.end   ?? videoEl.duration,
    };
  }

  // ─── set_background ───────────────────────────────────────────
  function _execBackground(a) {
    const c = a.color || a.background || (a.properties?.color) || (a.properties?.background);
    if (c) { scene.canvas.background = c; }
  }

  // ─── bring_forward / send_backward ────────────────────────────
  function _execBringFwd(a) {
    const el = resolveTarget(a); if (!el) return;
    el.zIndex = (el.zIndex || 0) + (a.amount || 1);
    _setRef(el.id);
  }
  function _execSendBwd(a) {
    const el = resolveTarget(a); if (!el) return;
    el.zIndex = Math.max(0, (el.zIndex || 0) - (a.amount || 1));
    _setRef(el.id);
  }

  // ─────────────────────────────────────────────────────────────
  // ADOPT SERVER SCENE  (single source of truth from backend)
  // ─────────────────────────────────────────────────────────────
  function _adoptServerScene(ss) {
    console.group("[ADOPT] Server scene");

    // Preserve local video dimensions (server may not have them)
    const localVideo = { ...scene.video };

    // Canvas
    if (ss.canvas) {
      scene.canvas = {
        width:       ss.canvas.width       || 1080,
        height:      ss.canvas.height      || 1920,
        aspectRatio: ss.canvas.aspect_ratio || ss.canvas.aspectRatio || null,
        background:  ss.canvas.background  || null,
        speed:       ss.canvas.speed       || 1.0,
        trim:        ss.canvas.trim        || null,
      };
    }

    // Video crop
    const ssCrop = ss.canvas?.crop || ss.video?.crop || null;
    if (ssCrop) {
      scene.video.crop = {
        applied: ssCrop.applied ?? true,
        x: ssCrop.x ?? ssCrop.nx ?? 0,
        y: ssCrop.y ?? ssCrop.ny ?? 0,
        w: ssCrop.w ?? ssCrop.nw ?? 1,
        h: ssCrop.h ?? ssCrop.nh ?? 1,
      };
    }

    // Restore natural video dims
    scene.video.naturalWidth  = localVideo.naturalWidth  || ss.video?.width  || ss.video?.naturalWidth  || null;
    scene.video.naturalHeight = localVideo.naturalHeight || ss.video?.height || ss.video?.naturalHeight || null;
    scene.video.duration      = localVideo.duration      || ss.video?.duration || null;
    scene.video.filename      = localVideo.filename      || ss.video?.filename || getFilename();

    // References
    scene.refs = {
      lastCreatedId:    ss.references?.last_created    || ss.refs?.lastCreatedId    || null,
      lastReferencedId: ss.references?.last_referenced || ss.refs?.lastReferencedId || null,
    };

    // Normalize and adopt elements
    scene.elements = (ss.elements || []).map(se => _normalizeServerElement(se));
    scene.version  = ss.version || 0;

    // Destroy orphaned rendered nodes
    const live = new Set(scene.elements.map(e => e.id));
    for (const [id, node] of domNodes)   { if (!live.has(id)) { node.remove();    domNodes.delete(id);   } }
    for (const [id, grp]  of konvaNodes) { if (!live.has(id)) { grp.destroy();    konvaNodes.delete(id); } }

    _lastCropKey = null;
    console.log("  elements:", scene.elements.length, "version:", scene.version);
    console.groupEnd();
  }

  /** Normalize a server element to local scene element format */
  function _normalizeServerElement(se) {
    const p = { ...(se.properties || {}) };

    // Font key normalization
    const fontVal = p.font || p.font_family || p.fontFamily || null;
    if (fontVal) { p.fontFamily = fontVal; delete p.font; delete p.font_family; delete p.fontFamily; p.fontFamily = fontVal; }

    // Color key normalization
    if (p.text_color && !p.color)             p.color = p.text_color;
    if (p.textColor  && !p.color)             p.color = p.textColor;
    if (p.bg_color && !p.backgroundColor)     p.backgroundColor = p.bg_color;
    if (p.background_color && !p.backgroundColor) p.backgroundColor = p.background_color;

    // Text/content sync
    if (p.text && !p.content) p.content = p.text;
    if (p.content && !p.text) p.text = p.content;

    // Font size key normalization
    if (p.font_size && !p.fontSize)   p.fontSize = p.font_size;
    if (p.font_weight && !p.fontWeight) p.fontWeight = p.font_weight;

    // Parent ID normalization
    const parentId = se.parentId || se.parent_id || p.parent_id || p.parentId || null;
    // Remove parent from props (it's a top-level field now)
    delete p.parent_id; delete p.parentId;

    // Geometry from server (may use x/y/width/height or normalized props)
    const ex = se.x ?? p.x ?? (se.type === "banner" ? 0 : 0.0);
    const ey = se.y ?? p.y ?? (se.type === "banner" ? 0 : 0.4);
    const ew = se.width  ?? p.width  ?? (se.type === "banner" ? 1.0 : 1.0);
    const eh = se.height ?? p.height ?? (se.type === "banner" ? 0.1 : 0.12);

    // For banners: if position is "bottom", y should be near 1-height
    if (se.type === "banner") {
      const pos = (p.position || "top").toLowerCase();
      const bh  = eh;
      const by  = pos.includes("bottom") ? 1 - bh : 0;
      return {
        id: se.id, type: se.type, role: se.role || null,
        parentId,
        x: 0, y: by, width: 1.0, height: bh,
        zIndex: se.z_index ?? se.zIndex ?? 100,
        props: p,
      };
    }

    return {
      id: se.id, type: se.type, role: se.role || null,
      parentId,
      x: ex, y: ey, width: ew, height: eh,
      zIndex: se.z_index ?? se.zIndex ?? 5,
      props: p,
    };
  }

  // ─────────────────────────────────────────────────────────────
  // RESULT HANDLER
  // ─────────────────────────────────────────────────────────────
  function handleResult(result) {
    // The plan has arrived — dismiss the "Generating" overlay NOW so the
    // materialization animations below play on a clean canvas.
    hideEditOverlay();
    console.group("[RESULT] handleResult");
    console.log("  type:", result.response_type);
    console.log("  message:", result.message);
    console.log("  has server scene:", !!result.scene);

    // BAKED-BANNER SWAP — the server erased text burned into the video
    // pixels and re-encoded a clean clip. Point the preview at it. The
    // loadedmetadata listener re-syncs scene.video (filename, size,
    // duration) automatically.
    if (result.video_swapped && result.video_swapped.cleaned_url) {
      const swap = result.video_swapped;
      try {
        const sourceEl = videoEl.querySelector("source");
        const newUrl = swap.cleaned_url;
        if (sourceEl) sourceEl.src = newUrl; else videoEl.src = newUrl;
        videoEl.load();
        console.log("  video swapped to cleaned clip:", newUrl);
        showMsg(swap.message || "Removed text baked into the video's banner.", {
          type: "success", ms: 5000,
        });
      } catch (e) {
        console.warn("  video swap failed:", e);
      }
    }

    // Clarifications / conversations / crop choices need user input → keep them visible.
    // Results with NO actions (misclassified vague prompts) also stay visible.
    // Successful edit confirmations are deferred until the animations finish
    // (the canvas itself is the progress UI while the edit plays out).
    const actions = result?.plan?.actions || result?.actions || [];
    const isSwap = !!result.video_swapped;
    const needsInput =
      !isSwap && (
        result.response_type === "clarification" ||
        result.response_type === "conversation" ||
        result.response_type === "crop_choice" ||
        actions.length === 0
      );

    const isEdit =
      (result.response_type === "edit" && actions.length > 0) || isSwap;
    const showResultMsg = () => {
      if (!result.message) return;
      showMsg(result.message, {
        type:  isEdit ? "success" : "info",
        sticky: needsInput,
        ms:    needsInput ? undefined : 5000,
      });
    };

    // Questions / vague prompts → bubble immediately. Successful edits →
    // bubble appears only AFTER the materialization animations settle.
    if (!isEdit) showResultMsg();

    // Track the question Autoquence is waiting on. A clarification or
    // crop choice ALWAYS sets it; a conversation sets it only if the
    // message actually asks something; a successful edit clears it.
    if (result.response_type === "clarification" || result.response_type === "crop_choice") {
      pendingQuestion = result.message || "";
    } else if (result.response_type === "conversation") {
      pendingQuestion = (result.message || "").trim().endsWith("?") ? result.message : null;
    } else if (actions.length > 0 || isSwap) {
      pendingQuestion = null;
    }

    if (result.response_type === "clarification" || result.response_type === "conversation") {
      console.groupEnd(); return;
    }

    // Always save history before mutating
    saveHistory();

    // Log what the server returned so we can debug
    console.log(`  actions from server (${actions.length}):`, actions.map(a => `${a.action}[target=${a.target}]`).join(", "));
    if (result.scene) {
      console.log(`  server scene elements (${result.scene.elements?.length}):`,
        (result.scene.elements || []).map(e => e.id).join(", "));
    }

    // LOCAL ACTION PATH — execute actions directly on our local scene.
    // This is MORE RELIABLE than adopting the server scene because:
    //   1. The server may not have the same IDs as us (it reconstructed the scene from our snapshot)
    //   2. The server resolver may fail silently
    //   3. Our local resolveTarget() has access to the live scene with exact IDs
    //
    // ANIMATED PIPELINE: the scene is mutated to its FINAL state first
    // (source of truth), then the animation layer interpolates the
    // visible nodes from their pre-action visuals to that state.
    if (Array.isArray(actions) && actions.length > 0) {
      const beforeIds = new Set(scene.elements.map(e => e.id));
      const nodeStylesBefore = window.AQAnim ? _snapNodeStylesAll() : {};
      const cropBefore = { ...scene.video.crop };
      const gen = _animGen;
      const beforeList = [...beforeIds];

      // Pre-phase: fade out elements this plan deletes (visual continuity),
      // then run the whole plan.
      const deleteIds = [];
      for (const a of actions) {
        if (a.action === "delete_element") {
          const el = resolveTarget(a);
          if (el) deleteIds.push(el.id);
        }
      }

      const runExecution = () => {
        if (gen !== _animGen) { renderScene(); return; } // cancelled mid-fade
        actions.forEach(a => {
          try {
            console.log(`  [EXEC] ${a.action} target=${a.target || a.target_role || "(none)"}`);
            executeAction(a);
          } catch(e) { console.error("[ACTION ERROR]", a, e); }
        });
        const afterList = scene.elements.map(e => e.id);
        console.log("  before:", beforeList.join(", ") || "(none)");
        console.log("  after:", afterList.join(", ") || "(none)");
        scene.version++;

        // Also adopt canvas-level changes from server scene (background, crop, speed)
        if (result.scene) {
          _adoptCanvasOnly(result.scene);
        }

        // Final state rendered FIRST — then animate toward it. The AI's
        // summary bubble appears once every animation wave has settled.
        renderScene();
        _runPlanAnimations(gen, actions, beforeIds, nodeStylesBefore, cropBefore, showResultMsg);
      };

      if (deleteIds.length) {
        _fadeOutThen(deleteIds, runExecution);
      } else {
        runExecution();
      }
    } else if (result.scene && Array.isArray(result.scene.elements)) {
      // Fallback: no actions — use server scene as authoritative
      console.log("  [ADOPT] No actions, adopting server scene");
      _adoptServerScene(result.scene);
      renderScene();
    }

    console.log("[RESULT] Final scene version:", scene.version, "elements:", scene.elements.length,
      scene.elements.map(e => `${e.type}[${e.id}]`).join(", "));

    // AI asked which crop dimensions the user wants → show clickable ratio chips
    if (result.response_type === "crop_choice") {
      showCropChoiceChips(result.message || "What dimensions should I crop the video to?");
    }

    console.groupEnd();
  }

  /** Adopt ONLY canvas-level changes (background, speed, crop) from server scene, without replacing elements */
  function _adoptCanvasOnly(ss) {
    if (!ss) return;
    if (ss.canvas?.background && ss.canvas.background !== scene.canvas.background) {
      scene.canvas.background = ss.canvas.background;
    }
    if (ss.canvas?.speed && ss.canvas.speed !== scene.canvas.speed) {
      scene.canvas.speed = ss.canvas.speed;
      videoEl.playbackRate = ss.canvas.speed;
    }
    const ssCrop = ss.canvas?.crop || ss.video?.crop;
    if (ssCrop?.applied && !scene.video.crop.applied) {
      scene.video.crop = { ...ssCrop };
      _lastCropKey = null;
    }
  }

  // ─────────────────────────────────────────────────────────────
  // SEND PROMPT
  // ─────────────────────────────────────────────────────────────
  const convHistory = [];
  // Question Autoquence is currently waiting on (clarification /
  // follow-up). Sent with the next prompt so the server knows the
  // user's message is an ANSWER, not a fresh request.
  let pendingQuestion = null;

  function getSceneSnapshot() {
    return {
      version: scene.version,
      canvas:  { ...scene.canvas },
      video: {
        width:    scene.video.naturalWidth  || videoEl.videoWidth  || null,
        height:   scene.video.naturalHeight || videoEl.videoHeight || null,
        duration: scene.video.duration      || (Number.isFinite(videoEl.duration) ? videoEl.duration : null),
        filename: getFilename(),
        crop:     { ...scene.video.crop },
      },
      elements: scene.elements.map(el => ({
        id:         el.id,
        type:       el.type,
        role:       el.role || null,
        parentId:   el.parentId || null,
        x:          el.x,
        y:          el.y,
        width:      el.width,
        height:     el.height,
        zIndex:     el.zIndex || 0,
        properties: { ...el.props },
      })),
      refs: { ...scene.refs },
    };
  }

  async function sendPrompt(prompt) {
    // A new prompt supersedes any running edit animation: kill it so the
    // latest valid scene state always wins and no stale tween can fight
    // the freshly arriving plan.
    if (window.AQAnim && window.AQAnim.hasActive()) _cancelAnimations();
    const snap = getSceneSnapshot();
    console.group("[PROMPT]", prompt);
    console.log("  scene elements:", snap.elements.length);
    console.log("  snapshot:", JSON.stringify(snap).slice(0, 400));

    window.posthog?.capture("edit_request", {
      prompt:         String(prompt).slice(0, 300),
      scene_elements: snap.elements?.length || 0,
      answering:      !!pendingQuestion,
    });

    let res;
    try {
      res = await fetch("/api/autoquence/edit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          scene:            snap,
          available_assets: [{ type: "video", filename: getFilename() }],
          available_fonts:  AVAILABLE_FONTS,
          conversation:     convHistory.slice(-10),
          pending_question: pendingQuestion || null,
        }),
      });
    } catch (e) {
      window.posthog?.capture("edit_error", { phase: "network", error: e.message });
      throw e;
    }

    let result;
    try { result = await res.json(); }
    catch { throw new Error("Server returned an invalid response."); }
    if (!res.ok) {
      window.posthog?.capture("edit_error", { phase: "http", status: res.status });
      throw new Error(result.error || `Server error ${res.status}`);
    }

    console.log("[PROMPT] response:", JSON.stringify(result).slice(0, 500));
    console.groupEnd();

    convHistory.push({ role: "user",      content: prompt });
    const assistantEntry = { role: "assistant", content: result.message || "" };
    // Carry OpenRouter chain-of-thought so it can be forwarded verbatim for
    // multi-turn reasoning. Only set when the server actually returned it.
    if (result && result.reasoning_details) {
      assistantEntry.reasoning_details = result.reasoning_details;
    }
    convHistory.push(assistantEntry);

    handleResult(result);
    window.posthog?.capture("edit_completed", {
      response_type: result.response_type || "unknown",
      actions:       (result.plan?.actions || result.actions || []).length,
      message:       (result.message || "").slice(0, 200),
    });
    return result;
  }

  const AVAILABLE_FONTS = [
    "Arial","Arial Black","Impact","Inter","Roboto","Montserrat",
    "Poppins","Georgia","Verdana","Trebuchet MS","Oswald","Raleway",
    "Open Sans","Lato","Nunito","Bebas Neue","Anton","sans-serif","serif",
  ];

  // ─────────────────────────────────────────────────────────────
  // CROP UI (manual)
  // ─────────────────────────────────────────────────────────────
  let _cropBoxActive = false;
  let _activeCropRatio = null;   // locked aspect ratio while adjusting (null = free)
  let _cropPreviewing = false;   // true while the full-frame crop rectangle preview is open

  const cropRect = new Konva.Rect({
    x: 50, y: 50, width: 150, height: 250,
    stroke: "white", strokeWidth: 2, draggable: true,
    dragBoundFunc(pos) {
      return {
        x: Math.max(0, Math.min(pos.x, stage.width()  - this.width())),
        y: Math.max(0, Math.min(pos.y, stage.height() - this.height())),
      };
    },
  });
  const cropHandle = new Konva.Circle({ x: 200, y: 300, radius: 12, fill: "white", draggable: true });
  cropLayer.add(cropRect);
  cropLayer.add(cropHandle);

  cropRect.on("dragstart", () => {
    // Manual crop adjustment supersedes any running crop animation.
    if (window.AQAnim && window.AQAnim.hasActive()) _cancelAnimations();
  });

  cropHandle.on("dragmove", () => {
    const vb = displayedVideoRect();
    let w = Math.max(50, cropHandle.x() - cropRect.x());
    // Clamp inside video bounds
    w = Math.min(w, vb.x + vb.w - cropRect.x());
    if (_activeCropRatio) {
      // Lock to aspect ratio while resizing
      let h = w / _activeCropRatio;
      if (cropRect.y() + h > vb.y + vb.h) {
        h = Math.max(50, vb.y + vb.h - cropRect.y());
        w = h * _activeCropRatio;
      }
      cropRect.size({ width: w, height: h });
    } else {
      cropRect.width(w);
      cropRect.height(Math.max(50, Math.min(cropHandle.y() - cropRect.y(), vb.y + vb.h - cropRect.y())));
    }
    cropHandle.position({ x: cropRect.x() + cropRect.width(), y: cropRect.y() + cropRect.height() });
    cropLayer.batchDraw();
  });
  cropRect.on("dragmove", () => {
    cropHandle.position({ x: cropRect.x() + cropRect.width(), y: cropRect.y() + cropRect.height() });
    cropLayer.batchDraw();
  });

  /**
   * displayedVideoRect() — actual on-screen pixel rect of the FULL video frame,
   * accounting for any currently applied crop transform (matches renderVideo).
   */
  function displayedVideoRect() {
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    const vw = videoEl.videoWidth || scene.video.naturalWidth || cw;
    const vh = videoEl.videoHeight || scene.video.naturalHeight || ch;
    if (_cropPreviewing || !scene.video.crop.applied) return _containRect(cw, ch, vw, vh);
    const nat = _containRect(cw, ch, vw, vh);
    const c = currentCrop();
    const cpw = c.w * nat.w, cph = c.h * nat.h;
    const sc = Math.min(cw / cpw, ch / cph);
    return {
      x: cw / 2 - (c.x * nat.w + cpw / 2) * sc,
      y: ch / 2 - (c.y * nat.h + cph / 2) * sc,
      w: nat.w * sc,
      h: nat.h * sc,
    };
  }

  function showCropBox(ratio) {
    _activeCropRatio = ratio || null;
    const vb = displayedVideoRect();
    let bw = vb.w, bh = vb.h;
    if (ratio) {
      bw = vb.w; bh = bw / ratio;
      if (bh > vb.h) { bh = vb.h; bw = bh * ratio; }
    } else {
      bw = vb.w * 0.6; bh = vb.h * 0.6;
    }
    const sx = vb.x + (vb.w - bw) / 2, sy = vb.y + (vb.h - bh) / 2;
    cropRect.position({ x: sx, y: sy }); cropRect.size({ width: bw, height: bh });
    cropHandle.position({ x: sx + bw, y: sy + bh });
    cropLayer.batchDraw();
  }

  /**
   * openCropAdjust(ratioStr?) — show the crop preview/adjustment overlay.
   * Starts at the CURRENT applied crop (or centered at the given ratio).
   * User can drag to reposition / resize (ratio-locked), then Apply.
   */
  function openCropAdjust(ratioStr) {
    // Crop controls are about to take over — dismiss any AI response bubble
    // (e.g. the confirmation toast that appears right before the overlay opens).
    _hideResponseBox();

    const ratio = ratioStr ? parseRatio(ratioStr)
                : (scene.video.crop.applied
                    ? (scene.video.crop.w * (scene.video.naturalWidth || 1)) /
                      (scene.video.crop.h * (scene.video.naturalHeight || 1))
                    : null);

    // FORMER PREVIEW BEHAVIOR: restore the FULL video frame to its natural
    // letterboxed layout so the user drags the crop rectangle over the
    // untouched frame. The contained/centered crop view only renders on Apply.
    _cropPreviewing = true;
    _lastCropKey = null;
    renderVideo();

    konvaStage.style.display = "block";
    stage.width(container.clientWidth);
    stage.height(container.clientHeight);
    cropLayer.visible(true);
    cropLayer.moveToTop();
    _cropBoxActive = true;

    const vb = displayedVideoRect();

    if (ratio && scene.video.crop.applied) {
      // Start from current crop position (mapped onto the full-frame rect)
      cropRect.position({
        x: vb.x + scene.video.crop.x * vb.w,
        y: vb.y + scene.video.crop.y * vb.h,
      });
      cropRect.size({
        width:  scene.video.crop.w * vb.w,
        height: scene.video.crop.h * vb.h,
      });
      _activeCropRatio = ratio;
    } else {
      showCropBox(ratio);
    }
    cropHandle.position({ x: cropRect.x() + cropRect.width(), y: cropRect.y() + cropRect.height() });
    cropLayer.batchDraw();

    const cc = document.getElementById("cropControls");
    if (cc?.style) cc.style.display = "flex";
  }

  /**
   * showCropChoiceChips(message) — clickable ratio buttons shown when the AI
   * asks what dimensions the user wants (response_type "crop_choice").
   */
  function showCropChoiceChips(message) {
    hideCropChoiceChips();
    const wrap = document.createElement("div");
    wrap.id = "cropChoiceChips";
    Object.assign(wrap.style, {
      position: "fixed", left: "50%", bottom: "110px", transform: "translateX(-50%)",
      display: "flex", gap: "8px", flexWrap: "wrap", justifyContent: "center",
      background: "rgba(20,20,25,0.92)", padding: "10px 14px", borderRadius: "12px",
      zIndex: "3000", boxShadow: "0 4px 18px rgba(0,0,0,0.5)",
    });

    const label = document.createElement("span");
    label.textContent = message || "Choose crop size:";
    Object.assign(label.style, { color: "#fff", fontSize: "13px", alignSelf: "center", marginRight: "4px" });
    wrap.appendChild(label);

    const RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "4:5"];
    for (const r of RATIOS) {
      const b = document.createElement("button");
      b.textContent = r;
      Object.assign(b.style, {
        padding: "6px 14px", borderRadius: "8px", border: "1px solid #555",
        background: "#2a2a33", color: "#fff", cursor: "pointer", fontSize: "13px",
      });
      b.addEventListener("mouseenter", () => { b.style.background = "#4f8ef7"; });
      b.addEventListener("mouseleave", () => { b.style.background = "#2a2a33"; });
      b.addEventListener("click", () => {
        hideCropChoiceChips();
        saveHistory();
        _applyCropFromRatio(r);
        openCropAdjust(r);
      });
      wrap.appendChild(b);
    }

    // Skip option
    const skip = document.createElement("button");
    skip.textContent = "Skip";
    Object.assign(skip.style, {
      padding: "6px 14px", borderRadius: "8px", border: "1px solid #555",
      background: "transparent", color: "#aaa", cursor: "pointer", fontSize: "13px",
    });
    skip.addEventListener("click", hideCropChoiceChips);
    wrap.appendChild(skip);

    document.body.appendChild(wrap);
  }

  function hideCropChoiceChips() {
    document.getElementById("cropChoiceChips")?.remove();
  }

  function getCropDataFromUI() {
    // Map dragged rect to normalized coords of the FULL video frame,
    // using the rect the video is ACTUALLY displayed in (crop-aware).
    const vb = displayedVideoRect();
    let x = (cropRect.x() - vb.x) / vb.w;
    let y = (cropRect.y() - vb.y) / vb.h;
    let w = cropRect.width()        / vb.w;
    let h = cropRect.height()       / vb.h;
    // Clamp to valid range
    w = Math.max(0.05, Math.min(1, w));
    h = Math.max(0.05, Math.min(1, h));
    x = Math.max(0, Math.min(1 - w, x));
    y = Math.max(0, Math.min(1 - h, y));
    return { x, y, w, h };
  }

  document.getElementById("ratio169")?.addEventListener("click", () => showCropBox(16/9));
  document.getElementById("ratio11") ?.addEventListener("click", () => showCropBox(1));

  document.getElementById("cropApply")?.addEventListener("click", () => {
    const c = getCropDataFromUI();
    saveHistory();
    scene.video.crop = { applied: true, ...c };
    _lastCropKey = null;
    _cropPreviewing = false;
    cropLayer.visible(false); _cropBoxActive = false; _activeCropRatio = null;
    document.getElementById("cropControls").style.display = "none";
    renderScene();
  });
  document.getElementById("cropCancel")?.addEventListener("click", () => {
    _cropPreviewing = false;
    _lastCropKey = null;
    cropLayer.visible(false); _cropBoxActive = false; _activeCropRatio = null;
    document.getElementById("cropControls").style.display = "none";
    renderScene(); // restore previous crop rendering
  });
  document.getElementById("CropBtn")?.addEventListener("click", () => {
    // Manual crop attempt — hide any lingering AI response so it never
    // overlaps the crop controls.
    _hideResponseBox();
    // Show the FULL frame while the user draws the crop rectangle
    _cropPreviewing = true;
    _lastCropKey = null;
    renderVideo();
    konvaStage.style.display = "block";
    stage.width(container.clientWidth); stage.height(container.clientHeight);
    cropLayer.visible(true); cropLayer.moveToTop(); _cropBoxActive = true;
    document.getElementById("cropControls")?.style && (document.getElementById("cropControls").style.display = "flex");
    showCropBox(null);
  });

  

  topBannerBtn?.addEventListener("click", () => {
    saveHistory();
    _execAddBanner({ position: "top", id: null });
    renderScene();
  });

  // ─────────────────────────────────────────────────────────────
  // UNDO / REDO
  // ─────────────────────────────────────────────────────────────
  document.addEventListener("keydown", e => {
    const ctrl = e.ctrlKey || e.metaKey;
    if (ctrl && e.key === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
    if (ctrl && (e.key === "y" || (e.key === "z" && e.shiftKey))) { e.preventDefault(); redo(); }
  });
  document.getElementById("undoBtn")?.addEventListener("click", undo);
  document.getElementById("redoBtn")?.addEventListener("click", redo);

  // ─────────────────────────────────────────────────────────────
  // INTENT GATE (mirrors app.py router)
  // Only prompts that look like real edit requests trigger the
  // full-screen "Editing video…" blur overlay.
  // ─────────────────────────────────────────────────────────────
  const _CONVERSATION_RE = /^\s*(what|whats|what's|who|whos|who's|how|hows|how's|why|when|where|which|is\s+there|are\s+there|do\s+you|did\s+you|can\s+you\s+(explain|tell)|tell\s+me|explain|list\s+(all\s+)?(the\s+)?(fonts|colors|options)|hi\b|hey\b|hello\b|yo\b|sup\b|thanks|thank\s+you|thx|ty\b|cool\b|nice\b|great\b|awesome\b|good\s+(job|work)|lol\b)/i;

  const _EDIT_VERB_RE = /\b(add|create|insert|put|place|write|make|change|set|turn|replace|remove|delete|drop|hide|show|crop|trim|cut|speed|rush|slow|resize|scale|style|recolor|color|colour|font|banner|overlay|text|caption|subtitle|logo|background|bigger|smaller|larger|faster|slower|zoom|flip|rotate|move|position|top|bottom)\b/i;

  function looksLikeConversation(prompt) {
    if (_CONVERSATION_RE.test(prompt)) return true;
    return !_EDIT_VERB_RE.test(prompt);
  }

  // ─────────────────────────────────────────────────────────────
  // PROMPT FORM
  // ─────────────────────────────────────────────────────────────
  form?.addEventListener("submit", async e => {
    e.preventDefault();
    const btn   = document.querySelector(".uploadBtn");
    const input = form.querySelector("input, textarea");
    if (!input) return;
    const prompt = input.value.trim();
    if (!prompt) return;
    if (btn) { btn.classList.add("loading"); btn.disabled = true; }
    // Lightweight "generating" state: subtle blur + a small pulsing chip.
    // It is dismissed at the TOP of handleResult() — i.e. the moment the
    // AI's plan arrives — so the materialization animations play on a
    // clean, unobstructed canvas.
    if (!looksLikeConversation(prompt)) {
      showEditOverlay();
    } else {
      // Conversation / clarification prompts get the rotating "thinking…"
      // bubble while the AI responds. Edit requests skip it — the canvas
      // "Generating" chip + materialization animations are their progress UI.
      showThinking();
    }
    // NOTE: no showThinking() response bubble during the edit — the canvas
    // "Generating" chip + the materialization animations are the progress UI.
    // The AI's summary bubble appears once the edit finishes (see handleResult).
    try {
      await sendPrompt(prompt);
      input.value = "";
    } catch(err) {
      console.error("[PROMPT ERROR]", err);
      showMsg(`Error: ${err.message}`, { type: "error", sticky: true });
    } finally {
      hideThinking(); // safety net (no-op if showMsg already replaced the bubble)
      hideEditOverlay(); // safety net (no-op if handleResult already hid it)
      if (btn) { btn.classList.remove("loading"); btn.disabled = false; }
    }
  });

  // ─────────────────────────────────────────────────────────────
  // CLIENT-SIDE EXPORT BRIDGE
  //
  // Exposes just enough internals for export-engine.js to composite
  // EXACTLY what this preview shows: geometry helpers, fonts, scene
  // state, and a snapshot of the Konva banner layer.
  // ─────────────────────────────────────────────────────────────
  window.__AQ_CANVAS_BRIDGE__ = {
    getScene: () => scene,
    getVideoEl: () => videoEl,
    getContainer: () => container,
    getFilename: getFilename,
    // Clean re-render with no crop-preview overlay so getVideoRect()
    // reflects the real committed state.
    forceRender: () => { _cropPreviewing = false; renderScene(); },
    getAnchorRect: () => getVideoRect(),
    getElementRect: (el) => getWorldRect(el),
    cssFontFamily: cssFontFamily,
    ensureFont: ensureFont,
    // Pixel-perfect banners: elemLayer holds only Konva banner groups,
    // rendered at an oversampled pixelRatio for output resolution.
    snapshotKonvaLayer: (pixelRatio) => {
      konvaStage.style.display = "block";
      stage.width(container.clientWidth);
      stage.height(container.clientHeight);
      return elemLayer.toCanvas({ pixelRatio });
    },
    // Per-banner snapshot for the client export engine: renders ONE banner
    // group (bg rect + parented text) standalone so the engine can place
    // top/bottom banners flush against the fitted VIDEO PICTURE edges
    // (matching the preview's bannerSnapY() and the server FFmpeg path).
    snapshotBannerGroup: (el, pixelRatio) => {
      const grp = konvaNodes.get(el.id);
      if (!grp) return null;
      konvaStage.style.display = "block";
      stage.width(container.clientWidth);
      stage.height(container.clientHeight);
      const bgNode = grp.findOne(".bg");
      return {
        canvas: grp.toCanvas({ pixelRatio: pixelRatio }),
        pr: pixelRatio,
        x: grp.x(),
        y: grp.y(),
        w: bgNode ? bgNode.width() : grp.width(),
        h: bgNode ? bgNode.height() : grp.height(),
        position: ((el.props && el.props.position) || "top"),
        dragOffsetY: el._dragOffsetY || 0,
        // Cleaned-band banner: expose the explicit band y so the export
        // engine can pin it to the baked band.
        yFrac: (String((((el.props || {}).backgroundColor) || "").toLowerCase() === "transparent" && typeof el.y === "number") ? el.y : null),
      };
    },
    // Baked-in letterbox bars of the SOURCE pixels (fractions of visible
    // video height). Kept for the [BANNER-SNAP] diagnostic and export
    // parity checks — placement no longer offsets banners past bars, since
    // all renderers pin banners flush to the video edges.
    contentBars: () => detectContentBars(),
  };

  // ─────────────────────────────────────────────────────────────
  // PLAY / PAUSE
  // ─────────────────────────────────────────────────────────────
  playPauseBtn?.addEventListener("click", () => {
    if (videoEl.paused) { videoEl.play(); playPauseBtn.classList.add("active"); }
    else { videoEl.pause(); playPauseBtn.classList.remove("active"); }
  });

  // ─────────────────────────────────────────────────────────────
  // EXPORT
  // ─────────────────────────────────────────────────────────────
  // Upload a finished blob to the server with REAL upload progress.
  function uploadBlob(blob, filename, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/export/upload/${getFilename()}`);
      xhr.responseType = "json";
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress((e.loaded / e.total) * 100);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response);
        else reject(new Error(`Upload failed (${xhr.status}): ${xhr.statusText}`));
      };
      xhr.onerror = () => reject(new Error("Upload failed (network error)."));
      const fd = new FormData();
      fd.append("file", blob, filename);
      xhr.send(fd);
    });
  }

  // ── Legacy server-side FFmpeg path (fallback when the browser lacks
  //    WebCodecs/MediaRecorder, or the client render fails).
  async function legacyFFmpegExport(bar, pct, label) {
    let fp = 0;
    const edits = _buildExportEdits();
    console.log("[EXPORT] edits:", edits);

    const res = await fetch(`/edit-video/${getFilename()}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edits }),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { const err = await res.json(); detail = err.details || err.error || detail; } catch(_) {}
      throw new Error(`Server error ${res.status}: ${detail}`);
    }
    const data = await res.json();

    // Async export: poll /export/status/<id> for REAL render progress.
    let outputFile = data.output_file;
    if (data.job_id) {
      if (label) label.textContent = "Rendering…";
      const job = await pollExportJob(data.job_id, (serverPct) => {
        if (serverPct > fp) {
          fp = serverPct;
          if (bar) bar.style.width = `${fp}%`;
          if (pct) pct.textContent = `${Math.round(fp)}%`;
        }
      });
      outputFile = job.output_file || outputFile;
    }
    return outputFile;
  }

  expBtn?.addEventListener("click", async () => {
    const hasEdits = scene.elements.length > 0 || scene.video.crop.applied || scene.canvas.background || scene.canvas.speed !== 1.0;
    if (!hasEdits) { alert("No edits to export."); return; }

    window.posthog?.capture("export_started", {
      elements: scene.elements.length,
      cropped:  !!scene.video.crop.applied,
    });
    let exportPath = "unknown";

    const overlay = document.getElementById("exportOverlay");
    const bar     = document.getElementById("exportProgressBar");
    const pct     = document.getElementById("exportPercent");
    const label   = document.getElementById("exportLabel");

    const setProgress = (v) => {
      if (bar) bar.style.width = `${Math.round(v)}%`;
      if (pct) pct.textContent = `${Math.round(v)}%`;
    };

    if (overlay) overlay.style.display = "flex";
    if (label)   label.textContent = "Exporting…";

    let outputFile = null;

    try {
      // ── Preferred path: render in the BROWSER from the exact canvas
      //    composition (WYSIWYG), then upload the encoded file.
      const engine = window.__AQ_CLIENT_EXPORT__;
      if (engine && engine.isSupported()) {
        exportPath = "client";
        const result = await engine.exportScene({
          onProgress: (p) => { setProgress(p); },
        });

        if (label) label.textContent = "Uploading…";
        setProgress(70);
        const data = await uploadBlob(result.blob, result.filename, (upPct) => {
          setProgress(70 + upPct * 0.3);
        });
        outputFile = data?.output_file;

        for (const w of result.warnings || []) {
          if (typeof showMsg === "function") showMsg(w, { type: "warn" });
          else console.warn("[EXPORT]", w);
        }
      } else {
        throw new Error("Client export not supported in this browser.");
      }
    } catch (clientErr) {
      console.error("[CLIENT EXPORT FAILED — falling back to FFmpeg]", clientErr);
      if (label) label.textContent = "Rendering…";
      setProgress(0);
      window.posthog?.capture("export_fallback", { error: clientErr.message });
      try {
        exportPath = "ffmpeg";
        outputFile = await legacyFFmpegExport(bar, pct, label);
      } catch (legacyErr) {
        console.error("[EXPORT ERROR]", legacyErr);
        if (overlay) overlay.style.display = "none";
        window.posthog?.capture("export_failed", { error: legacyErr.message });
        alert(`Export failed: ${legacyErr.message}`);
        return;
      }
    }

    if (!outputFile) {
      if (overlay) overlay.style.display = "none";
      window.posthog?.capture("export_failed", { error: "no output file" });
      alert("Export failed: no output file.");
      return;
    }

    setProgress(100);
    if (label) label.textContent = "Download starting…";
    await new Promise(r => setTimeout(r, 500));
    window.posthog?.capture("export_completed", { path: exportPath, output_file: outputFile });

    const a = Object.assign(document.createElement("a"), {
      href: `/download/${outputFile}`, download: outputFile,
    });
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    if (overlay) overlay.style.display = "none";
  });

  async function pollExportJob(jobId, onProgress) {
    // Polls an async FFmpeg export job until done/error (15 min cap).
    const deadline = Date.now() + 15 * 60 * 1000;
    let job = null;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 600));
      const r = await fetch(`/export/status/${jobId}`);
      if (!r.ok) throw new Error(`Status check failed (${r.status})`);
      job = await r.json();
      if (typeof job.progress === "number") onProgress(job.progress);
      if (job.status === "done") return job;
      if (job.status === "error") throw new Error(job.error || "FFmpeg failed.");
    }
    throw new Error("Export timed out.");
  }

  function _buildExportEdits() {
    const edits = [];

    // Crop
    if (scene.video.crop.applied) {
      const c = scene.video.crop;
      edits.push({ type: "crop", nx: c.x, ny: c.y, nw: c.w, nh: c.h });
    }

    // Canvas
    if (scene.canvas.background) edits.push({ type: "background", color: scene.canvas.background });
    if (scene.canvas.speed && scene.canvas.speed !== 1.0) edits.push({ type: "speed", speed: scene.canvas.speed });
    if (scene.canvas.trim) edits.push({ type: "trim", ...scene.canvas.trim });

    // Exports are ALWAYS 9:16 (1080x1920) in FIT mode: the picture is
    // fitted and centered inside the frame (letterbox bars filled with
    // the scene background) — identical to the client-side engine and
    // the preview. The legacy server path scales to fit and pads.
    edits.push({
      type: "resize_canvas",
      aspect_ratio: "9:16",
      color: scene.canvas.background || "#000000",
    });

    // Elements
    const sorted = [...scene.elements].sort((a, b) => (a.zIndex||0) - (b.zIndex||0));
    for (const el of sorted) {
      const p = el.props || {};
      if (el.type === "banner") {
        const isBot = (p.position || "top").includes("bottom");
        // Merge child text elements into the banner edit so the
        // server draws the real sentence inside the bar.
        const kids = scene.elements.filter(e => e.type === "text" && e.parentId === el.id);
        const kid  = kids.length ? (kids[kids.length - 1].props || {}) : {};
        const k = kOutFactor(); // preview px → output px
        // Banners are always flush to the video edges (bar offsets are 0
        // everywhere) — matching bannerSnapY(), the client engine, and the
        // server FFmpeg path.
        // Banner height as a fraction of the PICTURE height (the fitted
        // video rect, not the container). This is resolution- and
        // letterbox-proof: the server multiplies it by the cropped
        // frame height, so the drawn banner is the same size relative to
        // the picture as in the preview — even when the picture doesn't
        // span the full 1080x1920 output frame.
        const _vr = getVideoRect();
        const heightFrac = _vr.h > 0
          ? Math.min(1, Math.max(0, (el._pixelHeight || 0) / _vr.h))
          : 0;
        edits.push({
          type:       isBot ? "bottom_banner" : "top_banner",
          id:         el.id,
          // Always flush to the video edges — bar offsets are 0 everywhere
          // (matches bannerSnapY(), the client engine, and the server).
          top_bar_frac:    0,
          bottom_bar_frac: 0,
          // Exact geometry so the FFmpeg fallback draws the SAME banner
          // the preview shows (no server-side re-measurement).
          x: 0,
          y: el.y || 0,
          w: 1,
          h: el.height || 0.1,
          // Cleaned-band banner: send the band top so FFmpeg draws the
          // text inside the baked band (no box is drawn for transparent).
          y_frac: (String(p.backgroundColor||"").toLowerCase() === "transparent" ? (el.y || 0) : 0),
          // Banner height as a fraction of the picture height (see above).
          height_frac:   heightFrac,
          // Legacy output-px value (kept for old servers / debugging).
          height_px:     Math.round((el._pixelHeight || 0) * k),
          font_size_out: Math.round(el._bannerFontSize || AQ_TYPO.banner.defaultFs),
          // The ACTUAL wrapped lines from the preview — FFmpeg draws these
          // verbatim instead of re-wrapping with different metrics.
          lines:       Array.isArray(el._textLines) ? el._textLines : null,
          line_height: el._lineHeight || AQ_TYPO.banner.lineHeight,
          text:       p.text || p.content || kid.text || kid.content || "",
          bg_color:   p.backgroundColor || p.bg_color || (isBot ? "#000000" : "#ffffff"),
          text_color: kid.color || kid.textColor || kid.text_color ||
                      p.color || p.textColor || p.text_color ||
                      (isBot ? "#ffffff" : "#000000"),
          font_size:  p.fontSize || p.font_size || kid.fontSize || kid.font_size || 22,
          font:       p.fontFamily || p.font || kid.fontFamily || kid.font || undefined,
        });
      } else if (el.type === "text") {
        // Skip texts parented to banners — already merged above.
        const parentEl = scene.elements.find(e => e.id === el.parentId);
        if (parentEl && parentEl.type === "banner") continue;
        edits.push({
          type:       "overlay_text",
          id:         el.id,
          text:       p.text || p.content || "",
          text_color: p.color || p.textColor || p.text_color || "#ffffff",
          // Preview default comes from the typography engine (30px @1080).
          font_size:     Number(p.fontSize || p.font_size || outPxToPreviewPx(AQ_TYPO.text.defaultFs)),
          font_size_out: Math.round(Number(p.fontSize || p.font_size || outPxToPreviewPx(AQ_TYPO.text.defaultFs)) * kOutFactor()),
          // Exact normalized rect — replaces the lossy top/center/bottom
          // position keyword in the FFmpeg fallback.
          x:         el.x ?? 0,
          y:         el.y ?? 0,
          w:         el.width ?? 1,
          h:         el.height ?? 0.12,
          textAlign: p.textAlign || p.alignment || "center",
          position:  el.parentId ? "center" : (el.y < 0.2 ? "top" : el.y > 0.7 ? "bottom" : "center"),
        });
      } else if (el.type === "shape") {
        edits.push({
          type: "shape", id: el.id,
          // Exact normalized rect (matches getWorldRect resolution)
          x: el.x ?? 0, y: el.y ?? 0,
          w: el.width ?? 0.3, h: el.height ?? 0.2,
          ...p,
        });
      } else if (el.type === "image" || el.type === "logo") {
        edits.push({
          type: el.type, id: el.id,
          x: el.x ?? 0, y: el.y ?? 0,
          w: el.width ?? 0.4, h: el.height ?? 0.3,
          ...p,
        });
      }
    }

    return edits;
  }

  // ─────────────────────────────────────────────────────────────
  // RESIZE + METADATA
  // ─────────────────────────────────────────────────────────────
  let _resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
      stage.width(container.clientWidth);
      stage.height(container.clientHeight);
      _lastCropKey = null;
      renderScene();
    }, 100);
  });

  videoEl.addEventListener("loadedmetadata", () => {
    scene.video.naturalWidth  = videoEl.videoWidth;
    scene.video.naturalHeight = videoEl.videoHeight;
    scene.video.duration      = videoEl.duration;
    scene.video.filename      = getFilename();
    _lastCropKey = null;
    renderScene();
  });

  // Frame pixels are only guaranteed from "loadeddata" on. Re-render so
  // detectContentBars() can scan a real frame and snap banners to the
  // first row of picture content (a metadata-time render scans nothing).
  videoEl.addEventListener("loadeddata", () => {
    renderScene();
  });

  // Seeking changes which frame is on screen, so the baked-bar scan
  // (detectContentBars) can change. Invalidate the scan cache and re-render
  // so the banner re-snaps to the actual picture edge instead of keeping an
  // offset frozen from a stale frame.
  videoEl.addEventListener("seeked", () => {
    _contentBarCache.key = null;
    renderScene();
  });

  if (videoEl.readyState >= 1) {
    scene.video.naturalWidth  = videoEl.videoWidth;
    scene.video.naturalHeight = videoEl.videoHeight;
    scene.video.duration      = videoEl.duration;
    scene.video.filename      = getFilename();
  }

  // ─────────────────────────────────────────────────────────────
  // MOBILE KEYBOARD
  // ─────────────────────────────────────────────────────────────
  const promptInputEl = document.querySelector(".promptInput");
  let _kbOpen = false, _kbTimer = null;
  promptInputEl?.addEventListener("focus", () => {
    clearTimeout(_kbTimer);
    setTimeout(() => { _kbOpen = true; document.body.classList.add("keyboard-open"); }, 100);
  });
  promptInputEl?.addEventListener("blur", () => {
    clearTimeout(_kbTimer);
    _kbTimer = setTimeout(() => { _kbOpen = false; document.body.classList.remove("keyboard-open"); }, 300);
  });

  // ─────────────────────────────────────────────────────────────
  // CHUNK UPLOAD
  // ─────────────────────────────────────────────────────────────
  const uploadForm   = document.getElementById("uploadForm");
  const videoFileIn  = document.getElementById("videoInput") || document.getElementById("videoFile");
  const CHUNK        = 8 * 1024 * 1024;
  const MAX_PAR      = 4;

  async function uploadChunked(file) {
    const total   = Math.ceil(file.size / CHUNK);
    const overlay = document.getElementById("uploadOverlay");
    const bar     = document.getElementById("progressBar");
    const pct     = document.getElementById("uploadPercent");
    const label   = document.getElementById("uploadLabel");

    if (overlay) overlay.style.display = "flex";
    let done = 0, finalFn = null;

    async function sendChunk(i) {
      const fd = new FormData();
      fd.append("chunk",       file.slice(i * CHUNK, Math.min((i+1)*CHUNK, file.size)));
      fd.append("filename",    file.name);
      fd.append("chunkIndex",  i);
      fd.append("totalChunks", total);
      const r = await fetch("/upload-chunk", { method: "POST", body: fd });
      if (!r.ok) throw new Error(`Chunk ${i} failed (${r.status})`);
      const d = await r.json();
      done++;
      const p2 = Math.round(done / total * 100);
      if (bar) bar.style.width = `${p2}%`;
      if (pct) pct.textContent = `${p2}%`;
      if (d.status === "complete") finalFn = d.filename;
    }

    const q = Array.from({length: total}, (_,i) => i);
    async function worker() { while (q.length) await sendChunk(q.shift()); }

    try {
      await Promise.all(Array.from({length: Math.min(MAX_PAR, total)}, () => worker()));
      if (!finalFn) throw new Error("No filename from server");
      if (label) label.textContent = "Processing…";
      if (bar) bar.style.width = "100%";
      if (pct) pct.textContent = "100%";
      window.location.href = `/canvas/${finalFn}`;
    } catch(err) {
      if (overlay) overlay.style.display = "none";
      throw err;
    }
  }

  uploadForm?.addEventListener("submit", async e => {
    e.preventDefault();
    const file = videoFileIn?.files?.[0];
    if (!file) { alert("Please select a video."); return; }
    try { await uploadChunked(file); }
    catch(err) { console.error("Upload failed:", err); alert(`Upload failed: ${err.message}`); }
  });

  // ─────────────────────────────────────────────────────────────
  // INIT
  // ─────────────────────────────────────────────────────────────
  saveHistory();
  _refreshUndoUI();

  console.log("%c[Autoquence V3] Full architectural rework — canonical scene model, isolated video geometry, parent/child relationships", "font-weight:bold;color:#4f8ef7;font-size:13px");
});