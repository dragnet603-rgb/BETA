"""Autoquence visual-beat planner (offline, Whisper-derived).

Turns the narration into an ADVISORY plan:

    voiceover -> {"image_count": N, "beats": [{"number", "description",
                  "start", "end"}, ...]}

The plan tells the creator roughly HOW MANY images the narration wants
and what each visual moment is about, BEFORE they create anything. It is
guidance only: they can ignore the descriptions, make their own images in
any tool (Flow, ChatGPT, Midjourney, ...) and upload them in order.

Design notes
------------
* Pure Python + stdlib. No LLM, no network, no API key: Whisper already
  returns segments, word timings and the sentence text we need.
* A beat breaks after EVERY comma and full stop (plus ; : and dashes) -
  that is how a creator naturally storyboards, and it keeps every beat
  tight instead of spanning several whole sentences. The count is
  therefore a clause count, never Whisper's segment count.
* Two guards keep the plan usable:
      short fragments  - a piece under MIN_CLAUSE_WORDS words ("airlines,")
                         joins the NEXT clause, so lists stay one beat
      short beats      - a beat under MIN_BEAT_SECONDS is absorbed by its
                         shorter neighbour (a 0.7s shot is invisible)
  plus MAX_BEATS and a punctuation-sparse fallback (see _split_run_ons).
* Beat boundaries always land on a real clause boundary; nothing is cut
  mid-clause unless the narration has no punctuation at all over a long
  stretch.
* Descriptions are short snippets of the actual transcript - never
  fabricated text.
* This module never touches the sync engine. Once the creator uploads
  their images, the existing create_visual_segments() still decides the
  real image timing.
"""

import re

# Tuning constants (public so tests can assert against them).
MIN_BEAT_SECONDS = 2.5       # shorter than this and a shot is unreadable
TARGET_BEAT_SECONDS = 8.0    # cadence used only when punctuation is sparse
MAX_BEATS = 20               # sanity cap for very long narrations
RUN_ON_SECONDS = 2 * TARGET_BEAT_SECONDS  # a beat this long gets halved
PAUSE_SECONDS = 1.0          # silence that reads as a visual change
MIN_CLAUSE_WORDS = 3         # below this a clause is a fragment
MAX_DESCRIPTION_CHARS = 90   # keep beat descriptions glanceable

FALLBACK_DESCRIPTION = "Visual moment"
ELLIPSIS = "\u2026"

# Bumped whenever the planning rules change: the API caches a plan in the
# job manifest, and a stale cache must never hide a planner fix.
PLAN_VERSION = 2

# Split after every comma and sentence end (plus ; : and em/en dashes). A
# split requires whitespace after the mark, so "1,000", "3.5 million" and
# "U.S. Army" never break.
_CLAUSE_RE = re.compile(r"[,.!?\u2026;:\u2014]+(?=\s|$)")

# Beats read better when they do not open on a connector.
_LEAD_CONNECTOR_RE = re.compile(
    r"^(?:and|but|so|also)\s+(?=\S)", re.IGNORECASE)

_EPS = 1e-6
# Beats are rounded to milliseconds for the JSON payload, so description
# coverage must ignore sub-10ms edge noise: otherwise a cut at 5.333
# versus a clause end at 5.33333 reads as "the beat ends mid-clause" and
# the last word of the description gets chopped off.
_EDGE_TOL = 0.01
_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_WS_RUN_RE = re.compile(r"[\r\n\t]+")


