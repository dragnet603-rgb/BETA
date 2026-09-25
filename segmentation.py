"""
segmentation.py — Autoquence core audio/image synchronization engine.

The user uploads N images (already in chronological order) and 1 voiceover.
This module turns the voiceover's Whisper word-level timestamps into
EXACTLY N contiguous visual segments, so image i is shown during segment i.

Guarantees of create_visual_segments():
  1.  Exactly image_count segments.
  2.  The first segment starts at 0.
  3.  The last segment ends at exactly audio_duration.
  4.  Segments are contiguous (no gaps, no overlaps, chronological).
  5.  start < end for every segment.
  6.  Boundaries fall between words (never mid-word) whenever possible.
  7.  Deterministic: same input -> same output.
  8.  Sentence-aware: a boundary prefers a real sentence start near the
      ideal proportional spot, so an image flips exactly when the
      sentence it belongs to begins (no more "sentence ended, image
      waits"). With image_count == sentence count the mapping is an
      exact 1:1 auto-match (image i shows sentence i).

MINIMUM SEGMENT DURATION
    A visual segment shorter than MIN_SEGMENT_SECONDS (0.5 s) is treated
    as broken output: images would flash by faster than a viewer can see.
    create_visual_segments() therefore refuses (ValueError) when the audio
    is too short to give every image at least MIN_SEGMENT_SECONDS, and it
    never picks a word boundary that would starve a neighbouring segment
    below that minimum. Falls back to proportional (equal) segmentation
    when the audio has no usable word timestamps at all (e.g. pure
    silence) — still respecting the minimum duration via validation.
"""

MIN_SEGMENT_SECONDS = 0.5

_EPS = 1e-6


class SegmentValidationError(ValueError):
    """Raised when the image count cannot be served by the audio length."""


def _clean_words(words):
    """Normalize Whisper word timestamps -> sorted list of (start, end).

    Accepts dicts with 'start'/'end' keys (objects with .start/.end also
    work). Drops empty/invalid/non-finite entries; ties broken by start.
    """
    cleaned = []
    for w in words or []:
        if isinstance(w, dict):
            start, end = w.get("start"), w.get("end")
        else:
            start, end = getattr(w, "start", None), getattr(w, "end", None)
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if start != start or end != end:  # NaN
            continue
        if end < start:
            start, end = end, start
        cleaned.append((start, end))
    cleaned.sort()
    return cleaned


def _word_boundaries(words, audio_duration):
    """Chronological candidate cut points that never split a word.

    For each adjacent word pair the valid cut zone is [prev.end,
    next.start]; a cut exactly at prev.end (== next.start when words
    touch) is preferred, and for real gaps the gap midpoint is offered
    too so the split sits inside the silence rather than snapping a word.
    """
    if not words:
        return []
    candidates = []
    for i in range(len(words) - 1):
        prev_end = words[i][1]
        next_start = words[i + 1][0]
        lo = max(prev_end, 0.0)
        hi = min(next_start, audio_duration)
        if hi - lo < _EPS:
            # Words touch (or overlap): the shared point is the boundary.
            point = max(prev_end, next_start)
            if _EPS < point < audio_duration - _EPS:
                candidates.append(point)
        else:
            if _EPS < lo < audio_duration - _EPS:
                candidates.append(lo)
            mid = (lo + hi) / 2.0
            if _EPS < mid < audio_duration - _EPS and mid - lo > _EPS:
                candidates.append(mid)
    return sorted(candidates)

def create_visual_segments(words, image_count, audio_duration,
                           sentence_starts=None):
    """
    Split the voiceover into exactly `image_count` visual segments.

    words          — Whisper word-level timestamps: [{'word': ..., 'start': s,
                     'end': e}, ...]
    image_count    — number of uploaded images (== number of segments out)
    audio_duration — total voiceover duration in seconds
    sentence_starts — optional Whisper segment (sentence) start times; when
                     given, boundaries prefer a sentence start near the
                     proportional target (see _snap_boundaries)

    Returns [{'index': 1, 'start': 0.0, 'end': 11.8}, ...] (1-based index,
    matching image order). Raises SegmentValidationError when the image
    count is clearly too high for the amount of speech.
    """
    try:
        image_count = int(image_count)
    except (TypeError, ValueError):
        raise SegmentValidationError("Image count must be a whole number.")
    if image_count < 1:
        raise SegmentValidationError("At least one image is required.")

    try:
        audio_duration = float(audio_duration)
    except (TypeError, ValueError):
        raise SegmentValidationError("Audio duration is unknown.")

    # Validation: every image needs at least MIN_SEGMENT_SECONDS of screen
    # time, otherwise the result would be a broken strobe of images.
    if audio_duration < image_count * MIN_SEGMENT_SECONDS - _EPS:
        raise SegmentValidationError(
            f"Not enough speech for {image_count} images: the voiceover is "
            f"{audio_duration:.1f}s but each image needs at least "
            f"{MIN_SEGMENT_SECONDS}s "
            f"(minimum {image_count * MIN_SEGMENT_SECONDS:.1f}s). "
            f"Use fewer images or a longer voiceover."
        )

    if image_count == 1:
        return [{"index": 1, "start": 0.0, "end": audio_duration}]

    clean = _clean_words(words)
    candidates = _word_boundaries(clean, audio_duration)
    sentences = _clean_sentence_starts(sentence_starts, audio_duration)

    # Equal image/sentence counts: exact 1:1 auto-match - image i starts
    # on sentence i's first word (image 0 also covers any lead-in, since
    # boundary 1 is sentences[1]). Only used when the resulting cuts are
    # fully valid; otherwise the general tiered snap below takes over.
    if len(sentences) == image_count:
        direct = sentences[1:]
        if _boundaries_ok(direct, image_count, audio_duration):
            return _build_segments(direct, image_count, audio_duration)

    # Tiered snap: sentence starts first, then word gaps, then the
    # proportional clamp. With neither sentence nor word data this
    # degenerates to proportional (equal) segmentation - validation
    # above already guarantees every equal slice respects the minimum.
    boundaries = _snap_boundaries(
        candidates, image_count, audio_duration, sentences
    )
    return _build_segments(boundaries, image_count, audio_duration)


