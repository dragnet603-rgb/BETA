"""Tests for the Autoquence visual-beat planner (offline, Whisper-derived).

Run:  python -m unittest test_beat_planner -v
"""

import unittest

from beat_planner import (
    MAX_BEATS,
    MIN_BEAT_SECONDS,
    MIN_CLAUSE_WORDS,
    extract_clauses,
    plan_beat_count,
    plan_beats,
    plan_beats_from_segments,
)


def make_segments(specs):
    """Build Whisper-style segments from (start, end, text) triples."""
    return [
        {"start": s, "end": e, "text": t} for (s, e, t) in specs
    ]


# A flowing 60s narration: 8 sentence-ish segments, no long silences.
FLOWING_SPEECH = make_segments([
    (0.0, 7.5, "What would happen if the internet suddenly stopped working."),
    (7.5, 15.0, "At first websites would not load and messages would fail."),
    (15.0, 22.5, "Online payments would stop working almost everywhere."),
    (22.5, 30.0, "Businesses would struggle to operate without the cloud."),
    (30.0, 37.5, "Supply chains would slow down within a few days."),
    (37.5, 45.0, "People would fall back on radio and television for news."),
    (45.0, 52.5, "Daily life would move offline in surprising ways."),
    (52.5, 60.0, "A modern city would keep running without the internet."),
])


def check_invariants(self, plan, duration):
    """Assert every contract required of the planner."""
    beats = plan["beats"]
    self.assertGreaterEqual(len(beats), 1)
    self.assertEqual(plan["image_count"], len(beats))
    self.assertEqual([b["number"] for b in beats],
                     list(range(1, len(beats) + 1)))
    self.assertAlmostEqual(beats[0]["start"], 0.0, places=6)
    self.assertAlmostEqual(beats[-1]["end"], duration, places=3)
    for i, beat in enumerate(beats):
        self.assertLess(beat["start"], beat["end"], f"beat {i + 1} empty")
        self.assertTrue(isinstance(beat["description"], str))
        self.assertTrue(beat["description"].strip())
        if i > 0:
            self.assertGreaterEqual(
                beat["start"], beats[i - 1]["end"] - 1e-6,
                "beats overlap or go backwards")
        if len(beats) > 1:
            self.assertGreaterEqual(
                beat["end"] - beat["start"],
                MIN_BEAT_SECONDS - 0.05,
                f"beat {i + 1} is uselessly short")


