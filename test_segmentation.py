"""Tests for the Autoquence core audio/image synchronization engine.

Run:  python -m unittest test_segmentation -v
"""

import math
import unittest

from segmentation import (
    MIN_SEGMENT_SECONDS,
    SegmentValidationError,
    create_visual_segments,
)


def make_words(timestamps, dur=0.35):
    """Build word dicts from a list of start times (seconds)."""
    return [{"word": f"w{i}", "start": round(t, 3), "end": round(t + dur, 3)}
            for i, t in enumerate(timestamps)]


def check_invariants(self, segments, image_count, audio_duration):
    """Assert every contract required of the segmenter."""
    self.assertEqual(len(segments), image_count)
    self.assertAlmostEqual(segments[0]["start"], 0.0, places=6)
    self.assertAlmostEqual(segments[-1]["end"], audio_duration, places=6)
    for i, seg in enumerate(segments):
        self.assertLess(seg["start"], seg["end"], f"segment {i} empty")
        self.assertEqual(seg["index"], i + 1)
        if i > 0:
            self.assertAlmostEqual(
                segments[i - 1]["end"], seg["start"], places=6,
                msg=f"gap/overlap between segment {i - 1} and {i}")
    for i in range(len(segments) - 1):
        self.assertLess(segments[i]["start"], segments[i + 1]["start"])


class TestNormalVoiceovers(unittest.TestCase):

    def test_ten_images_normal_voiceover(self):
        # 12s of speech, words every 0.6s -> expect ~12 boundaries
        words = make_words([0.1 + i * 0.6 for i in range(20)])
        segs = create_visual_segments(words, 10, 12.0)
        check_invariants(self, segs, 10, 12.0)

    def test_forty_images_normal_voiceover(self):
        # Whisper may return 25 speech segments; user uploaded 40 images.
        # The engine must still produce exactly 40 visual segments.
        words = make_words([0.2 + i * 0.25 for i in range(100)])  # 0.2s..25.2s
        segs = create_visual_segments(words, 40, 26.0)
        check_invariants(self, segs, 40, 26.0)
        # Should be roughly proportional (26/40 = 0.65s each), not equal to
        # the speech segment count (25).
        for s in segs:
            self.assertGreater(s["end"] - s["start"], 0.4)

    def test_three_images_normal_voiceover(self):
        words = make_words([0.5 + i * 1.0 for i in range(15)])
        segs = create_visual_segments(words, 3, 16.0)
        check_invariants(self, segs, 3, 16.0)
        self.assertAlmostEqual(segs[0]["end"], 5.0, delta=1.0)
        self.assertAlmostEqual(segs[1]["end"], 10.5, delta=1.5)

    def test_boundaries_fall_between_words_not_mid_word(self):
        # 100s audio, 5 images -> ideal cuts at 20/40/60/80.
        # Words end at 19.2/19.8/20.4/21.1 with a small gap before 20.05:
        # the cut must land between words (19.8 word end .. 20.05 word
        # start), never inside a word.
        words = make_words([0.0, 1.0, 18.85, 19.45, 20.05, 20.75, 22.0, 23.0])
        # word i ends at start+0.35: ends are 0.35, 1.35, 19.2, 19.8, 20.4, 21.1, ...
        segs = create_visual_segments(words, 5, 100.0)
        check_invariants(self, segs, 5, 100.0)
        self.assertGreaterEqual(segs[0]["end"], 19.8)
        self.assertLessEqual(segs[0]["end"], 20.05)

    def test_image_count_differs_from_whisper_segment_count(self):
        # 4 natural sentences (each a run of touching words separated by
        # long silences) but only 2 images.
        words = []
        for sentence_start in (1.0, 30.0, 60.0, 90.0):
            words += make_words(
                [sentence_start + i * 0.4 for i in range(8)])
        segs = create_visual_segments(words, 2, 120.0)
        check_invariants(self, segs, 2, 120.0)
        # Cut should land in the big silence between sentence 2 and 3.
        self.assertTrue(55.0 < segs[0]["end"] < 65.0)


class TestSilenceAndShortAudio(unittest.TestCase):

    def test_silence_between_words_midpoint_used(self):
        # A 1.5s gap between word 2 and 3: cutting at the gap midpoint is
        # better than snapping to a word edge.
        words = make_words([0.0, 1.0, 4.0, 5.0])
        segs = create_visual_segments(words, 2, 6.0)
        check_invariants(self, segs, 2, 6.0)
        self.assertAlmostEqual(segs[0]["end"], 2.675, places=6)

    def test_no_words_proportional_fallback(self):
        # Pure silence -> no word timestamps at all; must still return
        # proportional segments instead of crashing.
        segs = create_visual_segments([], 4, 20.0)
        check_invariants(self, segs, 4, 20.0)
        self.assertAlmostEqual(segs[0]["end"], 5.0, places=6)
        self.assertAlmostEqual(segs[1]["end"], 10.0, places=6)

    def test_very_short_audio_few_images(self):
        words = make_words([0.0, 1.0, 2.0, 3.0])
        segs = create_visual_segments(words, 3, 3.5)
        check_invariants(self, segs, 3, 3.5)

    def test_single_image_covers_whole_audio(self):
        segs = create_visual_segments([], 1, 42.0)
        check_invariants(self, segs, 1, 42.0)

    def test_image_count_too_high_raises_validation_error(self):
        # 9.9s voiceover, 20 images: 20 * 0.5s = 10s > 9.9s available.
        with self.assertRaises(SegmentValidationError):
            create_visual_segments([], 20, 9.9)
        # Even with words present, 20 * 0.5s = 10s > 8s available.
        words = make_words([0.0 + i * 0.3 for i in range(25)])
        with self.assertRaises(SegmentValidationError):
            create_visual_segments(words, 20, 8.0)

    def test_exactly_at_minimum_is_allowed(self):
        # 20 images over exactly 10s -> 0.5s each: at the documented
        # minimum, so this must NOT raise.
        segs = create_visual_segments([], 20, 10.0)
        check_invariants(self, segs, 20, 10.0)

    def test_minimum_segment_duration_respected(self):
        # 6 images over 3.3s: 3.3/6 = 0.55s each — above the 0.5s floor.
        words = make_words([0.0 + i * 0.15 for i in range(20)])
        segs = create_visual_segments(words, 6, 3.3)
        check_invariants(self, segs, 6, 3.3)
        for s in segs:
            self.assertGreaterEqual(
                s["end"] - s["start"],
                MIN_SEGMENT_SECONDS - 1e-6,
                "no segment may be shorter than the documented minimum")