def _clean_sentence_starts(sentence_starts, audio_duration):
    """Sorted unique sentence starts strictly inside (0, audio_duration)."""
    out = set()
    for s in sentence_starts or []:
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue
        if v != v:  # NaN
            continue
        if _EPS < v < audio_duration - _EPS:
            out.add(round(v, 3))
    return sorted(out)


def _boundaries_ok(boundaries, image_count, audio_duration):
    """True when the cut list is strictly increasing inside (0, audio)
    and every resulting segment is at least MIN_SEGMENT_SECONDS long."""
    n = image_count
    if len(boundaries) != n - 1:
        return False
    prev = 0.0
    for i, c in enumerate(boundaries):
        remaining = n - (i + 1)  # segments still needed after this cut
        if c < prev + MIN_SEGMENT_SECONDS - _EPS:
            return False
        if audio_duration - c < remaining * MIN_SEGMENT_SECONDS - _EPS:
            return False
        prev = c
    return True


def _snap_boundaries(candidates, image_count, audio_duration,
                     sentence_starts=None):
    """Pick image_count - 1 cut points near the ideal proportional spots.

    Tier 1 is a real SENTENCE start within `window` of the target: the
    image then flips exactly when the sentence it belongs to begins, so
    a sentence that ends early no longer waits for the next proportional
    cut. Tier 2 is the old behaviour - the nearest word boundary (never
    mid-word, gap midpoints included). When neither offers a feasible
    point, the proportional target itself is clamped into the legal
    range.

    For each internal boundary i the ideal position is
    audio_duration * i / image_count. Cuts are sequential: a chosen
    point constrains the ones after it (never closer than
    MIN_SEGMENT_SECONDS to the previous cut, always leaving enough room
    for every remaining segment).
    """
    n = image_count
    # How far a sentence start may sit from the proportional target and
    # still win: scales with the per-image slice so short timelines get
    # a tight window, long ones a saner absolute floor of 1s.
    window = max(1.0, 0.6 * audio_duration / n)
    sentences = list(sentence_starts or [])
    boundaries = []
    prev = 0.0
    for i in range(1, n):
        target = audio_duration * i / n
        remaining = n - i  # segments still needed from this cut to the end

        def feasible(c):
            return (
                c >= prev + MIN_SEGMENT_SECONDS - _EPS
                and audio_duration - c >= remaining * MIN_SEGMENT_SECONDS - _EPS
            )

        # Tier 1: a sentence start near the target.
        best = None
        best_dist = None
        for c in sentences:
            if c <= prev + _EPS or c >= audio_duration - _EPS:
                continue
            if not feasible(c):
                continue
            dist = abs(c - target)
            if dist > window:
                continue
            if best_dist is None or dist < best_dist:
                best, best_dist = c, dist

        # Tier 2: nearest word boundary (old behaviour).
        if best is None:
            for c in candidates:
                if c <= prev + _EPS or c >= audio_duration - _EPS:
                    continue
                dist = abs(c - target)
                if best_dist is not None and dist >= best_dist:
                    continue
                if feasible(c):
                    best, best_dist = c, dist

        if best is None:
            # Sparse data: clamp the proportional target so both the
            # previous segment and all remaining ones keep their minimum.
            best = min(max(target, prev + MIN_SEGMENT_SECONDS),
                       audio_duration - remaining * MIN_SEGMENT_SECONDS)
        boundaries.append(best)
        prev = best
    return boundaries


def _build_segments(boundaries, image_count, audio_duration):
    """Assemble the final contiguous segment list from cut points."""
    # Round for stable JSON output, then guarantee strict monotonicity
    # (rounding could otherwise merge two very close boundaries).
    rounded = []
    for b in boundaries:
        b = round(b, 3)
        if rounded and b <= rounded[-1]:
            b = round(rounded[-1] + 0.001, 3)
        if b >= audio_duration:
            break
        rounded.append(b)

    # Defensive: never emit fewer cuts than needed (if rounding pushed
    # some out, top up proportionally in the remaining window).
    while len(rounded) < image_count - 1:
        lo = rounded[-1] if rounded else 0.0
        span = audio_duration - lo
        cuts_left = image_count - 1 - len(rounded)
        nxt = round(lo + span / (cuts_left + 1), 3)
        if nxt <= (rounded[-1] if rounded else 0.0):
            nxt = (rounded[-1] if rounded else 0.0) + 0.001
        rounded.append(nxt)

    segments = []
    start = 0.0
    for i, end in enumerate(rounded + [audio_duration]):
        segments.append({
            "index": i + 1,
            "start": round(start, 3),
            "end": round(end, 3),
        })
        start = end
    # The last segment must end at EXACTLY the audio duration.
    segments[-1]["end"] = audio_duration
    return segments