def _num(value, default=0.0):
    """Best-effort float, never raises on bad manifest data."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def _word_count(text):
    return len(_WORD_RE.findall(text or ""))


def _split_pieces(text):
    """Split on every comma and sentence end, keeping character offsets."""
    out = []
    pos = 0
    for match in _CLAUSE_RE.finditer(text):
        out.append((pos, match.end(), text[pos:match.end()]))
        pos = match.end()
    if pos < len(text):
        out.append((pos, len(text), text[pos:]))
    return out


def _accumulate_clauses(pieces, min_words=MIN_CLAUSE_WORDS):
    """Group punctuation pieces into beat-sized clauses.

    Accumulation runs FORWARD: a piece too short to stand alone (for
    example "airlines,") joins the NEXT piece, so a list stays one visual
    beat and the boundary lands after the whole clause instead of inside
    it.

    Input/output rows are {"start", "end", "text"} with "text" possibly
    empty (timing-only segments from older manifests).
    """
    units = []
    buffer = []

    def flush():
        if not buffer:
            return
        units.append({
            "start": buffer[0]["start"],
            "end": max(piece["end"] for piece in buffer),
            "text": " ".join(p["text"] for p in buffer if p["text"]).strip(),
        })
        buffer.clear()

    for piece in pieces:
        if not piece["text"]:
            # Timing-only piece: a real boundary in its own right.
            flush()
            units.append(dict(piece))
            continue
        buffer.append(piece)
        if _word_count(" ".join(p["text"] for p in buffer)) >= min_words:
            flush()

    flush()
    return units


def extract_clauses(segments):
    """Whisper segments -> clause units with interpolated timings.

    Whisper gives one timestamp per segment, but a segment can hold
    several sentences and any number of commas. Each clause's time is
    interpolated across its segment by character position - good enough
    to place beats, and it keeps the real segment/word timings
    authoritative.
    """
    pieces = []

    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        start = _num(seg.get("start"))
        end = _num(seg.get("end"), start)
        if end < start:
            start, end = end, start
        span = end - start
        text = (seg.get("text") or "").strip()

        if not text:
            # Timing-only segment (older manifests): still a boundary.
            pieces.append({"start": start, "end": end, "text": ""})
            continue

        total = len(text)
        for raw_start, raw_end, raw in _split_pieces(text):
            clause = raw.strip()
            if not clause:
                continue
            lead = len(raw) - len(raw.lstrip())
            tail = len(raw) - len(raw.rstrip())
            char_start = raw_start + lead
            char_end = raw_end - tail
            pieces.append({
                "start": start + span * (char_start / total),
                "end": start + span * (char_end / total),
                "text": clause,
            })

    return _accumulate_clauses(pieces)


def _speech_groups(segments):
    """Contiguous speech runs, split where silence >= PAUSE_SECONDS."""
    groups = []
    current = None

    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        start = _num(seg.get("start"))
        end = _num(seg.get("end"), start)
        if end < start:
            start, end = end, start
        if current is None or start - current[1] >= PAUSE_SECONDS:
            if current is not None:
                groups.append(current)
            current = [start, end]
        else:
            current[1] = max(current[1], end)

    if current is not None:
        groups.append(current)
    return [(g[0], g[1]) for g in groups]


def _segments_from_words(words):
    """Timing-only segments rebuilt from word timings (no text available)."""
    runs = []
    start = None
    last_end = None

    for word in words or []:
        if not isinstance(word, dict):
            continue
        w_start = _num(word.get("start"))
        w_end = _num(word.get("end"), w_start)
        if start is None or w_start - last_end >= PAUSE_SECONDS:
            if start is not None:
                runs.append({"start": start, "end": last_end, "text": ""})
            start = w_start
        last_end = w_end

    if start is not None:
        runs.append({"start": start, "end": last_end, "text": ""})
    return runs


# ------------------------------------------------------------
# Beat partition: clause units -> the beats actually suggested
# ------------------------------------------------------------

def _contiguous_units(units, duration):
    """Clause units -> contiguous on-screen spans.

    Silence between two clauses belongs to the earlier beat (the same
    coverage rule the sync engine uses), so a beat's real length is
    "previous clause end -> this clause end". The guards below must
    measure THAT, otherwise a 1.8s clause preceded by 0.7s of silence
    looks too short and gets merged away.
    """
    spans = []
    previous = 0.0

    for unit in units or []:
        end = min(_num(unit.get("end")), duration)
        if end <= previous + _EPS:
            continue
        spans.append({"start": previous, "end": end})
        previous = end

    if spans:
        spans[-1]["end"] = duration
    return spans


def _merge_units(units, index):
    """Combine units[index] and units[index + 1] into one beat."""
    first, second = units[index], units[index + 1]
    merged = {
        "start": first["start"],
        "end": max(first["end"], second["end"]),
    }
    return units[:index] + [merged] + units[index + 2:]


def _shorter_neighbour(units, index):
    """Index of the neighbour that is cheaper to absorb."""
    if index == 0:
        return 1
    if index == len(units) - 1:
        return index - 1
    before = units[index - 1]["end"] - units[index - 1]["start"]
    after = units[index + 1]["end"] - units[index + 1]["start"]
    return index - 1 if before <= after else index + 1


def _merge_short_units(units, floor=MIN_BEAT_SECONDS):
    """Absorb every beat shorter than the floor into its shorter neighbour.

    A sub-2.5s clause (say "airlines," at 0.7s) cannot be seen by a
    viewer, so it joins whichever neighbour leaves the more even result.
    """
    units = list(units)
    while len(units) > 1:
        for index, unit in enumerate(units):
            if unit["end"] - unit["start"] >= floor - _EPS:
                continue
            neighbour = _shorter_neighbour(units, index)
            units = _merge_units(units, min(index, neighbour))
            break
        else:
            return units
    return units


def _cap_units(units, cap):
    """Merge the shortest beats until at most `cap` remain."""
    units = list(units)
    while len(units) > cap and len(units) > 1:
        lengths = [u["end"] - u["start"] for u in units]
        index = lengths.index(min(lengths))
        neighbour = _shorter_neighbour(units, index)
        units = _merge_units(units, min(index, neighbour))
    return units


def _beat_cap(duration):
    """Hard ceiling: how many beats the audio can show at the floor length."""
    if duration <= 0:
        return MAX_BEATS
    return max(1, min(MAX_BEATS, int(duration // MIN_BEAT_SECONDS)))


def _cadence_beats(duration):
    """Natural image cadence for narrated video (roughly one per 8s)."""
    if duration <= 0:
        return 1
    return max(1, int(round(duration / TARGET_BEAT_SECONDS)))


def _split_longest_unit(units, floor=MIN_BEAT_SECONDS):
    """Halve the longest beat, or None when none is long enough."""
    index = None
    for i, unit in enumerate(units):
        length = unit["end"] - unit["start"]
        if length < 2 * floor - _EPS:
            continue
        if index is None or length > units[index]["end"] - units[index]["start"]:
            index = i
    if index is None:
        return None

    unit = units[index]
    middle = round((unit["start"] + unit["end"]) / 2, 3)
    return (units[:index]
            + [{"start": unit["start"], "end": middle},
               {"start": middle, "end": unit["end"]}]
            + units[index + 1:])


def _split_run_ons(units, target, floor=MIN_BEAT_SECONDS):
    """Punctuation too sparse for the duration: halve the long beats.

    A 60s comma-free run-on would otherwise be a single 60s beat. Only
    beats longer than RUN_ON_SECONDS are halved, so a normal 4-10s clause
    (which the creator DID punctuate) is never cut mid-clause.
    """
    units = list(units)
    while len(units) < target:
        longest = max(units, key=lambda u: u["end"] - u["start"])
        if longest["end"] - longest["start"] <= RUN_ON_SECONDS + _EPS:
            break
        grown = _split_longest_unit(units, floor)
        if grown is None:
            break
        units = grown
    return units


def _partition_units(clauses, duration):
    """Clause units -> the final beat partition (list of {start, end}).

    One beat per clause, with the two usability guards applied:
    sub-MIN_BEAT_SECONDS clauses are absorbed, the MAX_BEATS /
    minimum-shot-length ceiling is honoured, and only genuinely
    punctuation-free run-ons are halved.
    """
    if duration <= 0:
        return []

    units = _contiguous_units(clauses, duration)
    if not units:
        return []

    cap = _beat_cap(duration)
    units = _merge_short_units(units)
    units = _cap_units(units, cap)
    units = _split_run_ons(units, min(_cadence_beats(duration), cap))
    return units


def _cuts_from_units(units, duration):
    """Beat cut points: every clause end except the audio's own end."""
    cuts = []
    for unit in units[:-1]:
        end = round(min(_num(unit.get("end")), duration), 3)
        if not 0.0 < end < duration:
            continue
        if cuts and end <= cuts[-1] + _EPS:
            continue
        cuts.append(end)
    return cuts