class TestClauseBreaks(unittest.TestCase):
    """A beat breaks after every comma and full stop."""

    def test_breaks_after_every_comma_and_full_stop(self):
        speech = make_segments([
            (0.0, 4.0, "The internet stops,"),
            (4.0, 8.0, "payments stop working,"),
            (8.0, 12.0, "buses stop moving."),
            (12.0, 16.0, "The city goes quiet."),
        ])
        plan = plan_beats((speech, []), 16.0)
        check_invariants(self, plan, 16.0)
        self.assertEqual(plan["image_count"], 4)
        self.assertAlmostEqual(plan["beats"][0]["end"], 4.0, places=2)
        self.assertAlmostEqual(plan["beats"][1]["end"], 8.0, places=2)
        self.assertAlmostEqual(plan["beats"][2]["end"], 12.0, places=2)

    def test_commas_inside_one_segment_still_split(self):
        # Whisper often returns a whole sentence with several commas in a
        # single segment: each comma must become its own beat.
        speech = make_segments([
            (0.0, 24.0,
             "The lights go out, the water stops, the phones go silent, "
             "and the city sleeps."),
        ])
        plan = plan_beats((speech, []), 24.0)
        check_invariants(self, plan, 24.0)
        self.assertEqual(plan["image_count"], 4)
        self.assertEqual(
            [b["description"] for b in plan["beats"]],
            ["The lights go out,", "the water stops,",
             "the phones go silent,", "the city sleeps."])

    def test_list_commas_do_not_create_one_word_beats(self):
        # "Banks, airlines, delivery services," is one visual moment, not
        # three invisible ones.
        speech = make_segments([
            (0.0, 8.0, "Banks, airlines, delivery services, and offices close."),
        ])
        plan = plan_beats((speech, []), 8.0)
        check_invariants(self, plan, 8.0)
        self.assertEqual(plan["image_count"], 2)
        self.assertGreaterEqual(
            len(plan["beats"][0]["description"].split()), MIN_CLAUSE_WORDS)
        self.assertIn("Banks, airlines, delivery services,",
                      plan["beats"][0]["description"])

    def test_short_clause_is_absorbed_by_shorter_neighbour(self):
        # A 1.5s clause cannot be seen: it joins the shorter neighbour.
        speech = make_segments([
            (0.0, 6.0, "The first big moment happens,"),
            (6.0, 7.5, "tiny but real,"),
            (7.5, 13.0, "then the second moment,"),
            (13.0, 19.0, "and the third moment ends."),
        ])
        plan = plan_beats((speech, []), 19.0)
        check_invariants(self, plan, 19.0)
        self.assertEqual(plan["image_count"], 3)
        self.assertIn("tiny but real,", plan["beats"][1]["description"])
        self.assertIn("then the second moment,", plan["beats"][1]["description"])

    def test_decimal_and_thousands_separator_do_not_split(self):
        speech = make_segments([
            (0.0, 10.0,
             "About 3.5 million people and 1,000 servers went offline."),
        ])
        plan = plan_beats((speech, []), 10.0)
        check_invariants(self, plan, 10.0)
        self.assertEqual(plan["image_count"], 1)
        self.assertIn("3.5 million", plan["beats"][0]["description"])
        self.assertIn("1,000", plan["beats"][0]["description"])

    def test_leading_connector_is_stripped(self):
        speech = make_segments([
            (0.0, 5.0, "And the payments stop,"),
            (5.0, 10.0, "the city goes quiet."),
        ])
        plan = plan_beats((speech, []), 10.0)
        check_invariants(self, plan, 10.0)
        self.assertEqual(plan["beats"][0]["description"], "the payments stop,")

    def test_leading_connector_stripped_on_the_first_beat_too(self):
        # A whole voiceover that opens with "And" must not lose the idea.
        speech = make_segments([(0.0, 6.0, "And the internet goes dark.")])
        plan = plan_beats((speech, []), 6.0)
        self.assertEqual(plan["beats"][0]["description"], "the internet goes dark.")

    def test_run_on_audio_still_gets_several_beats(self):
        # No punctuation at all for 40s: a single beat would be useless,
        # so the long run-on is halved until it matches the cadence.
        speech = make_segments([
            (0.0, 40.0,
             "This is one very long run on sentence that simply keeps going "
             "and never pauses for breath at any point in the narration"),
        ])
        plan = plan_beats((speech, []), 40.0)
        check_invariants(self, plan, 40.0)
        self.assertGreaterEqual(plan["image_count"], 3)

    def test_extract_clauses_groups_fragments(self):
        segments = make_segments([
            (0.0, 2.0, "So."),
            (2.0, 9.0, "Here is a full sentence with enough words in it."),
        ])
        clauses = extract_clauses(segments)
        self.assertEqual(len(clauses), 1)
        self.assertIn("full sentence", clauses[0]["text"])


class TestBeatCounts(unittest.TestCase):

    def test_count_is_not_whisper_segment_count(self):
        # 20 one-idea segments over 40s: the planner must NOT return one
        # beat per Whisper segment.
        staccato = make_segments([
            (i * 2.0, i * 2.0 + 1.8, f"Short idea number {i} here now.")
            for i in range(20)
        ])
        plan = plan_beats((staccato, []), 40.0)
        self.assertLess(plan["image_count"], 20)
        self.assertGreaterEqual(plan["image_count"], 2)

    def test_count_never_exceeds_max(self):
        long_speech = make_segments([
            (i * 3.0, i * 3.0 + 2.9, f"Thought number {i} keeps going on.")
            for i in range(60)
        ])
        plan = plan_beats((long_speech, []), 180.0)
        self.assertLessEqual(plan["image_count"], MAX_BEATS)

    def test_short_audio_never_suggests_faster_than_min_beat(self):
        plan = plan_beats((FLOWING_SPEECH, []), 9.0)
        # 9s // 2.5s minimum -> at most 3 beats.
        self.assertLessEqual(plan["image_count"], 3)

    def test_very_short_audio_single_beat(self):
        plan = plan_beats((FLOWING_SPEECH, []), 2.0)
        self.assertEqual(plan["image_count"], 1)
        self.assertEqual(plan["beats"][0]["end"], 2.0)

    def test_beat_count_grows_with_duration(self):
        short = plan_beats((FLOWING_SPEECH, []), 16.0)
        long = plan_beats((FLOWING_SPEECH, []), 90.0)
        self.assertGreater(long["image_count"], short["image_count"])

    def test_plan_beat_count_duration_only(self):
        # No transcript text at all -> pure duration estimate.
        self.assertEqual(plan_beat_count([], 40.0), 5)

    def test_unknown_duration_falls_back_to_segment_ends(self):
        # No audio duration reported (ffprobe failure): the last segment
        # end is used instead of guessing.
        plan = plan_beats((FLOWING_SPEECH, []), 0)
        check_invariants(self, plan, 60.0)
        self.assertEqual(plan["image_count"], 8)

    def test_nothing_at_all_returns_empty_plan(self):
        # Nothing to plan (no audio duration, no segments): an empty plan
        # is honest — the UI simply keeps the panel hidden.
        plan = plan_beats(([], []), 0)
        self.assertEqual(plan["image_count"], 0)
        self.assertEqual(plan["beats"], [])


