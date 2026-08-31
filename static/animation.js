/**
 * Autoquence — Action Animation Engine (reusable primitives)
 * ==========================================================
 *
 * A tiny, dependency-free tween engine used by canvas.js to visually
 * interpolate the Konva/DOM canvas from the CURRENT scene state to the
 * DESIRED scene state while an AI edit executes.
 *
 * DESIGN RULES:
 *   - The animation is NEVER the source of truth. canvas.js applies the
 *     final scene state first; this engine only interpolates what the
 *     user sees on the way there.
 *   - Every tween is single-shot (no persistent loops) and is removed
 *     from the active set on completion or cancellation — no leaks.
 *   - cancelAll() instantly kills every active tween (used by undo,
 *     redo, manual dragging and new prompts). Stale completion
 *     callbacks must be idempotent and cheap.
 *   - Durations are short (160–480ms) with subtle ease-out easing.
 */
(function () {
  "use strict";

  const active = new Set();
  let raf = null;

  function easeOutCubic(t) {
    return 1 - Math.pow(1 - t, 3);
  }

  function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function pump() {
    raf = null;
    const now = performance.now();
    for (const tw of [...active]) {
      if (tw._cancelled) { active.delete(tw); continue; }
      const t = Math.min(1, (now - tw._start) / tw._duration);
      const v = tw._easing(t);
      try { tw._onUpdate(v); } catch (e) { console.error("[AQAnim] update error", e); }
      if (t >= 1) {
        active.delete(tw);
        tw._done = true;
        try { if (tw._onComplete) tw._onComplete(); } catch (e) { console.error("[AQAnim] complete error", e); }
      }
    }
    if (active.size) raf = requestAnimationFrame(pump);
  }

  /**
   * tween({ duration, easing, onUpdate(v 0..1), onComplete, onCancel })
   * Returns a handle with .cancel(). onUpdate receives the EASED value.
   */
  function tween(opts) {
    const duration = Math.max(0, Number(opts.duration ?? 300));
    const tw = {
      _start: performance.now(),
      _duration: duration || 1,
      _easing: opts.easing || easeOutCubic,
      _onUpdate: opts.onUpdate || function () {},
      _onComplete: opts.onComplete,
      _onCancel: opts.onCancel,
      _cancelled: false,
      _done: false,
      cancel() {
        if (this._done || this._cancelled) return;
        this._cancelled = true;
        active.delete(this);
        if (raf && !active.size) { cancelAnimationFrame(raf); raf = null; }
        try { if (this._onCancel) this._onCancel(); } catch (e) { console.error("[AQAnim] cancel error", e); }
      },
    };
    if (duration <= 0) {
      // Zero-duration: run synchronously to the end (no rAF churn).
      try { tw._onUpdate(1); if (tw._onComplete) tw._onComplete(); } catch (e) { console.error("[AQAnim] error", e); }
      return tw;
    }
    active.add(tw);
    if (!raf) raf = requestAnimationFrame(pump);
    return tw;
  }

  /** Cancel every active tween. Each tween's onCancel finalizer runs. */
  function cancelAll() {
    for (const tw of [...active]) tw.cancel();
    active.clear();
    if (raf) { cancelAnimationFrame(raf); raf = null; }
  }

  window.AQAnim = {
    tween,
    cancelAll,
    hasActive: () => active.size > 0,
    activeCount: () => active.size,
    easeOut: easeOutCubic,
    easeInOut: easeInOutCubic,
  };
})();