class TestContracts(unittest.TestCase):

    def test_leading_silence_first_segment_starts_at_zero(self):
        # Voiceover starts at 30s: segment 1 still starts at 0.0.
        words = make_words([30.0 + i * 0.5 for i in range(20)])
        segs = create_visual_segments(words, 4, 40.0)
        check_invariants(self, segs, 4, 40.0)
        self.assertAlmostEqual(segs[0]["start"], 0.0, places=6)

    def test_final_segment_reaches_exact_audio_duration(self):
        words = make_words([0.0 + i * 0.7 for i in range(50)])
        for n in (2, 3, 5, 7, 11, 13):
            segs = create_visual_segments(words, n, 37.123)
            self.assertEqual(segs[-1]["end"], 37.123)

    def test_no_gaps_no_overlaps_chronological(self):
        words = make_words([0.0 + i * 0.45 for i in range(60)])
        segs = create_visual_segments(words, 12, 28.0)
        for i in range(len(segs) - 1):
            self.assertEqual(segs[i]["end"], segs[i + 1]["start"])
            self.assertLess(segs[i]["end"], segs[i + 1]["end"])
        self.assertTrue(all(math.isfinite(s["start"]) and math.isfinite(s["end"])
                            for s in segs))

    def test_deterministic(self):
        words = make_words([0.0 + i * 0.5 for i in range(40)])
        a = create_visual_segments(words, 9, 21.0)
        b = create_visual_segments(words, 9, 21.0)
        self.assertEqual(a, b)

    def test_beyond_last_word_last_segment_holds_to_audio_end(self):
        # Speech ends at 6s, audio runs to 15s: last image holds the tail.
        words = make_words([0.0 + i * 0.5 for i in range(12)])
        segs = create_visual_segments(words, 3, 15.0)
        check_invariants(self, segs, 3, 15.0)
        self.assertEqual(segs[-1]["end"], 15.0)


class TestSentenceAware(unittest.TestCase):
    """Boundary preference for real sentence starts - the fix for
    'the sentence ended but the image waited to switch' - plus the
    exact 1:1 image<->sentence auto-match."""

    # Real sentence starts from job 0d3be1a34c3f (10 sentences, 10 images).
    SENTENCES_10 = [0.08, 4.28, 8.14, 14.5, 19.7, 25.46,
                    28.08, 35.2, 42.82, 48.38]

    def test_equal_counts_map_images_to_sentences_exactly(self):
        words = []
        for s in self.SENTENCES_10:
            words += make_words([s + i * 0.4 for i in range(6)])
        segs = create_visual_segments(
            words, 10, 51.97, sentence_starts=self.SENTENCES_10)
        check_invariants(self, segs, 10, 51.97)
        for i in range(1, 10):
            self.assertAlmostEqual(
                segs[i]["start"], self.SENTENCES_10[i], places=3,
                msg=f"image {i} must start exactly on sentence {i}")

    def test_unequal_counts_prefer_nearby_sentence_starts(self):
        # 6 sentences, 5 images: every cut lands ON a sentence start
        # (all sit within the tier-1 window of their proportional
        # target), while the invariants still hold.
        sentences = [0.1, 10.0, 20.0, 30.0, 40.0, 50.0]
        words = []
        for s in sentences:
            words += make_words([s + i * 0.5 for i in range(8)])
        segs = create_visual_segments(
            words, 5, 60.0, sentence_starts=sentences)
        check_invariants(self, segs, 5, 60.0)
        for i in range(1, 5):
            self.assertIn(round(segs[i]["start"], 3), sentences)

    def test_far_sentence_falls_back_to_word_gap(self):
        # Sentence starts far outside the window must not drag a cut
        # across the timeline; the old word-gap snap still applies.
        words = make_words([float(t) for t in range(0, 100)])
        segs = create_visual_segments(
            words, 4, 100.0, sentence_starts=[0.5, 5.0])
        check_invariants(self, segs, 4, 100.0)
        # First target is 25s; sentence 5.0 sits 20s away (window is
        # 15s) so the cut stays near the target inside a word gap.
        self.assertNotAlmostEqual(segs[0]["end"], 5.0, delta=0.001)
        self.assertAlmostEqual(segs[0]["end"], 25.0, delta=1.0)

    def test_sentence_at_zero_or_end_is_ignored(self):
        words = make_words([0.5 + i * 0.9 for i in range(30)])
        segs = create_visual_segments(
            words, 3, 30.0, sentence_starts=[0.0, 5.0, 30.0, 29.999])
        check_invariants(self, segs, 3, 30.0)

    def test_no_sentences_behaves_exactly_like_before(self):
        words = make_words([0.0 + i * 0.7 for i in range(40)])
        a = create_visual_segments(words, 5, 30.0)
        b = create_visual_segments(words, 5, 30.0, sentence_starts=None)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