class TestBeatBoundaries(unittest.TestCase):

    def test_cuts_land_on_clause_ends(self):
        # 3 clean sentences of exactly 10s each, 30s audio -> beats cut at
        # 10 and 20, never mid-clause.
        speech = make_segments([
            (0.0, 10.0, "First idea stands alone right here."),
            (10.0, 20.0, "Second idea stands alone right here."),
            (20.0, 30.0, "Third idea stands alone right here."),
        ])
        plan = plan_beats((speech, []), 30.0)
        check_invariants(self, plan, 30.0)
        self.assertEqual(plan["image_count"], 3)
        self.assertAlmostEqual(plan["beats"][0]["end"], 10.0, places=2)
        self.assertAlmostEqual(plan["beats"][1]["end"], 20.0, places=2)

    def test_no_gaps_no_backwards_beats(self):
        plan = plan_beats((FLOWING_SPEECH, []), 60.0)
        check_invariants(self, plan, 60.0)

    def test_deterministic(self):
        a = plan_beats((FLOWING_SPEECH, []), 60.0)
        b = plan_beats((FLOWING_SPEECH, []), 60.0)
        self.assertEqual(a, b)


class TestDescriptions(unittest.TestCase):

    def test_descriptions_come_from_the_transcript(self):
        plan = plan_beats((FLOWING_SPEECH, []), 60.0)
        all_text = " ".join(s["text"] for s in FLOWING_SPEECH)
        for beat in plan["beats"]:
            # Each description is a real snippet from the narration
            # (offline planner) — never fabricated content.
            self.assertTrue(beat["description"])
            self.assertTrue(
                beat["description"].strip("…").strip() in all_text,
                f"not a transcript snippet: {beat['description']!r}")

    def test_long_run_on_is_split_and_trimmed(self):
        speech = make_segments([
            (0.0, 30.0,
             "This is a very long winding sentence that keeps piling on "
             "clause after clause after clause and simply refuses to stop "
             "talking at any reasonable length whatsoever indeed."),
        ])
        plan = plan_beats((speech, []), 30.0)
        check_invariants(self, plan, 30.0)
        for beat in plan["beats"]:
            self.assertLessEqual(len(beat["description"]), 91)
            if len(beat["description"]) > 55:
                # A long snippet must visibly signal that it is partial:
                # "…" at the front (beat starts mid-clause) or the back
                # (beat ends mid-clause / text trimmed for length).
                self.assertTrue(
                    beat["description"].startswith("…")
                    or beat["description"].endswith("…"),
                    f"unmarked partial snippet: {beat['description']!r}")

    def test_segments_without_text_still_plan(self):
        # Timing-only segments (old manifests): beats still come out.
        speech = [{"start": 0.0, "end": 5.0}, {"start": 5.0, "end": 10.0}]
        plan = plan_beats((speech, []), 10.0)
        check_invariants(self, plan, 10.0)

    def test_empty_transcript_falls_back_to_proportional(self):
        plan = plan_beats(([], []), 40.0)
        self.assertEqual(plan["image_count"], 5)
        self.assertAlmostEqual(plan["beats"][1]["start"], 8.0, places=2)
        self.assertEqual(plan["beats"][0]["description"], "Visual moment")

    def test_accepts_plain_segment_list(self):
        plan = plan_beats_from_segments(FLOWING_SPEECH, 60.0)
        check_invariants(self, plan, 60.0)


if __name__ == "__main__":
    unittest.main()


