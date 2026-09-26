"""Unit tests for the on-device (WebGPU) transcript adoption helpers.

static/client-transcribe.js produces these segments in the browser;
sync_pipeline validates/normalizes them so the server can adopt them
exactly like faster-whisper's output. See POST /sync/client-transcript.
"""

import os
import unittest
from unittest import mock

import sync_pipeline


class TestClientTranscribeEnabled(unittest.TestCase):
    """SYNC_CLIENT_TRANSCRIBE env toggle: server-side transcription is the
    default; on-device adoption needs an explicit SYNC_CLIENT_TRANSCRIBE=1."""

    def test_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SYNC_CLIENT_TRANSCRIBE", None)
            self.assertFalse(sync_pipeline.client_transcribe_enabled())

    def test_disabled_values(self):
        # Anything that is not an explicit opt-in stays off - including an
        # empty value and garbage a dashboard might inject.
        for val in ("0", "false", "OFF", " no ", "", "banana"):
            with self.subTest(val=val), mock.patch.dict(
                    os.environ, {"SYNC_CLIENT_TRANSCRIBE": val}):
                self.assertFalse(sync_pipeline.client_transcribe_enabled())

    def test_enabled_values(self):
        for val in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(val=val), mock.patch.dict(
                    os.environ, {"SYNC_CLIENT_TRANSCRIBE": val}):
                self.assertTrue(sync_pipeline.client_transcribe_enabled())


class TestCleanClientSegments(unittest.TestCase):
    SEG = {"start": 1.0, "end": 3.0, "text": " Hello world "}

    def test_rejects_non_lists(self):
        self.assertIsNone(sync_pipeline.clean_client_segments(None, 10))
        self.assertIsNone(sync_pipeline.clean_client_segments([], 10))
        self.assertIsNone(sync_pipeline.clean_client_segments("x", 10))
        self.assertIsNone(sync_pipeline.clean_client_segments({"start": 0}, 10))

    def test_normalizes_valid_segment(self):
        out = sync_pipeline.clean_client_segments([self.SEG], 10)
        self.assertEqual(
            out, [{"start": 1.0, "end": 3.0, "text": "Hello world"}]
        )

    def test_drops_invalid_segments(self):
        raw = [
            "not a dict",
            {"start": "x", "end": 2, "text": "a"},
            {"start": -1, "end": 2, "text": "a"},       # negative start
            {"start": 4, "end": 4, "text": "a"},         # zero length
            {"start": 1, "end": 2, "text": "   "},       # empty text
            {"start": 0, "end": 50, "text": "a"},        # past 10s audio
            {"start": 1, "end": 2, "text": "keep me"},
        ]
        out = sync_pipeline.clean_client_segments(raw, 10)
        self.assertEqual(out, [{"start": 1.0, "end": 2.0, "text": "keep me"}])

    def test_clamps_small_overlap_instead_of_dropping(self):
        raw = [
            {"start": 0.0, "end": 5.0, "text": "one"},
            {"start": 4.9, "end": 7.0, "text": "two"},
        ]
        out = sync_pipeline.clean_client_segments(raw, 10)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["start"], 5.0)

    def test_drops_out_of_order_chunk(self):
        raw = [
            {"start": 4.0, "end": 5.0, "text": "one"},
            {"start": 1.0, "end": 2.0, "text": "two"},   # fully before prev
        ]
        out = sync_pipeline.clean_client_segments(raw, 10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "one")

    def test_caps_segment_count_and_text_length(self):
        raw = [
            {"start": i * 0.5, "end": i * 0.5 + 0.4, "text": "x" * 500}
            for i in range(sync_pipeline.CLIENT_TRANSCRIBE_MAX_SEGMENTS + 50)
        ]
        out = sync_pipeline.clean_client_segments(raw, 100000)
        self.assertEqual(len(out), sync_pipeline.CLIENT_TRANSCRIBE_MAX_SEGMENTS)
        self.assertEqual(
            len(out[0]["text"]), sync_pipeline.CLIENT_TRANSCRIBE_MAX_TEXT
        )

    def test_times_rounded_to_milliseconds(self):
        out = sync_pipeline.clean_client_segments(
            [{"start": 0.123456, "end": 1.987654, "text": "a"}], 10
        )
        self.assertEqual(out[0]["start"], 0.123)
        self.assertEqual(out[0]["end"], 1.988)


class TestWordsFromSegments(unittest.TestCase):
    """Word timings are approximated from segment text (the client pass
    has none) - they must be monotonic, in-range and span each segment."""

    def test_empty_input(self):
        self.assertEqual(sync_pipeline.words_from_segments([]), [])
        self.assertEqual(sync_pipeline.words_from_segments(None), [])
        self.assertEqual(
            sync_pipeline.words_from_segments([{"text": "no times"}]), []
        )

    def test_splits_and_spans_one_segment(self):
        words = sync_pipeline.words_from_segments(
            [{"start": 0.0, "end": 3.0, "text": "one two three"}]
        )
        self.assertEqual(len(words), 3)
        self.assertEqual(words[0]["start"], 0.0)
        self.assertEqual(words[-1]["end"], 3.0)
        for a, b in zip(words, words[1:]):
            self.assertAlmostEqual(a["end"], b["start"], places=3)

    def test_monotonic_across_segments(self):
        segs = [
            {"start": 0.0, "end": 2.0, "text": "first half"},
            {"start": 2.5, "end": 5.0, "text": "second half here"},
        ]
        words = sync_pipeline.words_from_segments(segs)
        starts = [w["start"] for w in words]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(words[-1]["end"], 5.0)

    def test_single_word_segment_spans_whole_segment(self):
        self.assertEqual(
            sync_pipeline.words_from_segments(
                [{"start": 1.0, "end": 2.0, "text": "Go"}]
            ),
            [{"start": 1.0, "end": 2.0}],
        )


class TestFileSha256(unittest.TestCase):
    def test_matches_hashlib(self):
        import hashlib
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"
            p.write_bytes(b"voiceover bytes")
            self.assertEqual(
                sync_pipeline.file_sha256(p),
                hashlib.sha256(b"voiceover bytes").hexdigest(),
            )

    def test_missing_file_is_empty(self):
        self.assertEqual(sync_pipeline.file_sha256("no/such/file"), "")


if __name__ == "__main__":
    unittest.main()
