/**
 * sync.js - Autoquence timeline page (images + optional voiceover).
 *
 * The timeline is IMAGE-DRIVEN: every uploaded image becomes a block the
 * moment the page loads, so the order and the timing of each image are
 * editable before - or entirely without - any transcription. Speech
 * segments, when they exist, only supply the per-image screen times.
 *
 * Playback has two clocks: the <audio> element once a voiceover is
 * loaded, and a requestAnimationFrame timer otherwise. That is why the
 * play button and the preview work with no audio at all.
 *
 * Dragging uses Pointer Events (mouse, touch and pen) instead of HTML5
 * drag-and-drop, which has no touch support and therefore never worked
 * on a phone or tablet.
 */
(function () {
  "use strict";

  const JOB_ID = window.location.pathname.split("/").pop();
  const $ = (id) => document.getElementById(id);

  // Every id used here must exist in templates/sync.html - a missing one
  // used to make the whole script bail out with no visible error.
  const imgEl       = $("syncPreviewImg");
  const audioEl     = $("syncAudio");
  const playBtn     = $("syncPlayBtn");
  const addVoiceBtn = $("syncAddVoice");
  const addImagesBtn = $("syncAddImages");
  const imageTrack  = $("syncImageTrack");
  const audioTrack  = $("syncAudioTrack");
  const audioBlock  = $("syncAudioBlock");
  const audioInput  = $("syncAudioInput");
  const imageInput  = $("syncImageInput");
  const ruler       = $("syncRuler");
  const scrollEl    = $("syncScroll");
  const laneStack   = $("syncLaneStack");
  const mismatchEl  = $("syncMismatch");
  const msgEl       = $("syncMsg");
  const msgText     = $("syncMsgText");
  const msgCancel   = $("syncMsgCancel");
  const noteEl      = $("syncNote");
  const downloadEl  = $("downloadBtn");

  // Rendering state: blurred preview + sharp on-canvas label (the old
  // floating progress card is gone).
  const previewBox    = document.querySelector(".sync-canvas");
  const renderOverlay = $("syncRenderOverlay");
  const renderText    = $("syncRenderText");

  // Add prompt modal: timestamped script -> timeline + segment retiming.
  const promptBtn    = $("syncAddPrompt");
  const promptModal  = $("promptModal");
  const promptCancel = $("promptCancel");
  const promptText   = $("promptText");
  const promptApply  = $("promptApply");
  const promptError  = $("promptError");

  // Apply progress popup: staged updates while the script is applied
  // and saved (loading -> prompt received -> detected X timestamps ->
  // loading X images -> saved). Its Cancel button owns the run: it can
  // fire at ANY stage, never a control parked at the bottom of the page.
  const progressEl     = $("promptProgress");
  const progressText   = $("promptProgressText");
  const progressCancel = $("promptProgressCancel");

  // Beats-from-audio popup: shows the job's existing beat plan as ONE
  // copyable "m:ss - description" prompt - the exact line format
  // parsePromptLines() accepts, so a copy pastes straight back into
  // Add prompt (or into any image tool).
  const genBeatsBtn    = $("syncGenBeats");
  const beatsModal     = $("beatsModal");
  const beatsPrompt    = $("beatsPromptText");
  const beatsError     = $("beatsError");
  const beatsCopyBtn   = $("beatsCopyBtn");
  const beatsCopyLabel = $("beatsCopyLabel");
  const beatsClose     = $("beatsClose");

  // Transcribing status popup (Generate beats): shown while Whisper is
  // still running - it closes itself on completion, then the beats
  // popup above takes over.
  const beatsProgress       = $("beatsProgress");
  const beatsProgressText   = $("beatsProgressText");
  const beatsProgressCancel = $("beatsProgressCancel");

  const DEFAULT_CLIP_SECONDS = 3.0;
  const MIN_CLIP_SECONDS = 0.5;
  const MAX_CLIP_SECONDS = 60.0;

  // Fixed timeline scale. A 3s image is always ~96px (~2.5cm on screen at
  // 96dpi), so adding images LENGTHENS the timeline and the user scrolls
  // through it - instead of every block being squeezed as the total grows.
  const PX_PER_SECOND = 32;
  const MIN_BLOCK_PX = 22;   // keeps very short clips grabbable

  const state = {
    images: [],    // image URLs, index = upload order
    clips: [],     // [{image: index into images, duration: seconds}]
    segments: [],  // speech segments from the server (may be empty)
    matched: false,
    status: "",      // last polled job status (drives the beats popup)
    hasAudio: false,
    audioName: "",     // original upload filename (from the server)
    audioDuration: 0,   // seconds, from the <audio> element
    sig: "",       // fingerprint, so status polls don't clobber edits
    prompts: "",   // raw timestamped script persisted in the manifest
    imageWarning: "",  // server note when two uploads are the same picture
  };

  // Waveform peaks cache: recomputed only when the audio URL changes.
  let wavePeaks = null;
  let waveFor = "";

  // Playback clock
  let playMode = "timer";   // "audio" once a voiceover is loaded
  let playing = false;
  let playOffset = 0;       // seconds, timer mode
  let playStart = 0;        // performance.now() when timer play began
  let rafId = null;

  let selectedIndex = -1;
  let dragActive = false;
  let draggedRecently = 0;
  let persistTimer = null;
  let copyLabelTimer = null;   // flips the beats "Copy" label back after 2.2s
  let pinnedClip = -1;   // clip index held during drag/resize so the preview doesn't flip

  // ─────────────────────────────────────────────────────────────
  // Small helpers
  // ─────────────────────────────────────────────────────────────
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  function num(v, fallback) {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  }

  function fmt(seconds) {
    const s = Math.max(0, Math.round(seconds));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m ? `${m}:${String(r).padStart(2, "0")}` : `${r}s`;
  }

  function escapeHtml(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // Status/errors: ERRORS open the popup with a Cancel button (the old
  // floating pill is gone). Routine statuses (transcribing, ready, ...)
  // are silent - the old hint line was removed.
  function showMsg(text, isError) {
    if (!isError) return;
    if (msgText) msgText.textContent = text;
    else if (msgEl) msgEl.textContent = text;
    if (msgEl) {
      msgEl.classList.toggle("error", true);
      msgEl.style.display = "flex";
    }
  }

  msgCancel?.addEventListener("click", () => {
    if (msgEl) msgEl.style.display = "none";
  });

  // ─────────────────────────────────────────────────────────────
  // Server data -> timeline
  // ─────────────────────────────────────────────────────────────

  /**
   * One clip per timeline block. With segments the server's speech timings
   * decide how long each image shows; without them every image gets the
   * default length so the timeline is usable immediately.
   */
  function buildClips(images, segments, prevClips) {
    const prev = prevClips || [];
    if (!images.length) return [];

    if (!segments.length) {
      return images.map((_, i) => {
        const keep = prev.find((c) => c.image === i);
        return {
          image: i,
          duration: keep ? keep.duration : DEFAULT_CLIP_SECONDS,
        };
      });
    }

    const last = segments[segments.length - 1];
    const audioEnd = num(last && last.end, 0);

    return segments.map((seg, i) => {
      const start = Math.max(0, num(seg.start, 0));
      const end = num(seg.end, start + DEFAULT_CLIP_SECONDS);
      // Hold until the next sentence starts so pauses stay on the current
      // image; the final image holds to the end of the voiceover.
      const hold = i + 1 < segments.length
        ? Math.max(num(segments[i + 1].start, end), end)
        : Math.max(end, audioEnd);
      // The first block also covers any leading silence, so timeline time
      // maps 1:1 onto audio time and the preview never drifts.
      const blockStart = i === 0 ? 0 : start;
      const image = Number.isInteger(seg.image)
        ? seg.image
        : clamp(Math.floor((i * images.length) / segments.length),
            0, images.length - 1);
      return {
        image,
        duration: Math.max(MIN_CLIP_SECONDS, hold - blockStart),
      };
    });
  }

  function applyServerData(data) {
    const images = data.image_urls || [];
    const segments = data.segments || [];
    const sig = `${images.length}:${segments.length}:${data.status}`;

    state.hasAudio = !!data.has_audio;
    playMode = state.hasAudio ? "audio" : "timer";
    // Export is refused while Whisper is still working: the timeline
    // has no synced times yet. The Export click handler checks
    // state.status and shows a hint instead of a disabled button.

    if (typeof data.audio_name === "string" && data.audio_name) {
      state.audioName = data.audio_name;
    } else if (data.audio_url) {
      // Old jobs have no stored name: fall back to the file in the URL.
      try {
        state.audioName = decodeURIComponent(
          String(data.audio_url).split("/").pop().split("?")[0]) || state.audioName;
      } catch (e) { /* keep the previous name */ }
    }

    if (state.hasAudio && data.audio_url
        && audioEl.getAttribute("src") !== data.audio_url) {
      audioEl.src = data.audio_url;
    }

    const rebuild = sig !== state.sig || !state.clips.length;
    state.images = images;
    state.segments = segments;
    state.matched = !!data.matched;
    state.sig = sig;
    state.status = typeof data.status === "string" ? data.status : "";
    if (typeof data.prompts === "string") state.prompts = data.prompts;
    // Server-side duplicate-picture note (it owns the stored files, and
    // only it can hash them): "" clears a stale note after a re-upload.
    if (typeof data.image_warning === "string") {
      state.imageWarning = data.image_warning;
    }

    if (!rebuild) return;

    // A timeline saved on the server wins on first load; later polls only
    // rebuild when the fingerprint changed, so an in-progress edit is safe.
    const saved = Array.isArray(data.clips) ? data.clips : [];
    const useSaved = !segments.length && saved.length > 0;
    state.clips = useSaved
      ? saved
        .filter((c) => Number.isInteger(c.image)
          && c.image >= 0 && c.image < images.length)
        .map((c) => ({
          image: c.image,
          duration: clamp(num(c.duration, DEFAULT_CLIP_SECONDS),
            MIN_CLIP_SECONDS, MAX_CLIP_SECONDS),
        }))
      : buildClips(images, segments, state.clips);

    if (!state.clips.length && images.length) {
      state.clips = buildClips(images, [], state.clips);
    }
    // A saved script must survive a rebuild that fell back to defaults
    // (its clip timings would otherwise be silently discarded - the
    // original "script applied but the render ignored it" bug). With
    // segments present reapplySavedPrompts is a no-op: timing follows
    // the sentences by design.
    if (!useSaved && !segments.length && state.prompts) {
      reapplySavedPrompts();
    }
    selectedIndex = -1;
  }

  // ─────────────────────────────────────────────────────────────
  // Boot: poll the job status
  // ─────────────────────────────────────────────────────────────
  async function boot() {
    const deadline = Date.now() + 3 * 60 * 1000; // transcription cap
    while (Date.now() < deadline) {
      let data;
      try {
        const res = await fetch(`/sync/status/${JOB_ID}`);
        data = await res.json();
        if (!res.ok) throw new Error(data.error || `Status ${res.status}`);
      } catch (err) {
        showMsg(err.message, true);
        return;
      }

      applyServerData(data);
      if (!dragActive) render();

      if (data.status === "ready") {
        paintVoiceoverChip(true);
        if (!state.images.length) {
          showMsg("Voiceover ready - add images to match it.");
        } else {
          showMsg("Ready - drag blocks to reorder, drag a right edge to retime, "
            + "then Export.");
        }
        return;
      }

      if (data.status === "error") {
        // Transcription failed (e.g. no API key). The timeline still works:
        // the images can be timed by hand and built as a silent slideshow.
        paintVoiceoverChip(false);
        showMsg(`Transcription failed: ${data.error} - you can still reorder, `
          + "retime and build a silent slideshow.", true);
        return;
      }

      if (data.status === "awaiting_audio") {
        // Images-only job: nothing to wait for. The timeline is already
        // usable, and uploadVoiceover() calls boot() again once a
        // voiceover is added, so there is no reason to keep polling.
        paintVoiceoverChip(false);
        showMsg("Add a voiceover to sync the images to speech - or drag the "
          + "blocks, retime them and build a silent slideshow right now.");
        return;
      }

      showMsg(data.status === "transcribing"
        ? "Transcribing voiceover…"
        : "Loading…");

      await new Promise((r) => setTimeout(r, 1500));
    }
    showMsg("Timed out waiting for the voiceover - you can still build "
      + "the slideshow.", true);
  }

  // ─────────────────────────────────────────────────────────────
  // Timeline rendering (driven by state.clips, not by segments)
  // ─────────────────────────────────────────────────────────────
  function totalDuration() {
    return state.clips.reduce((sum, c) => sum + c.duration, 0);
  }

  /**
   * Size the lane stack from the timeline length at the fixed scale. The
   * CSS min-width:100% keeps a short timeline filling the panel, so the
   * unused part simply shows as background.
   */
  function applyLaneWidth() {
    if (!laneStack) return;
    const content = totalDuration() * PX_PER_SECOND;
    laneStack.style.width = content > 0 ? `${content}px` : "100%";
  }

  /** Scroll the timeline so a position (px) is inside the visible area. */
  function keepPxVisible(px, margin) {
    if (!scrollEl) return;
    const m = margin == null ? 70 : margin;
    const view = scrollEl.clientWidth;
    if (!view) return;
    const left = scrollEl.scrollLeft;
    if (px < left + m) scrollEl.scrollLeft = Math.max(0, px - m);
    else if (px > left + view - m) scrollEl.scrollLeft = px - view + m;
  }

  /** Nudge the timeline along while a drag/resize nears either edge, so a
   *  long timeline can still be reordered or retimed end to end. */
  function edgeScroll(clientX) {
    if (!scrollEl) return;
    const rect = scrollEl.getBoundingClientRect();
    const EDGE = 44;
    if (clientX < rect.left + EDGE) {
      scrollEl.scrollLeft = Math.max(0, scrollEl.scrollLeft - 12);
    } else if (clientX > rect.right - EDGE) {
      scrollEl.scrollLeft += 12;
    }
  }

  /**
   * Size the audio block to the voiceover's real length at the same fixed
   * scale as the images, so the two lanes line up and you can see whether
   * the images outlast the narration. Without audio the chip just hugs its
   * content instead of stretching across the whole timeline.
   */
  function applyAudioWidth() {
    try {
      const d = Number(audioEl && audioEl.duration);
      if (Number.isFinite(d) && d > 0) state.audioDuration = d;
    } catch (e) { /* keep 0 */ }
    if (!audioBlock) return;
    const dur = audioEl && isFinite(audioEl.duration) && audioEl.duration > 0
      ? audioEl.duration
      : 0;
    if (dur) {
      audioBlock.style.flex = "none";
      audioBlock.style.width =
        `${Math.max(MIN_BLOCK_PX, dur * PX_PER_SECOND)}px`;
    } else {
      audioBlock.style.flex = "0 0 auto";
      audioBlock.style.width = "";
    }
  }

  /** Index of the clip playing at time t (clamped to the timeline). */
  function clipAt(t) {
    if (!state.clips.length) return -1;
    if (t <= 0) return 0;
    let acc = 0;
    for (let i = 0; i < state.clips.length; i++) {
      acc += state.clips[i].duration;
      if (t < acc) return i;
    }
    return state.clips.length - 1;
  }

  function render() {
    const total = totalDuration();
    imageTrack.innerHTML = "";

    // The playhead is created once and reused: render() runs after every
    // edit, so appending a fresh one would stack green lines.
    if (!document.getElementById("syncPlayhead")) {
      const ph = document.createElement("div");
      ph.id = "syncPlayhead";
      audioTrack.appendChild(ph);
    }

    state.clips.forEach((clip, i) => {
      const block = document.createElement("div");
      block.className = "sync-img-block"
        + (i === selectedIndex ? " selected" : "");
      block.dataset.index = String(i);
      // Fixed scale: width depends only on this clip's own duration, so it
      // never changes when other images are added or removed.
      block.style.width =
        `${Math.max(MIN_BLOCK_PX, clip.duration * PX_PER_SECOND)}px`;

      // Filmstrip: the picture is painted as a repeating background at a
      // constant scale (see .sync-img-block), so a longer clip only shows
      // more of the strip - the photo itself never magnifies.
      if (state.images[clip.image]) {
        block.style.backgroundImage = `url("${state.images[clip.image]}")`;
      }
      block.innerHTML =
        `<span class="blockNum">${i + 1}</span>`
        + `<span class="blockDur">${clip.duration.toFixed(1)}s</span>`
        + `<span class="blockHandle" title="Drag to change how long this image shows"></span>`;

      imageTrack.appendChild(block);
    });

    // Ruler ticks, one per `step` seconds at the fixed scale. A tick that
    // would spill past the end of the timeline is skipped, otherwise its
    // half-width label widens the scroll area by a few pixels.
    ruler.innerHTML = "";
    const span = total || 1;
    const contentPx = span * PX_PER_SECOND;
    const step = span > 120 ? 30 : span > 60 ? 10 : span > 20 ? 5 : 2;
    for (let t = 0; t <= span; t += step) {
      if (t > 0 && (t * PX_PER_SECOND) + 26 > contentPx) break;
      const tick = document.createElement("span");
      tick.style.left = `${t * PX_PER_SECOND}px`;
      tick.textContent = fmt(t);
      ruler.appendChild(tick);
    }

    renderMarkers();
    renderNote();
    applyLaneWidth();
    applyAudioWidth();
    // Lane/audio widths may have just changed (stale wave bitmap = blur).
    drawWave();

    updateBanner();
    updatePreview();
    updatePlayhead();
    wireTimelineEvents();
  }

  /** Duplicate-picture note: a picture cannot cut to itself, so a repeated
   *  upload looks "out of sync" even when every beat is exact. */
  function renderNote() {
    if (!noteEl) return;
    if (state.imageWarning) {
      noteEl.textContent = state.imageWarning;
      noteEl.style.display = "block";
    } else {
      noteEl.textContent = "";
      noteEl.style.display = "none";
    }
  }

  function updateBanner() {
    if (!mismatchEl) return;
    const nImg = state.images.length;
    const nSeg = state.segments.length;
    const warn = imageWarnHtml();

    if (!nImg) {
      mismatchEl.style.display = "block";
      if (state.hasAudio) {
        mismatchEl.innerHTML =
          "<strong>No images yet - add images to match the voiceover.</strong> "
          + "The beats below suggest how many to make; the timeline syncs "
          + "itself when they arrive.";
      } else {
        mismatchEl.innerHTML =
          "<strong>No images in this job.</strong> Go back and upload images "
          + "to build the video.";
      }
      return;
    }

    if (!nSeg) {
      // Images-only: the timeline is fully usable, so this is a hint with
      // an action rather than an error.
      mismatchEl.style.display = "block";
      const total = totalDuration();
      mismatchEl.innerHTML =
        `<strong>${nImg} image${nImg === 1 ? "" : "s"} ready - `
        + `${total.toFixed(1)}s timeline, no voiceover timestamps.</strong> `
        + `Drag a block to reorder it, drag a block's <strong>right edge</strong> `
        + `to change how long it shows, and drag on the ruler to scrub. `
        + `Tap <strong>Export</strong> when you're happy.`
        + warn;
      return;
    }

    if (!state.matched) {
      mismatchEl.style.display = "block";
      if (nImg <= nSeg) {
        mismatchEl.innerHTML =
          `<strong>${nImg} images, ${nSeg} speech segments.</strong> Tap `
          + `<strong>Auto-match</strong> to spread the images across the `
          + `segments, or fine-tune by hand: drag a block to reorder, `
          + `drag its right edge to retime it.`
          + `<button id="syncAutoMatch" type="button">Auto-match `
          + `${nImg} → ${nSeg}</button>`
          + warn;
        document.getElementById("syncAutoMatch")
          ?.addEventListener("click", applyAutoMatch);
      } else {
        mismatchEl.innerHTML =
          `<strong>${nImg} images but only ${nSeg} speech segments.</strong> `
          + `Add a voiceover segment per image (or upload fewer images) so `
          + `every image gets its own slot. The timeline still builds as a `
          + `slideshow right now.`
          + warn;
      }
      return;
    }

    // All matched: the only thing left to say is the duplicate-picture
    // note (beat timing itself is fine - a repeated picture just cannot
    // produce a visible cut).
    if (warn) {
      mismatchEl.style.display = "block";
      mismatchEl.innerHTML = warn;
      return;
    }

    mismatchEl.style.display = "none";
  }

  // Decode the voiceover into peaks (0..1) for the waveform canvas.
  // Peak work is chunked so a long file never freezes the page; on any
  // failure the chip still shows the name + duration as a fallback.
  async function computeWavePeaks(url) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    const res = await fetch(url, { cache: "force-cache" });
    if (!res.ok) return null;
    const buf = await res.arrayBuffer();
    const Ctx = new AC();
    let decoded = null;
    try {
      decoded = await Ctx.decodeAudioData(buf);
    } finally {
      if (Ctx.close) { try { await Ctx.close(); } catch (e) {} }
    }
    if (!decoded) return null;
    const ch = decoded.getChannelData(0);
    // ~16 bars/sec (96..4096): drawWave downsamples to the box's pixel
    // width, so finer data only means a sharper wave on a wide/stretched
    // lane — never more drawing work than there are pixels.
    const bars = Math.max(96, Math.min(4096, Math.round(decoded.duration * 16)));
    const step = Math.max(1, Math.floor(ch.length / bars));
    const peaks = new Array(Math.ceil(ch.length / step));
    for (let b = 0, i = 0; i < ch.length; b++, i += step) {
      let m = 0;
      const end = Math.min(i + step, ch.length);
      // Sample every 7th frame: plenty for one bar, ~7x less work.
      for (let j = i; j < end; j += 7) {
        const v = Math.abs(ch[j]);
        if (v > m) m = v;
      }
      peaks[b] = m;
      // Yield every 64 bars so the UI stays responsive.
      if ((b & 63) === 63) await new Promise((r) => setTimeout(r, 0));
    }
    // Normalize so a quiet recording still fills the lane.
    let top = 0;
    for (const p of peaks) if (p > top) top = p;
    if (top > 0.01) for (let b = 0; b < peaks.length; b++) peaks[b] /= top;
    return peaks;
  }

  // Paint the cached peaks into the chip canvas at the timeline scale.
  //
  // The canvas is CSS-stretched (width:100%) by its block, so the backing
  // store must always match the CURRENT box size x devicePixelRatio. A
  // bitmap drawn at an older size gets rescaled by the browser and turns
  // fuzzy the moment the lane, window or zoom changes — the "blurry wave
  // when stretched" bug. render(), the ResizeObserver and the
  // resize/zoom listeners below all funnel back here.
  function drawWave() {
    const cv = document.getElementById("syncWaveCanvas");
    if (!cv || !wavePeaks || !wavePeaks.length) return;
    const w = cv.clientWidth
      || (cv.parentElement && cv.parentElement.clientWidth) || 1;
    const h = 40;
    // devicePixelRatio includes browser zoom; cap at 3x for memory and
    // clamp to ~16k canvas pixels so a very long audio still draws (at
    // lower density) instead of exceeding the canvas dimension limit.
    const dpr = Math.min(3, window.devicePixelRatio || 1,
      Math.max(1, Math.floor(16384 / w)));
    const backW = Math.round(w * dpr);
    const backH = Math.round(h * dpr);
    if (cv.width !== backW || cv.height !== backH) {
      cv.width = backW;
      cv.height = backH;
    }
    const ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.fillStyle = "#2dee06";
    // One group per >=1 backing pixel, each drawn with its group's max
    // peak and edges snapped to whole pixels: no fractional fill widths,
    // so no antialiased hairlines between bars (the old ceil(bw)-0.4
    // left ~0.4px translucent gaps that read as fuzz on a wide wave).
    const bars = wavePeaks.length;
    const groups = Math.min(bars, cv.width);
    const gw = cv.width / groups;
    for (let g = 0; g < groups; g++) {
      const i0 = Math.floor(g * bars / groups);
      const i1 = Math.max(i0 + 1, Math.floor((g + 1) * bars / groups));
      let v = 0;
      for (let i = i0; i < bars && i < i1; i++) {
        if (wavePeaks[i] > v) v = wavePeaks[i];
      }
      v = Math.max(0.06, v);
      const x0 = Math.round(g * gw);
      const x1 = Math.round((g + 1) * gw);
      const barH = Math.round(v * cv.height);
      ctx.fillRect(x0, Math.round((cv.height - barH) / 2),
        Math.max(1, x1 - x0), barH);
    }
  }

  // Keep the bitmap matched to its box. CSS width:100% means any change
  // to the block (lane edits + the max-width clamp, window resize, zoom)
  // rescales whatever bitmap exists — without these hooks the browser
  // stretches a stale one into blur.
  if (window.ResizeObserver && audioBlock) {
    new ResizeObserver(() => drawWave()).observe(audioBlock);
  }
  window.addEventListener("resize", () => drawWave());
  (function watchDpr() {
    // Browser/OS zoom changes devicePixelRatio with no layout change, so
    // no resize event fires — a resolution media query does. Re-arm after
    // every change so the next zoom is caught too.
    if (!window.matchMedia) return;
    let q;
    try {
      q = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
    } catch (e) { return; }
    const onChange = () => { drawWave(); watchDpr(); };
    if (q.addEventListener) q.addEventListener("change", onChange, { once: true });
    else if (q.addListener) q.addListener(onChange);
  })();

  function waveFileLabel() {
    // Never show the internal stored name (audio.mp3): prefer the real
    // upload name, and only fall back to the URL tail for old jobs.
    if (state.audioName && !/^audio\.[a-z0-9]+$/i.test(state.audioName)) {
      return state.audioName;
    }
    const src = audioEl.getAttribute("src") || audioEl.src || "";
    const tail = String(src).split("/").pop().split("?")[0];
    try {
      const dec = decodeURIComponent(tail);
      if (dec && !/^audio\.[a-z0-9]+$/i.test(dec)) return dec;
    } catch (e) { /* fall through */ }
    return state.audioName || "Voiceover";
  }

  function paintVoiceoverChip(hasAudio) {
    if (!audioBlock) return;
    if (hasAudio) {
      paintVoiceoverLoaded();
      return;
    }
    audioBlock.innerHTML =
      `<span class="chipTitle">Tap to add Audio</span>`
      + `<span class="chipSub"></span>`;
  }

  // Loaded voiceover: filename + duration over a waveform canvas sized
  // to the real audio length at the timeline scale. If decode fails the
  // canvas simply stays empty - name + duration still show.
  function paintVoiceoverLoaded() {
    if (!audioBlock) return;
    const label = waveFileLabel();
    const dur = state.audioDuration > 0 ? state.audioDuration : null;
    audioBlock.classList.remove("add-voiceover");
    audioBlock.innerHTML =
      `<canvas id="syncWaveCanvas" class="waveCanvas"></canvas>`
      // + `<span class="chipTitle">${escapeHtml(label)}${dur != null ? " \u00b7 " + fmt(dur) : ""}</span>`
      + `<span class="chipSub"></span>`;
    applyAudioWidth();
    drawWave();
    const src = audioEl.getAttribute("src") || audioEl.src || "";
    if (src && waveFor !== src) {
      waveFor = src;
      wavePeaks = null;
      computeWavePeaks(src).then((peaks) => {
        if (!peaks || !peaks.length) return;
        if ((audioEl.getAttribute("src") || audioEl.src || "") !== src) return;
        wavePeaks = peaks;
        drawWave();
      }).catch(() => { /* name + duration fallback stays */ });
    } else {
      drawWave();
    }
  }

  /** Confirm the on-screen arrangement: save it, then re-check the match. */
  function applyAutoMatch() {
    persistNow().then(() => {
      showMsg(state.matched
        ? "Saved - every image has a slot. Tap Export."
        : "Saved. Some images have no slot yet - add or split segments.");
    });
  }

  // ─────────────────────────────────────────────────────────────
  // Timeline interactions: reorder (drag) + retime (right edge)
  //
  // Pointer Events are used instead of HTML5 drag-and-drop so this works
  // on touch screens as well as with a mouse. A drag only starts after a
  // few pixels of movement so a plain tap still selects a block.
  // ─────────────────────────────────────────────────────────────
  function wireTimelineEvents() {
    [...imageTrack.children].forEach((block, i) => {
      block.addEventListener("pointerdown", (e) => {
        if (e.pointerType === "mouse" && e.button !== 0) return;
        if (e.target.closest(".blockHandle")) startResize(e, i);
        else startDrag(e, i);
      });
    });
  }

  /** Slot the pointer is currently over, as an insertion index. */
  function insertionIndex(x) {
    const kids = [...imageTrack.children];
    for (let k = 0; k < kids.length; k++) {
      const rect = kids[k].getBoundingClientRect();
      if (x < rect.left + rect.width / 2) return k;
    }
    return kids.length;
  }

  function clearInsertHighlight() {
    [...imageTrack.children].forEach((b) => b.classList.remove("insert-before"));
  }

  function startDrag(e, index) {
    const block = imageTrack.children[index];
    if (!block) return;

    dragActive = true;
    pinnedClip = index;
    const startX = e.clientX;
    const startY = e.clientY;
    let moved = false;
    let target = index;

    const onMove = (ev) => {
      if (!moved) {
        if (Math.abs(ev.clientX - startX) < 8
            && Math.abs(ev.clientY - startY) < 8) return;
        moved = true;
        block.classList.add("dragging");
      }
      ev.preventDefault();
      edgeScroll(ev.clientX);
      target = insertionIndex(ev.clientX);
      clearInsertHighlight();
      const kids = [...imageTrack.children];
      if (target < kids.length) kids[target].classList.add("insert-before");
    };

    const onUp = () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("pointercancel", onUp);
      dragActive = false;
      pinnedClip = -1;
      block.classList.remove("dragging");
      clearInsertHighlight();

      if (moved) {
        draggedRecently = Date.now();
        moveClip(index, target);
      } else {
        selectedIndex = index;
        render();
        showMsg(`Image ${state.clips[index].image + 1} selected - `
          + `${state.clips[index].duration.toFixed(1)}s. Drag it to reorder, `
          + `or drag its right edge to retime.`);
      }
    };

    document.addEventListener("pointermove", onMove, { passive: false });
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onUp);
    e.preventDefault();
  }

  function startResize(e, index) {
    dragActive = true;
    pinnedClip = index;
    const startX = e.clientX;
    const startDuration = state.clips[index].duration;

    const onMove = (ev) => {
      ev.preventDefault();
      edgeScroll(ev.clientX);
      // Fixed scale: 1px of movement is 1 / PX_PER_SECOND of a second.
      const delta = (ev.clientX - startX) / PX_PER_SECOND;
      const next = clamp(startDuration + delta, MIN_CLIP_SECONDS, MAX_CLIP_SECONDS);
      // Snapped to 0.1s so the label reads cleanly.
      state.clips[index].duration = Math.round(next * 10) / 10;
      render();
      showMsg(`Image ${state.clips[index].image + 1}: `
        + `${state.clips[index].duration.toFixed(1)}s`);
    };

    const onUp = () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("pointercancel", onUp);
      dragActive = false;
      pinnedClip = -1;
      draggedRecently = Date.now();
      selectedIndex = index;
      render();
      persist();
    };

    document.addEventListener("pointermove", onMove, { passive: false });
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onUp);
    e.preventDefault();
  }


  /** Move a block to a new position; positions are insertion slots. */
  function moveClip(from, to) {
    const n = state.clips.length;
    let insert = to > from ? to - 1 : to;
    insert = clamp(insert, 0, n - 1);
    if (from === insert) {
      render();
      return;
    }
    const [clip] = state.clips.splice(from, 1);
    state.clips.splice(insert, 0, clip);
    selectedIndex = insert;
    render();
    persist();
    showMsg(`Moved to position ${insert + 1} of ${n}.`);
  }

  // ─────────────────────────────────────────────────────────────
  // Persisting the timeline
  // ─────────────────────────────────────────────────────────────
  /** Fit the clip layout exactly onto the voiceover (when known) so the
   *  preview timeline, the saved segments and the rendered video all
   *  cover the same span: a short timeline extends on its LAST clip (the
   *  final image holds the tail); a long one is squeezed from the tail
   *  so no image is ever pushed past the audio's end. Returns true when
   *  a duration changed. */
  function fitClipsToAudio() {
    const A = state.audioDuration;
    const m = state.clips.length;
    if (!(A > 0) || !m) return false;
    const MIN = MIN_CLIP_SECONDS;
    const r3 = (v) => Math.round(v * 1000) / 1000;

    let total = 0;
    for (const c of state.clips) total += c.duration;
    if (Math.abs(total - A) <= 0.001) return false;

    if (m * MIN > A + 1e-6) {
      // Pathological: not even minimum clips fit - equal split anyway
      // (manifest validation explains the real problem).
      const d = r3(A / m);
      for (const c of state.clips) c.duration = d;
      return true;
    }

    if (total < A) {
      const last = state.clips[m - 1];
      const pad = A - total;
      if (last.duration + pad <= MAX_CLIP_SECONDS) {
        last.duration = r3(last.duration + pad);   // last image holds the tail
      } else {
        // Tail too large for one block: scale everything up (capped).
        const scale = A / total;
        for (const c of state.clips) {
          c.duration = r3(Math.min(MAX_CLIP_SECONDS, c.duration * scale));
        }
      }
      return true;
    }

    // total > A: cumulative clamp - earlier clips keep their length, the
    // overflow squeezes out of the tail, every image keeps MIN_CLIP_SECONDS.
    let prev = 0;
    for (let k = 0; k < m; k++) {
      const upper = A - (m - 1 - k) * MIN;
      let end = Math.min(prev + state.clips[k].duration, upper);
      if (end < prev + MIN) end = prev + MIN;
      if (k === m - 1) end = A;
      state.clips[k].duration = r3(end - prev);
      prev = end;
    }
    return true;
  }

  /** Beat markers for the audio track: where each line of the
   *  timestamped script lands. With transcribed segments the PRIMARY
   *  marker sits at the real sentence start (that is what actually
   *  drives the switch); the typed timestamp shows as a faint ghost
   *  tick when it differs and still fits inside the voiceover. */
  function markerBeats() {
    if (!state.prompts) return [];
    let typed = [];
    try {
      const res = parsePromptLines(state.prompts);
      if (res.errors.length || !res.prompts.length) return [];
      typed = res.prompts;
    } catch (e) {
      return [];
    }
    const effSegs = state.segments.length
      && state.segments.length === state.clips.length
      ? state.segments
      : null;
    const limit = state.audioDuration > 0
      ? state.audioDuration
      : totalDuration();
    const out = [];
    typed.forEach((p, i) => {
      const eff = effSegs && effSegs[i]
        ? num(effSegs[i].start, p.start)
        : p.start;
      out.push({ t: eff, text: p.text, ghost: false, typed: p.start });
      if (effSegs && Math.abs(p.start - eff) > 0.25 && p.start <= limit + 0.25) {
        out.push({
          t: Math.min(p.start, limit),
          text: p.text,
          ghost: true,
          typed: p.start,
        });
      }
    });
    return out;
  }

  function renderMarkers() {
    if (!audioTrack) return;
    audioTrack.querySelectorAll(".beat-marker").forEach((el) => el.remove());
    for (const b of markerBeats()) {
      if (!(b.t >= 0)) continue;
      const el = document.createElement("div");
      el.className = "beat-marker" + (b.ghost ? " ghost" : "");
      el.style.left = `${b.t * PX_PER_SECOND}px`;
      const clock = promptClock(b.ghost ? b.typed : b.t);
      el.title = `${b.ghost ? "typed " : ""}${clock} \u2014 ${b.text}`;
      audioTrack.appendChild(el);
    }
  }

  function segmentsWithClips() {
    return state.segments.map((seg, i) => ({
      ...seg,
      image: state.clips[i] ? state.clips[i].image : seg.image,
    }));
  }

  function timelinePayload() {
    const payload = { clips: state.clips, prompts: state.prompts };
    if (state.segments.length) payload.segments = segmentsWithClips();
    return payload;
  }

  /** Save the timeline to the server.
   *  opts.signal  - AbortController signal so the Apply popup's Cancel
   *                 can abort the request mid-flight.
   *  opts.silent  - failure is returned (false) instead of opening the
   *                 #syncMsg error popup; the Apply progress popup shows
   *                 the error itself inside its own card. */
  async function persistNow(opts) {
    const signal = opts && opts.signal;
    const silent = !!(opts && opts.silent);
    clearTimeout(persistTimer);
    // Fit the layout exactly onto the voiceover (when known) before
    // saving, so the segments derived server-side and the preview
    // agree down to the last frame.
    if (fitClipsToAudio()) render();
    try {
      const res = await fetch(`/sync/update/${JOB_ID}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(timelinePayload()),
        signal,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Save failed");
      state.matched = !!data.matched;
      updateBanner();
      return true;
    } catch (err) {
      if (signal && signal.aborted) throw err;  // Cancel: caller rolls back
      if (!silent) showMsg(err.message, true);
      return false;
    }
  }

  function persist() {
    clearTimeout(persistTimer);
    persistTimer = setTimeout(persistNow, 350);
  }

  // ─────────────────────────────────────────────────────────────
  // Preview + playback
  //
  // Two clocks: the audio element when a voiceover exists, otherwise a
  // rAF timer. Everything (preview, playhead, scrubbing) reads from
  // currentTime(), so the page is fully usable with no audio at all.
  // ─────────────────────────────────────────────────────────────
  function currentTime() {
    if (playMode === "audio") return audioEl.currentTime || 0;
    if (playing) return playOffset + (performance.now() - playStart) / 1000;
    return playOffset;
  }

  function seekTo(t) {
    if (!state.clips.length) return;
    const clamped = clamp(t, 0, totalDuration());
    if (playMode === "audio") {
      try {
        audioEl.currentTime = clamped;
      } catch (err) {
        // Metadata not loaded yet - nothing to seek.
      }
    } else {
      playOffset = clamped;
      if (playing) playStart = performance.now();
    }
    updatePreview();
    updatePlayhead();
  }

  function updatePreview() {
    // While dragging or resizing, hold the preview on the clip being
    // edited so shifting a boundary can't flip the big preview mid-gesture.
    const idx = pinnedClip >= 0 && pinnedClip < state.clips.length
      ? pinnedClip
      : clipAt(currentTime());
    if (idx < 0) return;
    const image = state.clips[idx].image;
    if (imgEl.dataset.idx !== String(image)) {
      imgEl.src = state.images[image] || "";
      imgEl.dataset.idx = String(image);
    }
  }

  function updatePlayhead() {
    const ph = document.getElementById("syncPlayhead");
    if (!ph) return;
    const px = currentTime() * PX_PER_SECOND;
    ph.style.left = `${px}px`;
    // Follow the playhead while playing, so a long timeline doesn't run
    // off-screen.
    if (playing) keepPxVisible(px);
  }

  function syncPlayBtnState() {
    if (playBtn) playBtn.classList.toggle("active", playing);
  }

  function stopTimer() {
    if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
  }

  function tickTimer() {
    stopTimer();
    rafId = requestAnimationFrame(() => {
      if (!playing) return;
      if (playMode === "timer" && currentTime() >= totalDuration()) {
        playing = false;
        playOffset = totalDuration();
        syncPlayBtnState();
        updatePreview();
        updatePlayhead();
        return;
      }
      // Runs in BOTH playback modes: the audio element's timeupdate
      // event only fires ~4x per second, which made image switches lag
      // up to ~250ms behind their boundary; rAF keeps them tight.
      updatePreview();
      updatePlayhead();
      tickTimer();
    });
  }

  function startPlay() {
    if (!state.images.length) {
      showMsg("Add images first - there is nothing to preview.", true);
      return;
    }
    if (!state.clips.length) {
      showMsg("Upload some images first - there is nothing to preview.", true);
      return;
    }
    if (playMode === "audio") {
      if (currentTime() >= totalDuration() - 0.05) seekTo(0);
      const attempt = audioEl.play();
      if (attempt && typeof attempt.catch === "function") {
        attempt.catch((err) => showMsg(
          `Couldn't play the voiceover: ${err.message}`, true));
      }
    } else {
      if (playOffset >= totalDuration() - 0.05) playOffset = 0;
      playStart = performance.now();
    }
    playing = true;
    tickTimer();   // one rAF loop drives BOTH clocks (see tickTimer)
    syncPlayBtnState();
  }

  function pausePlay() {
    if (playMode === "audio") audioEl.pause();
    else playOffset = currentTime();
    stopTimer();
    playing = false;
    syncPlayBtnState();
  }

  function togglePlay() {
    if (playing) pausePlay();
    else startPlay();
  }

  /** Scrub: map a pointer position on a lane/ruler to a timeline time. */
  function seekFromEvent(e) {
    if (!state.clips.length) return;
    const el = e.currentTarget;
    const rect = el.getBoundingClientRect();
    // Lanes are laid out at a fixed px-per-second scale, so the offset from
    // the lane's left edge converts straight to seconds (rect.left already
    // accounts for any scroll offset).
    seekTo(clamp((e.clientX - rect.left) / PX_PER_SECOND, 0, totalDuration()));
  }

  if (playBtn) playBtn.addEventListener("click", togglePlay);

  [imageTrack, audioTrack, ruler].forEach((el) => {
    el?.addEventListener("click", (e) => {
      // A drag or resize that just finished keeps its own gesture.
      if (Date.now() - draggedRecently < 350) return;
      if (e.target.closest(".blockHandle")) return;
      // Tapping a block selects it; tapping the empty part of a track or
      // the ruler scrubs the timeline.
      if (e.target.closest(".sync-img-block")) return;
      if (audioBlock && e.target.closest("#syncAudioBlock")
          && !state.hasAudio) return;
      seekFromEvent(e);
    });
  });

  audioEl.addEventListener("loadedmetadata", () => {
    applyAudioWidth();
    if (state.hasAudio) paintVoiceoverLoaded();
  });

  audioEl.addEventListener("timeupdate", () => {
    if (playMode !== "audio") return;
    updatePreview();
    updatePlayhead();
  });

  audioEl.addEventListener("ended", () => {
    playing = false;
    syncPlayBtnState();
  });

  // ─────────────────────────────────────────────────────────────
  // Voiceover
  // ─────────────────────────────────────────────────────────────
  addVoiceBtn?.addEventListener("click", () => audioInput?.click());

  addImagesBtn?.addEventListener("click", () => imageInput?.click());

  imageInput?.addEventListener("change", async () => {
    const files = Array.from(imageInput.files || []).filter(
      (f) => f.type.startsWith("image/")
    );
    imageInput.value = "";
    if (files.length) {
      // Downscale in the browser first: 1920px is the render target, so
      // full-res phone photos are pure upload weight (5-20x smaller).
      showMsg(`Preparing ${files.length} image${files.length > 1 ? "s" : ""}…`);
      const prepared = await window.shrinkImagesForUpload(files);
      uploadImages(prepared);
    }
  });

  // The server syncs new images to the voiceover when one exists
  // (/sync/images returns its segments): adopt that timing instead of
  // the placeholder durations, then persist the synced timeline.
  function adoptServerSync(data) {
    const synced = data && Array.isArray(data.segments) ? data.segments : [];
    if (synced.length) {
      state.segments = synced;
      state.matched = !!data.matched;
      state.clips = buildClips(state.images, state.segments, state.clips);
    }
    state.sig = state.images.length + ":" + state.segments.length + ":"
      + (state.hasAudio ? "ready" : "awaiting_audio");
  }

  async function uploadImages(files) {
    showMsg(`Uploading ${files.length} image${files.length > 1 ? "s" : ""}…`);
    try {
      const fd = new FormData();
      files.forEach((f) => fd.append("images", f));
      const res = await fetch(`/sync/images/${JOB_ID}`, { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Image upload failed");
      const urls = Array.isArray(data.image_urls) ? data.image_urls : [];
      const base = state.images.length;
      urls.forEach((url, k) => {
        state.images.push(url);
        state.clips.push({ image: base + k, duration: DEFAULT_CLIP_SECONDS });
      });
      adoptServerSync(data);
      reapplySavedPrompts();  // new images line up to the saved script
      if (typeof data.image_warning === "string") {
        state.imageWarning = data.image_warning;
      }
      render();
      updatePreview();
      await persistNow();
      if (state.matched) {
        showMsg("Added images - synced to the voiceover. Drag to fine-tune, then Export.");
      } else {
        showMsg("Added images - drag to arrange, then Export.");
      }
    } catch (err) {
      showMsg(err.message, true);
    }
  }

  // The chip on the audio track is a second entry point for the picker
  // (the track's own click handler ignores it while there is no audio).
  audioBlock?.addEventListener("click", () => {
    if (!state.hasAudio) audioInput?.click();
  });

  audioInput?.addEventListener("change", () => {
    const file = (audioInput.files || [])[0];
    if (file) uploadVoiceover(file);
    audioInput.value = "";
  });

  async function uploadVoiceover(file) {
    paintVoiceoverChip(false);
    if (audioBlock) {
      audioBlock.innerHTML =
        `<span class="chipTitle">Uploading ${escapeHtml(file.name)}…</span>`;
    }
    try {
      const fd = new FormData();
      fd.append("audio", file);
      const res = await fetch(`/sync/audio/${JOB_ID}`, { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Audio upload failed");
      showMsg("Voiceover uploaded - transcribing…");
      boot();
    } catch (err) {
      showMsg(err.message, true);
      paintVoiceoverChip(false);
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Visual beat plan (advisory): voiceover -> suggested image count
  // + ordered beats. The creator can ignore it entirely and make
  // their own images in any tool; the sync engine is untouched.
  // ─────────────────────────────────────────────────────────────
  // Shared cache: the side panel and the copy popup both read the ONE
  // /sync/beats/<job_id> response - no new generation logic, the server
  // already plans (and caches) the beats per job.
  let beatsPlanCache = null;    // {count, beats} once loaded
  let beatsPlanPending = null;  // in-flight fetch, dedupes races

  /** m:ss stamp shared by the panel and the copyable prompt. Voiceovers
   *  past 99:59 fall back to h:mm:ss, the other shape PROMPT_LINE_RE
   *  accepts, so a copy always pastes back into Add prompt cleanly. */
  const beatStamp = (t) => {
    const s = Math.max(0, Math.round(Number(t) || 0));
    const m = Math.floor(s / 60);
    const sec = String(s % 60).padStart(2, "0");
    return m >= 100
      ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${sec}`
      : `${m}:${sec}`;
  };

  /** Fetch (once) the job's beat plan. Resolves {count, beats};
   *  rejects with the server's own message so the popup can show a
   *  friendly error. Failures are never cached - the next tap retries. */
  function fetchBeats() {
    if (beatsPlanCache) return Promise.resolve(beatsPlanCache);
    if (beatsPlanPending) return beatsPlanPending;
    const p = (async () => {
      const res = await fetch(`/sync/beats/${JOB_ID}`);
      let data = {};
      try { data = await res.json(); } catch (e) { /* non-JSON error body */ }
      if (!res.ok) {
        throw new Error(data.error || `Beats unavailable (${res.status}).`);
      }
      const beats = Array.isArray(data.beats) ? data.beats : [];
      const count = Number(data.image_count) || 0;
      if (!count || !beats.length) {
        throw new Error("No beats for this voiceover yet.");
      }
      beatsPlanCache = { count, beats };
      return beatsPlanCache;
    })();
    beatsPlanPending = p;
    const clear = () => { if (beatsPlanPending === p) beatsPlanPending = null; };
    p.then(clear, clear);
    return p;
  }

  /** The whole plan as ONE prompt: "m:ss - description" per line -
   *  exactly the format PROMPT_LINE_RE/parsePromptLines() accept, so
   *  the copy pastes straight into Add prompt or any image tool.
   *  Stamps are forced strictly upward (the parser rejects equal
   *  times) and descriptions collapse to a single line. */
  function formatBeatPrompt(beats) {
    let prev = -1;
    return beats.map((b) => {
      let t = Math.max(0, Math.round(Number(b.start) || 0));
      if (t <= prev) t = prev + 1;
      prev = t;
      const desc = String(b.description || "")
        .replace(/\s+/g, " ").trim() || "Beat";
      return `${beatStamp(t)} - ${desc}`;
    }).join("\n");
  }

  // ─────────────────────────────────────────────────────────────
  // Export: render, then auto-download - one tap, top right.
  //
  // The always-visible Export button runs the whole job: persist the
  // timeline, render it (a synced video when segments exist, a silent
  // slideshow otherwise) and push the finished MP4 to the browser.
  // ─────────────────────────────────────────────────────────────
  let exportBusy = false;  // one render at a time; taps while busy are ignored

  downloadEl?.addEventListener("click", async (ev) => {
    // It is an <a>, so every path must preventDefault: with no href yet
    // a tap would otherwise navigate the page away.
    ev.preventDefault();
    if (exportBusy) return;
    if (!state.images.length) {
      showMsg("Add images first - the voiceover alone has nothing to show.", true);
      return;
    }
    if (!state.clips.length) {
      showMsg("Upload some images first.", true);
      return;
    }
    // Same guard the old Build button had: while Whisper is still
    // transcribing, the timeline has no synced times and rendering
    // now would only produce a silent slideshow.
    if (state.status === "transcribing" || state.status === "processing") {
      showMsg("Wait for the voiceover to finish transcribing.", true);
      return;
    }
    exportBusy = true;
    downloadEl.classList.add("busy");
    try {
      await persistNow();
      const res = await fetch(`/sync/render/${JOB_ID}`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || `Render failed to start (${res.status})`);
      }
      await pollRender(data.job_id);
    } catch (err) {
      showMsg(err.message, true);
    } finally {
      exportBusy = false;
      downloadEl.classList.remove("busy");
    }
  });

  async function pollRender(exportJobId) {
    // Rendering state lives ON the canvas: the preview blurs and a sharp
    // "Rendering your video" label sits over it.
    previewBox?.classList.add("rendering");
    if (renderOverlay) renderOverlay.style.display = "flex";
    if (renderText) renderText.textContent = "Rendering your video… 0%";
    const deadline = Date.now() + 20 * 60 * 1000; // 20 min cap
    try {
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 800));
        const res = await fetch(`/export/status/${exportJobId}`);
        if (!res.ok) throw new Error(`Status check failed (${res.status})`);
        const job = await res.json();

        const p = Math.round(Number(job.progress) || 0);
        if (renderText) renderText.textContent = `Rendering your video… ${p}%`;

        if (job.status === "done") {
          if (downloadEl && job.output_file) {
            // Keep the corner button armed (href refreshed), then hand
            // the file straight to the browser: /download replies with
            // Content-Disposition: attachment, so this navigation saves
            // the MP4 and never leaves the page.
            downloadEl.href = `/download/${job.output_file}`;
            window.location.assign(`/download/${job.output_file}`);
          }
          return;
        }
        if (job.status === "error") throw new Error(job.error || "Render failed.");
      }
      throw new Error("Render timed out. Try again.");
    } finally {
      previewBox?.classList.remove("rendering");
      if (renderOverlay) renderOverlay.style.display = "none";
    }
  }

  // ─────────────────────────────────────────────────────────────
  // Add prompt: paste a script with timestamps, one line per visual
  // beat. Apply maps each timestamp onto the image timeline AND onto
  // the speech segments when a voiceover exists - the synced build
  // reads segments, not clips, so clips alone would never change it.
  // ─────────────────────────────────────────────────────────────
  // Robust pattern supporting:
  // - List item prefixes (1., 1), -, *, •, >, #)
  // - Speaker labels (Speaker 1:, [Narrator]:, Host:, John Doe:)
  // - Markdown wrapper formatting around timestamps (**00:00**, `00:00`, _00:00_)
  // - Bracketed / parenthesized stamps ([00:15], (0:45), [01:23:45])
  // - Milliseconds / subsecond decimals (00:15.5, 00:15,500)
  // - Time ranges (00:10 - 00:20, [00:10 -> 00:20], 00:10 to 00:20)
  // - Varied separators (-, –, —, :, |, /, whitespace)
  // - Trailing descriptions (or empty text)
  const PROMPT_LINE_RE =
    /^(?:[\s*\-_#=>\u2022\u25E6\u25AA\u25B8]|(?:\d+|[a-zA-Z])[.)\]])*\s*(?:(?:\[([A-Za-z0-9 _\-]+)\]|([A-Za-z]{2,}[A-Za-z0-9 _\-]*?))\s*:|\[([A-Za-z0-9 _\-]+)\]\s*(?=[(\[]|\d)|([A-Za-z]{2,}[A-Za-z0-9 _\-]*?)\s*(?=[(\[]))?\s*[*_\x60~]*(?:\[|\()?\s*(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2}(?:[.,]\d+)?)(?:\s*(?:[-–—~→:]+|->|-->|\bto\b)\s*(?:\d{1,3}:)?\d{1,2}:\d{1,2}(?:[.,]\d+)?)*(?:\s*(?:\]|\)))?[*_\x60~]*(?:(?:[\s\-–—|/.:]+|\s+)(\S.*)|$)/;

  const promptClock = (t) =>
    `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

  /** Strips balanced wrapping quotes and markdown markers from prompt text. */
  function cleanPromptText(text) {
    let s = String(text || "").trim();
    let changed = true;
    while (changed && s.length >= 2) {
      changed = false;
      const first = s[0];
      const last = s[s.length - 1];
      if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
        s = s.slice(1, -1).trim();
        changed = true;
        continue;
      }
      if (s.startsWith("***") && s.endsWith("***") && s.length >= 6) {
        s = s.slice(3, -3).trim();
        changed = true;
        continue;
      }
      if (s.startsWith("___") && s.endsWith("___") && s.length >= 6) {
        s = s.slice(3, -3).trim();
        changed = true;
        continue;
      }
      if (s.startsWith("**") && s.endsWith("**") && s.length >= 4) {
        s = s.slice(2, -2).trim();
        changed = true;
        continue;
      }
      if (s.startsWith("__") && s.endsWith("__") && s.length >= 4) {
        s = s.slice(2, -2).trim();
        changed = true;
        continue;
      }
      if ((first === "*" && last === "*") || (first === "_" && last === "_") || (first === "`" && last === "`")) {
        s = s.slice(1, -1).trim();
        changed = true;
        continue;
      }
    }
    return s;
  }

  /** Parse pasted lines -> {prompts, errors}. Blank lines are skipped,
   *  timestamps must strictly increase, and every problem is reported
   *  with its line number so the whole script can be fixed in one pass. */
  function parsePromptLines(raw) {
    const prompts = [];
    const errors = [];
    String(raw || "").split(/\r?\n/).forEach((rawLine, i) => {
      const line = rawLine.trim();
      if (!line) return;
      // Strip outer markdown emphasis (e.g. whole line wrapped in **...**) for matching fallback
      const plain = line.replace(/[*_`~]/g, "").trim();
      const m = PROMPT_LINE_RE.exec(line) || PROMPT_LINE_RE.exec(plain);
      if (!m) {
        errors.push(`Line ${i + 1}: expected "0:00 - description" `
          + `(got "${line.slice(0, 30)}").`);
        return;
      }
      const speaker = m[1] || m[2] || m[3] || m[4] || "";
      const hours = m[5] ? parseInt(m[5], 10) : 0;
      const minutes = parseInt(m[6], 10);
      const seconds = parseFloat(String(m[7]).replace(",", "."));
      const start = hours * 3600 + minutes * 60 + seconds;
      const prev = prompts.length ? prompts[prompts.length - 1].start : -Infinity;
      if (start <= prev) {
        errors.push(`Line ${i + 1}: timestamps must go up - each line needs `
          + `a later time than the one above.`);
        return;
      }
      let text = (m[8] || "").trim();
      text = text.replace(/^[-–—|/:\s]+/, "").trim();
      text = cleanPromptText(text);
      if (speaker) {
        const speakerPrefix = speaker.trim() + ": ";
        if (!text.startsWith(speakerPrefix)) {
          text = speakerPrefix + text;
        }
      }
      prompts.push({ start, text });
    });
    return { prompts, errors };
  }

  /** Reasons the script cannot drive THIS timeline ("" = all good).
   *  Beat count never matters: zero images and more beats than images
   *  are both fine (see applyPromptTiming), and timestamps that run
   *  past a loaded voiceover are clamped to fit rather than rejected.
   *  The only hazard left is structural: a segment/image count mismatch
   *  that means the manifest itself is broken. */
  function promptBlocker(prompts) {
    if (!prompts.length) return "";
    const m = state.clips.length;
    if (state.segments.length && state.segments.length !== m) {
      return "This job's timeline is out of sync - reload the page and try again.";
    }
    return "";
  }

  /** Re-parse the textarea, show every problem inline, toggle Apply. */
  function refreshPromptValidation() {
    const raw = promptText ? promptText.value : "";
    const { prompts, errors } = parsePromptLines(raw);
    const problems = errors.slice();
    if (!problems.length) {
      const blocker = promptBlocker(prompts);
      if (blocker) problems.push(blocker);
    }
    if (promptError) {
      promptError.textContent = problems.join("\n");
      promptError.style.display = problems.length ? "block" : "none";
    }
    const ok = !!raw.trim() && !problems.length;
    if (promptApply) promptApply.disabled = !ok;
    return { prompts, ok };
  }

  promptBtn?.addEventListener("click", () => {
    if (promptModal) promptModal.style.display = "flex";
    if (promptText && !promptText.value.trim() && state.prompts) {
      promptText.value = state.prompts;  // restore the last applied script
    }
    refreshPromptValidation();
    promptText?.focus();
  });

  /** Map timestamps onto the clip timeline when the job has NO usable
   *  speech timing yet (slideshow, or a voiceover still transcribing /
   *  failed). With transcribed segments present the script is only kept
   *  as beat markers: the timeline follows the real sentence starts -
   *  which is what makes each image flip exactly when its sentence
   *  begins - so typed estimates never fight the recording.
   *
   *  Prompt and image counts are independent:
   *    no images   -> save only (the script lines later images up);
   *    more beats  -> the extra beats wait for images to arrive;
   *    fewer beats -> scripted images follow the script, the rest keep
   *                    their current durations behind it.
   *  Timestamps that overrun a loaded voiceover are clamped to fit
   *  (every image keeps MIN_CLIP_SECONDS) instead of rejected.
   *  Returns the success message to show after a safe persist. */
  function applyPromptTiming(prompts) {
    const n = prompts.length;
    const clips = state.clips;
    const m = clips.length;
    if (!m) {
      return `${n} prompt${n === 1 ? "" : "s"} saved - add ${n} images and `
        + "they'll line up to these timestamps.";
    }

    if (state.segments.length) {
      const applied = Math.min(n, m);
      const pending = n - applied;
      let message = `${applied} prompt${applied === 1 ? "" : "s"} saved as `
        + "beat markers - timing follows your voiceover's sentences";
      if (pending > 0) {
        message += `, ${pending} beat${pending === 1 ? "" : "s"} waiting `
          + "for images";
      }
      // One image per beat: n (the beat count) is exactly how many
      // images this script needs, so the creator knows what to make.
      return message + ` - use ${n} image${n === 1 ? "" : "s"} - `
        + "the markers are drawn on the audio track.";
    }

    const oldDur = clips.map((c) => c.duration);
    const timelineEnd = totalDuration();  // before any retime below
    const audioDur = state.audioDuration > 0 ? state.audioDuration : 0;

    // pts[k] = when image k starts (pts[0] = 0: any lead-in before the
    // first timestamp belongs to image 0); pts[m] = when the last image
    // ends. Scripted starts win; unscripted images keep their current
    // durations and follow on. With a loaded voiceover every boundary
    // is clamped so each image keeps MIN_CLIP_SECONDS of screen time
    // inside the audio - timestamps past the end squeeze the tail
    // instead of producing segments the server would reject.
    const MIN = MIN_CLIP_SECONDS;
    if (audioDur > 0 && m * MIN > audioDur + 1e-6) {
      // Pathological: not even an equal split fits - equal split anyway
      // (manifest validation explains the real problem to the user).
      const d = Math.round((audioDur / m) * 1000) / 1000;
      clips.forEach((c) => { c.duration = d; });
      return `${n} prompt${n === 1 ? "" : "s"} applied - the voiceover is `
        + "too short for this many images.";
    }

    const pts = [0];
    let overrun = false;
    for (let k = 1; k < m; k++) {
      let raw = k < n ? prompts[k].start : pts[k - 1] + oldDur[k - 1];
      if (audioDur > 0) {
        const upper = audioDur - (m - k) * MIN;
        if (k < n && raw > upper + 1e-9) overrun = true;
        raw = Math.min(raw, upper);
        raw = Math.max(raw, pts[k - 1] + MIN);
      } else if (raw < pts[k - 1] + MIN) {
        raw = pts[k - 1] + MIN;
      }
      pts.push(Math.round(raw * 1000) / 1000);
    }
    let end;
    if (audioDur > 0) {
      end = audioDur;                     // voiceover: run to audio end
    } else if (m < n) {
      end = prompts[m].start;             // next scripted beat
    } else if (m > n) {
      end = pts[m - 1] + oldDur[m - 1];   // unscripted tail keeps its length
    } else {
      end = Math.max(timelineEnd, prompts[n - 1].start + oldDur[m - 1]);
    }
    pts.push(end);

    // Clip durations mirror the server's own clamp (0.5..60s, 3 decimals)
    // so what is shown is exactly what a later save would keep.
    const clamp3 = (v) => clamp(Math.round(v * 1000) / 1000,
      MIN_CLIP_SECONDS, MAX_CLIP_SECONDS);
    for (let k = 0; k < m; k++) {
      clips[k].duration = clamp3(pts[k + 1] - pts[k]);
    }

    const applied = Math.min(n, m);
    const pending = n - applied;
    let message = `${applied} prompt${applied === 1 ? "" : "s"} applied to `
      + "the timeline";
    if (pending > 0) {
      message += `, ${pending} beat${pending === 1 ? "" : "s"} waiting for images`;
    }
    if (overrun) {
      message += ` (timestamps past ${promptClock(audioDur)} were clamped `
        + "to fit the voiceover)";
    }
    return message + " - drag blocks to fine-tune, then Export.";
  }

  /** Re-align the timeline to the saved timestamped script (after new
   *  images arrive or a rebuild discarded it). Skipped silently when
   *  the script no longer parses or the timeline is structurally out
   *  of sync - the Add prompt modal explains why when opened. */
  function reapplySavedPrompts() {
    if (!state.prompts || !state.clips.length) return;
    const { prompts, errors } = parsePromptLines(state.prompts);
    if (errors.length || !prompts.length) return;
    if (promptBlocker(prompts)) return;
    applyPromptTiming(prompts);
  }

  promptCancel?.addEventListener("click", () => {
    if (promptModal) promptModal.style.display = "none";
  });

  promptText?.addEventListener("input", refreshPromptValidation);

  // ─────────────────────────────────────────────────────────────
  // Beats-from-audio popup (#syncGenBeats): the same advisory plan
  // as the side panel, handed over as ONE copyable
  // "m:ss - description" prompt - the format the Add prompt modal
  // parses, so a copy pastes straight back into the timeline.
  //
  // Flow while Whisper is still working: tap -> transcribing status
  // popup (live elapsed time) -> it closes itself when the
  // transcription completes -> this popup opens with the plan ready
  // to copy. Cancel inside the status popup just stops waiting; the
  // transcription itself keeps running server-side.

  // True while the status popup is waiting out the transcription, so
  // another tap can't start a second poller next to the first one.
  let beatsWaiting = false;

  function openBeatsProgress() {
    if (beatsProgress) beatsProgress.style.display = "flex";
  }

  function closeBeatsProgress() {
    if (beatsProgress) beatsProgress.style.display = "none";
  }

  /** Open the script popup ready for a fresh load: placeholder +
   *  disabled Copy, previous error cleared, Copy label reset. */
  function openBeatsModalFresh() {
    if (beatsModal) beatsModal.style.display = "flex";
    if (beatsCopyLabel) beatsCopyLabel.textContent = "Copy";
    clearTimeout(copyLabelTimer);
    if (beatsError) beatsError.style.display = "none";
    if (beatsPrompt) {
      beatsPrompt.value = "";
      beatsPrompt.placeholder = "Generating beats…";
    }
    if (beatsCopyBtn) beatsCopyBtn.disabled = true;
  }

  /** Open the script popup and load the plan into it (the placeholder
   *  shows while the fetch runs; errors land in #beatsError, where
   *  beats errors have always been shown). */
  async function loadBeatsIntoModal(setErr) {
    openBeatsModalFresh();
    try {
      const plan = await fetchBeats();   // placeholder shows meanwhile
      const text = formatBeatPrompt(plan.beats);
      if (beatsPrompt) {
        beatsPrompt.value = text;
        beatsPrompt.placeholder = "";
        beatsPrompt.focus();
        beatsPrompt.select();            // ready for hand-copy too
      }
      if (beatsCopyBtn) beatsCopyBtn.disabled = false;
      if (beatsError) beatsError.style.display = "none";
    } catch (err) {
      setErr(err && err.message
        ? err.message
        : "Couldn't load beats yet - try again in a moment.");
    }
  }

  /**
   * Poll /sync/status until the transcription leaves
   * transcribing/processing - same cadence (1.5s) and cap (3 min) as
   * boot(). Each poll feeds applyServerData + render, so the timeline
   * underneath updates the moment the segments arrive, exactly as a
   * page load would. Resolves {outcome, message}:
   *   ready     - transcription finished; the beats can be fetched
   *   error     - server reported a transcription failure
   *   network   - the status fetch itself failed
   *   timeout   - the cap ran out
   *   cancelled - Cancel was tapped (transcription keeps running)
   */
  async function waitForTranscription() {
    const deadline = Date.now() + 3 * 60 * 1000;   // same cap as boot()
    const started = Date.now();
    while (Date.now() < deadline) {
      if (!beatsWaiting) return { outcome: "cancelled", message: "" };
      let data;
      try {
        const res = await fetch(`/sync/status/${JOB_ID}`);
        data = await res.json();
        if (!res.ok) throw new Error(data.error || `Status ${res.status}`);
      } catch (err) {
        return beatsWaiting
          ? { outcome: "network",
              message: "Lost connection while transcribing - check your "
                + "connection and tap Generate beats again." }
          : { outcome: "cancelled", message: "" };
      }

      applyServerData(data);
      if (!dragActive) render();

      const st = typeof data.status === "string" ? data.status : "";
      if (st === "error") {
        return {
          outcome: "error",
          message: `Transcription failed: ${data.error || "unknown error"} - `
            + "you can still reorder, retime and build a silent slideshow.",
        };
      }
      if (st !== "transcribing" && st !== "processing") {
        return { outcome: "ready", message: "" };
      }

      const secs = Math.round((Date.now() - started) / 1000);
      if (beatsProgressText) {
        beatsProgressText.textContent = secs > 0
          ? `Transcribing voiceover… (${secs}s)`
          : "Transcribing voiceover…";
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
    return {
      outcome: "timeout",
      message: "Timed out waiting for the voiceover - tap Generate beats "
        + "again in a moment.",
    };
  }

  genBeatsBtn?.addEventListener("click", async () => {
    const setErr = (msg) => {
      if (beatsError) {
        beatsError.textContent = msg;
        beatsError.style.display = "block";
      }
      if (beatsPrompt) {
        beatsPrompt.value = "";
        beatsPrompt.placeholder = "";
      }
      if (beatsCopyBtn) beatsCopyBtn.disabled = true;
    };

    // Friendly up-front error for the one "not yet" case that has
    // nothing to wait for.
    if (!state.hasAudio) {
      openBeatsModalFresh();
      setErr("Add a voiceover first - beats are planned from your narration.");
      return;
    }

    // Whisper still running: show the transcribing status popup, wait
    // it out, then hand over to the script popup.
    if (state.status === "transcribing" || state.status === "processing") {
      if (!beatsProgress) {
        // Status popup missing from the page (template out of sync):
        // fall back to the old inline error instead of a silent wait.
        openBeatsModalFresh();
        setErr("Your voiceover is still transcribing - try again in a moment.");
        return;
      }
      if (beatsWaiting) return;   // a wait is already on screen
      beatsWaiting = true;
      openBeatsProgress();
      if (beatsProgressText) {
        beatsProgressText.textContent = "Transcribing voiceover…";
      }
      const outcome = await waitForTranscription();
      const cancelled = !beatsWaiting;
      beatsWaiting = false;
      closeBeatsProgress();       // status popup goes away on completion
      if (cancelled || outcome.outcome === "cancelled") return;
      if (outcome.outcome !== "ready") {
        openBeatsModalFresh();
        setErr(outcome.message);  // surfaced where beats errors live
        return;
      }
    }

    await loadBeatsIntoModal(setErr);
  });

  // Cancel inside the status popup: stop waiting and close. The
  // transcription keeps running server-side - tapping Generate beats
  // again simply re-attaches to it.
  beatsProgressCancel?.addEventListener("click", () => {
    beatsWaiting = false;
    closeBeatsProgress();
  });

  // Clipboard icon: modern API first, select()+execCommand fallback
  // for insecure contexts; the label flips to "Copied" for 2.2s.
  beatsCopyBtn?.addEventListener("click", async () => {
    const text = beatsPrompt ? beatsPrompt.value : "";
    if (!text) return;
    let copied = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        copied = true;
      }
    } catch (err) {
      copied = false;
    }
    if (!copied && beatsPrompt) {
      try {
        beatsPrompt.focus();
        beatsPrompt.select();
        copied = document.execCommand("copy");
      } catch (err) {
        copied = false;
      }
    }
    if (copied && beatsCopyLabel) {
      beatsCopyLabel.textContent = "Copied";
      clearTimeout(copyLabelTimer);
      copyLabelTimer = setTimeout(() => {
        if (beatsCopyLabel) beatsCopyLabel.textContent = "Copy";
      }, 2200);
    }
  });

  beatsClose?.addEventListener("click", () => {
    if (beatsModal) beatsModal.style.display = "none";
  });

  // ─────────────────────────────────────────────────────────────
  // Apply progress popup: staged, visible updates while the run
  // happens, with Cancel inside the popup at every stage.
  //
  // applyRun tracks the live run:
  //   phase: "running" | "done" | "error" | "cancelled"
  //   abort: AbortController for the save (null until the save starts)
  //   snapshot: pre-Apply clips/prompts so Cancel can roll back
  // ─────────────────────────────────────────────────────────────
  const applyRun = { phase: "idle", abort: null, snapshot: null };

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  function setProgress(text) {
    if (progressText) progressText.textContent = text;
  }

  function openProgress() {
    if (progressEl) {
      progressEl.classList.remove("prompt-progress-done", "prompt-progress-error");
      progressEl.style.display = "flex";
    }
    if (progressCancel) {
      progressCancel.textContent = "Cancel";
      progressCancel.disabled = false;
    }
  }

  /** Terminal state: spinner stops, Cancel becomes OK/Close. */
  function finishProgress(text, type) {
    applyRun.phase = type === "error" ? "error" : "done";
    setProgress(text);
    if (progressEl) {
      progressEl.classList.add("prompt-progress-done");
      progressEl.classList.toggle("prompt-progress-error", type === "error");
    }
    if (progressCancel) {
      progressCancel.textContent = type === "error" ? "Close" : "OK";
      progressCancel.disabled = false;
    }
  }

  function closeProgress() {
    if (progressEl) progressEl.style.display = "none";
  }

  /** Cancel/OK click. While the run is still moving this fires the
   *  cancel: abort the save (if one is in flight), close the popup
   *  immediately, and let the run handler roll the timeline back to
   *  its pre-Apply snapshot. On a terminal state it just closes. */
  progressCancel?.addEventListener("click", () => {
    if (applyRun.phase === "running") {
      applyRun.phase = "cancelled";
      if (applyRun.abort) applyRun.abort.abort();
      closeProgress();   // pre-mutation stages: nothing to roll back yet
      return;
    }
    closeProgress();
  });

  promptApply?.addEventListener("click", async () => {
    const { prompts, ok } = refreshPromptValidation();
    if (!ok) return;
    if (applyRun.phase === "running") return;   // one run at a time

    /** True after a Cancel that landed during the staged messages
     *  (before anything was mutated): drop the run leftovers. The
     *  popup is already closed by the Cancel handler. */
    const bailed = () => {
      if (applyRun.phase !== "cancelled") return false;
      applyRun.snapshot = null;
      applyRun.abort = null;
      return true;
    };

    // Pre-Apply snapshot so Cancel can restore the exact prior state.
    applyRun.snapshot = {
      clips: state.clips.map((c) => ({ image: c.image, duration: c.duration })),
      prompts: state.prompts,
    };
    applyRun.abort = new AbortController();
    applyRun.phase = "running";

    // Swap the Add prompt modal for the progress popup.
    if (promptModal) promptModal.style.display = "none";
    openProgress();

    // Staged updates - each stage yields so the user SEES the progress
    // and Cancel stays responsive between steps.
    setProgress("Loading…");
    await wait(450);
    if (bailed()) return;

    setProgress("Prompt received");
    await wait(450);
    if (bailed()) return;

    setProgress(`Detected ${prompts.length} timestamp`
      + `${prompts.length === 1 ? "" : "s"}`);
    await wait(450);
    if (bailed()) return;

    const imgCount = state.images.length;
    setProgress(imgCount
      ? `Now loading ${imgCount} image${imgCount === 1 ? "" : "s"}…`
      : "No images yet - saving your timestamps…");
    await wait(450);
    if (bailed()) return;

    // Mutate the timeline and save.
    const message = applyPromptTiming(prompts);
    state.prompts = promptText.value;  // persisted together with the timeline
    selectedIndex = -1;
    render();
    updatePreview();

    let saved;
    try {
      saved = await persistNow({ signal: applyRun.abort.signal, silent: true });
    } catch (err) {
      saved = null;   // aborted by Cancel
    }

    // Cancel during the save: restore the snapshot and re-render so
    // nothing from this run survives, then close (no terminal message).
    if (applyRun.phase === "cancelled") {
      if (applyRun.snapshot) {
        state.clips = applyRun.snapshot.clips;
        state.prompts = applyRun.snapshot.prompts;
        render();
        updatePreview();
        // Best-effort: push the restored timeline back so the server
        // converges too if the aborted request already landed.
        persistNow({ silent: true }).catch(() => {});
      }
      applyRun.snapshot = null;
      applyRun.abort = null;
      closeProgress();
      return;
    }

    if (saved !== true) {
      // Save failed (not cancelled): report INSIDE the popup instead
      // of the bottom hint line; the timeline edits stay applied so a
      // retry from the Add prompt modal still works.
      finishProgress("Couldn't save the timeline - check your connection "
        + "and try Apply again.", "error");
      applyRun.snapshot = null;
      applyRun.abort = null;
      return;
    }

    finishProgress(message, "ok");
    applyRun.snapshot = null;
    applyRun.abort = null;
    window.posthog?.capture("sync_prompt_applied", {
      prompt_count: prompts.length,
      image_count: state.clips.length,
    });
  });

  boot();
})();
