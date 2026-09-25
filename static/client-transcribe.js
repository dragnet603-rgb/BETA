/**
 * client-transcribe.js - optional on-device Whisper transcription.
 *
 * Runs Xenova/whisper-tiny.en in the browser via transformers.js + WebGPU
 * inside a Web Worker, so the page never blocks. Produces segment-level
 * {start, end, text} chunks used as a FAST beats preview.
 *
 * Server faster-whisper remains the authoritative fallback: the server
 * only accepts this transcript when IT opted in (client_transcribe=1 on
 * /sync/audio), validates the audio hash + segment caps, and any failure
 * falls back to the normal server-side job via POST /sync/transcribe.
 *
 * Usage:
 *   const ok = await window.AQClientTranscribe.isSupported();
 *   const res = await window.AQClientTranscribe.transcribe(file, { onProgress });
 *   // res -> { segments: [{start, end, text}], duration, engine: "webgpu-tiny.en" }
 */
(function () {
  "use strict";

  const CDN = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.8.1";
  const MODEL_ID = "Xenova/whisper-tiny.en";
  const MAX_AUDIO_SECONDS = 180;   // desktop guard: longer clips use the server
  const HARD_TIMEOUT_MS = 120000;  // 2 min total budget per attempt

  let worker = null;
  let seq = 0;
  const pending = new Map(); // id -> {resolve, reject, onProgress, timer}

  function workerSource() {
    // Built as a string so no extra static file is required.
    return `
      let _pipe = null;
      async function ensurePipe(post) {
        if (_pipe) return _pipe;
        const mod = await import(${JSON.stringify(CDN)});
        try {
          if (mod.env && mod.env.backends && mod.env.backends.onnx) {
            mod.env.backends.onnx.wasm = mod.env.backends.onnx.wasm || {};
            mod.env.backends.onnx.wasm.numThreads = 1;
          }
        } catch (_) {}
        post({ type: "status", phase: "loading-model" });
        // q8 (quantized) weights download ~4x smaller; retry with full
        // fp32 if the WebGPU backend rejects the quantized graph.
        try {
          _pipe = await mod.pipeline("automatic-speech-recognition", ${JSON.stringify(MODEL_ID)}, {
            device: "webgpu",
            dtype: "q8",
          });
        } catch (e1) {
          _pipe = await mod.pipeline("automatic-speech-recognition", ${JSON.stringify(MODEL_ID)}, {
            device: "webgpu",
            dtype: "fp32",
          });
        }
        return _pipe;
      }
      function toChunks(raw, fallbackDuration) {
        const chunks = raw && Array.isArray(raw.chunks) ? raw.chunks : [];
        const out = [];
        for (const c of chunks) {
          const ts = c && c.timestamp ? c.timestamp : null;
          const text = String((c && c.text) || "").trim();
          if (!text) continue;
          const start = ts && Number.isFinite(ts[0]) ? Number(ts[0]) : null;
          let end = ts && Number.isFinite(ts[1]) ? Number(ts[1]) : null;
          // The final chunk often has an open end (null): close it at the
          // decoded duration instead of dropping the last sentence.
          if (start != null && end == null
              && Number.isFinite(fallbackDuration) && fallbackDuration > start) {
            end = fallbackDuration;
          }
          if (start == null || end == null || end <= start) continue;
          out.push({ start, end, text });
        }
        if (!out.length && raw && typeof raw.text === "string" && raw.text.trim()) {
          const dur = Number.isFinite(fallbackDuration) && fallbackDuration > 0 ? fallbackDuration : 0;
          if (dur > 0) out.push({ start: 0, end: dur, text: raw.text.trim() });
        }
        return out;
      }
      self.onmessage = async (ev) => {
        const msg = ev.data || {};
        if (msg.type !== "transcribe") return;
        const post = (m) => self.postMessage(Object.assign({ id: msg.id }, m));
        try {
          const audio = msg.pcm instanceof Float32Array ? msg.pcm : new Float32Array(msg.pcm);
          const pipe = await ensurePipe(post);
          post({ type: "status", phase: "transcribing" });
          // Segment-level timestamps (whisper's own sentence chunks).
          // No language/task option: tiny.en is English-only, so there
          // is nothing to detect - and passing a "language" option to an
          // .en model errors in some transformers.js versions.
          const raw = await pipe(audio, {
            sampling_rate: msg.sampleRate || 16000,
            return_timestamps: true,
            chunk_length_s: 30,
            stride_length_s: 5,
          });
          post({ type: "done", segments: toChunks(raw, msg.duration) });
        } catch (err) {
          post({ type: "error", message: String((err && err.message) || err || "transcription failed") });
        }
      };
    `;
  }


  function ensureWorker() {
    if (worker) return worker;
    const blob = new Blob([workerSource()], { type: "text/javascript" });
    worker = new Worker(URL.createObjectURL(blob), { type: "module" });
    worker.onmessage = (ev) => {
      const msg = ev.data || {};
      const entry = pending.get(msg.id);
      if (!entry) return;
      if (msg.type === "status") {
        try { entry.onProgress && entry.onProgress(msg.phase); } catch (_) {}
      } else if (msg.type === "done") {
        pending.delete(msg.id);
        clearTimeout(entry.timer);
        entry.resolve({ segments: Array.isArray(msg.segments) ? msg.segments : [] });
      } else if (msg.type === "error") {
        pending.delete(msg.id);
        clearTimeout(entry.timer);
        entry.reject(new Error(msg.message || "on-device transcription failed"));
      }
    };
    worker.onerror = (err) => {
      for (const [, entry] of pending) {
        clearTimeout(entry.timer);
        try { entry.reject(err && err.error ? err.error : new Error("transcriber worker crashed")); } catch (_) {}
      }
      pending.clear();
      try { worker.terminate(); } catch (_) {}
      worker = null;
    };
    return worker;
  }

  function terminateWorker() {
    try { worker && worker.terminate(); } catch (_) {}
    worker = null;
  }

  async function gpuAvailable() {
    try {
      if (!navigator.gpu) return false;
      const adapter = await navigator.gpu.requestAdapter({ powerPreference: "high-performance" });
      return !!adapter;
    } catch (_) {
      return false;
    }
  }

  async function decodeTo16kMono(file) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error("Web Audio unavailable");
    const buf = await file.arrayBuffer();
    const ctx = new Ctx();
    try {
      const audio = await ctx.decodeAudioData(buf.slice(0));
      const duration = audio.duration || 0;
      if (!Number.isFinite(duration) || duration <= 0) throw new Error("undecodable audio");
      if (duration > MAX_AUDIO_SECONDS) {
        const err = new Error("audio too long for on-device pass");
        err.code = "TOO_LONG";
        throw err;
      }
      const targetRate = 16000;
      const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(duration * targetRate)), targetRate);
      const src = offline.createBufferSource();
      src.buffer = audio;
      src.connect(offline.destination);
      src.start(0);
      const rendered = await offline.startRendering();
      const pcm = rendered.getChannelData(0).slice(0);
      return { pcm, sampleRate: targetRate, duration };
    } finally {
      try { ctx.close(); } catch (_) {}
    }
  }

  function validSegments(segments, duration) {
    if (!Array.isArray(segments) || !segments.length) return null;
    const out = [];
    let prevEnd = -Infinity;
    for (const s of segments) {
      let start = Number(s && s.start);
      const end = Number(s && s.end);
      const text = String((s && s.text) || "").trim();
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || start < 0) continue;
      if (!text) continue;
      // Chunked output can overlap a few ms: clamp the start instead of
      // dropping the chunk (the server-side cleaner clamps the same way).
      start = Math.max(start, prevEnd);
      if (end <= start) continue;
      out.push({ start: Math.round(start * 1000) / 1000, end: Math.round(end * 1000) / 1000, text });
      prevEnd = end;
    }
    if (!out.length) return null;
    if (Number.isFinite(duration) && duration > 0) {
      const last = out[out.length - 1];
      if (last.end > duration + 1) return null;
    }
    return out;
  }

  async function isSupported() {
    try {
      if (typeof Worker === "undefined") return false;
      if (!window.AudioContext && !window.webkitAudioContext) return false;
      if (typeof OfflineAudioContext === "undefined") return false;
      return await gpuAvailable();
    } catch (_) {
      return false;
    }
  }

  async function transcribe(file, opts) {
    const options = opts || {};
    const onProgress = options.onProgress;
    if (!file) throw new Error("no audio file");
    if (!(await gpuAvailable())) throw new Error("WebGPU unavailable");
    const decoded = await decodeTo16kMono(file);
    const w = ensureWorker();
    const id = ++seq;
    let segments;
    try {
      segments = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error("on-device transcription timed out"));
        }, options.timeoutMs || HARD_TIMEOUT_MS);
        pending.set(id, { resolve, reject, onProgress, timer });
        // Copy the buffer: keep the caller's data intact for structured clone.
        const copy = decoded.pcm.slice(0);
        w.postMessage(
          { type: "transcribe", id, pcm: copy, sampleRate: decoded.sampleRate, duration: decoded.duration },
          [copy.buffer],
        );
      });
    } catch (err) {
      terminateWorker();
      throw err;
    }
    const clean = validSegments(segments && segments.segments, decoded.duration);
    if (!clean) throw new Error("no usable transcript chunks");
    return { segments: clean, duration: decoded.duration, engine: "webgpu-tiny.en" };
  }

  window.AQClientTranscribe = {
    MODEL_ID,
    MAX_AUDIO_SECONDS,
    isSupported,
    transcribe,
  };
})();