# ------------------------------------------------------------
# Fallback for transcripts with no text at all (timing only)
# ------------------------------------------------------------

def _clamp_count(count, duration):
    """Keep the suggestion feasible: 1..MAX_BEATS, and >= MIN_BEAT_SECONDS."""
    count = max(1, min(MAX_BEATS, int(count)))
    if duration > 0:
        feasible = int(duration // MIN_BEAT_SECONDS)
        count = min(count, max(1, feasible))
    return count


def plan_beat_count(clauses, audio_duration, pause_groups=None):
    """Beat count for a transcript WITHOUT text (timing only).

    With real narration the count is simply the clause count, so this
    duration-based estimate is only reached for old manifests that store
    segment timings but no words.
    """
    duration = max(0.0, _num(audio_duration))
    duration_beats = _cadence_beats(duration)

    speech = [c for c in (clauses or []) if (c.get("text") or "").strip()]
    if not speech:
        return _clamp_count(duration_beats, duration)

    return _clamp_count(duration_beats + len(speech) // 4, duration)


def _boundaries(clauses, groups, duration):
    """Times a beat is allowed to start/end at: clause ends + pauses."""
    out = []
    for clause in clauses or []:
        end = _num(clause.get("end"))
        if 0.0 < end < duration:
            out.append(end)
    for start, end in groups or []:
        for edge in (start, end):
            if 0.0 < edge < duration:
                out.append(edge)
    return out


def _choose_cuts(candidates, count, duration):
    """Pick count-1 cut points nearest to the ideal even split.

    Cuts only land on candidate boundaries (clause/pause edges), and
    always respect MIN_BEAT_SECONDS - so beats follow the narration's own
    structure instead of an arbitrary grid.
    """
    if count <= 1 or duration <= 0:
        return []

    usable = sorted({
        round(_num(c), 3) for c in candidates
        if MIN_BEAT_SECONDS < _num(c) < duration - MIN_BEAT_SECONDS
    })

    cuts = []
    for i in range(1, count):
        ideal = duration * i / count
        floor_cut = cuts[-1] + MIN_BEAT_SECONDS if cuts else MIN_BEAT_SECONDS
        ceiling = duration - (count - i) * MIN_BEAT_SECONDS

        best = None
        for candidate in usable:
            if candidate <= floor_cut - _EPS or candidate >= ceiling + _EPS:
                continue
            if best is None or abs(candidate - ideal) < abs(best - ideal):
                best = candidate
        if best is None:
            # No boundary available (dense speech): fall back to the
            # proportional position, still honouring the minimum length.
            best = min(max(ideal, floor_cut), max(ceiling, floor_cut))
        cuts.append(round(best, 3))

    return _sanitize_cuts(cuts, count, duration)


def _sanitize_cuts(cuts, count, duration):
    """Guarantee an ordered cut list; otherwise use a proportional split."""
    if len(cuts) != count - 1:
        return [round(duration * i / count, 3) for i in range(1, count)]

    previous = 0.0
    for cut in cuts:
        if cut - previous < MIN_BEAT_SECONDS - 0.05:
            return [round(duration * i / count, 3) for i in range(1, count)]
        if cut > duration - MIN_BEAT_SECONDS + 0.05:
            return [round(duration * i / count, 3) for i in range(1, count)]
        previous = cut
    return cuts


# ------------------------------------------------------------
# Descriptions: short, verbatim snippets of the narration
# ------------------------------------------------------------

def _slice_text(text, clause_start, clause_end, from_time, to_time):
    """The part of a clause that falls inside [from_time, to_time]."""
    span = clause_end - clause_start
    if span <= 0:
        return text.strip()
    if (from_time <= clause_start + _EDGE_TOL
            and to_time >= clause_end - _EDGE_TOL):
        return text

    total = len(text)
    start_ratio = min(1.0, max(0.0, (from_time - clause_start) / span))
    end_ratio = min(1.0, max(0.0, (to_time - clause_start) / span))
    # Snap rounded cut times back onto the clause edges.
    if start_ratio * span <= _EDGE_TOL:
        start_ratio = 0.0
    if (1.0 - end_ratio) * span <= _EDGE_TOL:
        end_ratio = 1.0

    start_char = int(round(start_ratio * total))
    end_char = int(round(end_ratio * total))
    if end_char <= start_char:
        return text

    snippet = text[start_char:end_char]
    if start_ratio > 0.0:
        space = snippet.find(" ")
        if space != -1:
            snippet = snippet[space + 1:]  # drop the half word at the front
    if end_ratio < 1.0:
        space = snippet.rfind(" ")
        if space != -1:
            snippet = snippet[:space]      # drop the half word at the back
    return snippet.strip()


def _clean_snippet(text, lead_cut=False, tail_cut=False):
    """Short, single-line description; marked with an ellipsis when partial.

    A leading ellipsis means "the beat starts mid-clause"; a trailing one
    means it ends mid-clause (or the text was trimmed for length).
    """
    text = _WS_RUN_RE.sub(" ", (text or "").strip()).strip(" " + ELLIPSIS)
    if not text:
        return FALLBACK_DESCRIPTION

    # "and online payments would fail." reads better as "online payments
    # would fail." - only the beat's very first connector is dropped.
    stripped = _LEAD_CONNECTOR_RE.sub("", text, count=1)
    if stripped.strip():
        text = stripped.strip()

    trailing = bool(tail_cut)
    reserve = (1 if lead_cut else 0) + (1 if trailing else 0)
    if len(text) + reserve > MAX_DESCRIPTION_CHARS + 1:
        limit = max(1, MAX_DESCRIPTION_CHARS - (1 if lead_cut else 0))
        head = text[:limit]
        space = head.rfind(" ")
        if space > 0:
            head = head[:space]
        text = head.rstrip(" ,;:-")
        trailing = True
        if not text:
            return FALLBACK_DESCRIPTION

    prefix = ELLIPSIS if lead_cut else ""
    suffix = ELLIPSIS if trailing else ""
    return prefix + text + suffix


def _describe(clauses, start, end):
    """Short transcript snippet covering the beat's time window."""
    parts = []
    lead_cut = False
    tail_cut = False

    for clause in clauses or []:
        text = (clause.get("text") or "").strip()
        if not text:
            continue
        c_start = _num(clause.get("start"))
        c_end = _num(clause.get("end"), c_start)
        # Tolerance, not _EPS: cuts are rounded to milliseconds, so a
        # neighbouring clause that ends 0.0005s after this beat starts
        # must not leak into its description.
        if c_end <= start + _EDGE_TOL or c_start >= end - _EDGE_TOL:
            continue

        covered_from = max(start, c_start)
        covered_to = min(end, c_end)
        parts.append(_slice_text(text, c_start, c_end, covered_from, covered_to))
        if covered_from > c_start + _EDGE_TOL:
            lead_cut = True   # the beat starts inside this clause
        if covered_to < c_end - _EDGE_TOL:
            tail_cut = True   # the beat ends inside this clause

    if not parts:
        return FALLBACK_DESCRIPTION
    return _clean_snippet(" ".join(parts), lead_cut, tail_cut)


def _build_plan(cuts, duration, clauses):
    """Assemble the structured response from ordered cut points."""
    edges = [0.0] + list(cuts) + [duration]
    beats = []
    for i in range(len(edges) - 1):
        start, end = edges[i], edges[i + 1]
        beats.append({
            "number": i + 1,
            "description": _describe(clauses, start, end),
            "start": round(start, 3),
            "end": round(end, 3),
        })
    return {"image_count": len(beats), "beats": beats}


# ------------------------------------------------------------
# Public API
# ------------------------------------------------------------

def _unpack(transcript, words=None):
    """Accept (segments, words), {"segments", "words"}, or segments alone."""
    if transcript is None:
        return [], list(words or [])
    if isinstance(transcript, dict):
        return (list(transcript.get("segments") or []),
                list(transcript.get("words") or words or []))
    if (isinstance(transcript, (tuple, list)) and len(transcript) == 2
            and isinstance(transcript[0], (list, tuple))
            and isinstance(transcript[1], (list, tuple))):
        return list(transcript[0]), list(transcript[1])
    if isinstance(transcript, (list, tuple)):
        return list(transcript), list(words or [])
    return [], list(words or [])


def plan_beats(transcript, audio_duration, words=None):
    """Narration -> suggested image count + ordered visual beats.

    transcript: (segments, words) exactly as sync_pipeline.transcribe()
                returns, {"segments", "words"}, or a plain segment list.
    audio_duration: voiceover length in seconds (ffprobe / manifest).

    Returns {"image_count": N, "beats": [{"number", "description",
    "start", "end"}, ...]}: one beat per comma / full stop, with
    unusably short beats absorbed into a neighbour and long
    punctuation-free run-ons halved. An unknown duration (0) returns an
    empty plan rather than bogus beats.

    Advisory only - the creator may ignore every description and upload
    whatever images they like, in order.
    """
    segments, word_list = _unpack(transcript, words)

    duration = max(0.0, _num(audio_duration))
    if duration <= 0:
        for segment in segments:
            if isinstance(segment, dict):
                duration = max(duration, _num(segment.get("end")))
    if duration <= 0:
        return {"image_count": 0, "beats": []}

    if not segments and word_list:
        # Word timings without segments: rebuild speech runs so the plan
        # still follows pauses. There is no text, so the count falls back
        # to the duration estimate.
        segments = _segments_from_words(word_list)

    clauses = extract_clauses(segments)
    speech = [c for c in clauses if (c.get("text") or "").strip()]

    if speech:
        # Real narration: one beat per clause (comma / full stop).
        units = _partition_units(clauses, duration)
        if units:
            cuts = _cuts_from_units(units, duration)
            return _build_plan(cuts, duration, clauses)

    # No text at all (old manifests store timings without words): a
    # duration-based estimate across whatever boundaries we do have.
    groups = _speech_groups(segments)
    count = plan_beat_count(clauses, duration, len(groups))
    cuts = _choose_cuts(_boundaries(clauses, groups, duration), count, duration)
    return _build_plan(cuts, duration, clauses)


def plan_beats_from_segments(segments, audio_duration, words=None):
    """Convenience wrapper for callers that only hold Whisper segments."""
    return plan_beats((list(segments or []), list(words or [])), audio_duration)






