"""Tests for the sync pipeline timeline helpers (clips <-> segments and
the sentence-aware resolve) that fix 'script applied but the rendered
video ignored it'.

Run:  python -m unittest test_sync_timeline -v
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from sync_pipeline import (
    clips_from_segments,
    duplicate_image_warning,
    find_duplicate_images,
    resolve_visual_segments,
    segments_from_clips,
)


# Real transcript of job 0d3be1a34c3f: the voiceover's 10 sentences.
REAL_SENTENCES = [
    (0.08, 3.24, "What would happen if the internet suddenly stopped working?"),
    (4.28, 7.4, "At first most people wouldn't even realize what happened."),
    (8.14, 13.56, "Websites would stop loading, messages wouldn't send."),
    (14.5, 18.86, "Within hours, businesses would start shutting down."),
    (19.7, 24.52, "Banks, airlines, delivery services, and offices."),
    (25.46, 27.36, "Then the bigger problems would appear."),
    (28.08, 34.22, "Supply chains would slow down because of all this."),
    (35.2, 41.82, "People would turn to phone calls, radio, television."),
    (42.82, 47.8, "And if the outage lasted for weeks, we would notice."),
    (48.38, 51.18, "Entire parts of modern life would have to change."),
]
AUDIO_DUR = 51.97125


def sentences():
    return [{"start": s, "end": e, "text": t} for s, e, t in REAL_SENTENCES]


def words():
    out = []
    for start, end, _ in REAL_SENTENCES:
        t = start
        while t + 0.3 <= end:
            out.append({"word": "w", "start": round(t, 3),
                        "end": round(t + 0.3, 3)})
            t += 0.4
    return out


class TestResolveUsesSentences(unittest.TestCase):

    def test_image_i_starts_with_sentence_i(self):
        segs, matched, warning = resolve_visual_segments(
            words(), sentences(), 10, AUDIO_DUR)
        self.assertTrue(matched)
        self.assertIsNone(warning)
        self.assertEqual(len(segs), 10)
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertAlmostEqual(segs[-1]["end"], AUDIO_DUR, places=6)
        for i in range(1, 10):
            self.assertAlmostEqual(
                segs[i]["start"], REAL_SENTENCES[i][0], places=3,
                msg=f"image {i} must start exactly on sentence {i}")

    def test_contiguous_and_assigned_in_order(self):
        segs, matched, _ = resolve_visual_segments(
            words(), sentences(), 10, AUDIO_DUR)
        self.assertTrue(matched)
        for i, seg in enumerate(segs):
            self.assertEqual(seg["image"], i)
            if i:
                self.assertEqual(segs[i - 1]["end"], seg["start"])

    def test_sentence_sync_works_without_word_timestamps(self):
        # Sentence starts alone are enough for the 1:1 auto-match.
        segs, matched, _ = resolve_visual_segments(
            [], sentences(), 10, AUDIO_DUR)
        self.assertTrue(matched)
        self.assertAlmostEqual(segs[5]["start"], REAL_SENTENCES[5][0],
                               places=3)


class TestClipSegmentConversion(unittest.TestCase):

    def test_clips_from_segments_mirror_durations(self):
        segs, _, _ = resolve_visual_segments(
            words(), sentences(), 10, AUDIO_DUR)
        clips = clips_from_segments(segs)
        self.assertEqual(len(clips), 10)
        for i, c in enumerate(clips):
            self.assertEqual(c["image"], i)
            self.assertAlmostEqual(
                c["duration"], segs[i]["end"] - segs[i]["start"], places=3)

    def test_segments_from_clips_extends_last_to_audio(self):
        clips = [{"image": 0, "duration": 3.0},
                 {"image": 1, "duration": 3.0}]
        segs = segments_from_clips(clips, 10.0)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertEqual(segs[0]["end"], 3.0)
        self.assertEqual(segs[-1]["end"], 10.0)  # last image holds the tail

    def test_segments_from_clips_squeezes_overflow_from_the_tail(self):
        clips = [{"image": i, "duration": 20.0} for i in range(3)]
        segs = segments_from_clips(clips, 25.0)
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[-1]["end"], 25.0)
        for seg in segs:
            self.assertLess(seg["start"], seg["end"])
            self.assertGreaterEqual(seg["end"] - seg["start"], 0.5)
        for i in range(1, 3):
            self.assertEqual(segs[i - 1]["end"], segs[i]["start"])

    def test_roundtrip_stays_stable(self):
        # derive(clips_from(segs)) reproduces segs: what the preview
        # shows is what the render gets, save after save.
        segs, _, _ = resolve_visual_segments(
            words(), sentences(), 10, AUDIO_DUR)
        again = segments_from_clips(clips_from_segments(segs), AUDIO_DUR)
        self.assertEqual(len(again), len(segs))
        for a, b in zip(again, segs):
            self.assertEqual(a["image"], b["image"])
            self.assertAlmostEqual(a["start"], b["start"], places=3)
            self.assertAlmostEqual(a["end"], b["end"], places=3)


class TestDuplicateImages(unittest.TestCase):
    """The same picture uploaded twice cannot cut to itself: the video
    holds it across those beats, which reads as "the sync is off" even
    when every boundary is exact (job 0d3be1a34c3f did this - img_001,
    002, 003 and 004 were byte-identical)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, data=b"picture"):
        (self.folder / name).write_bytes(data)

    def test_identical_files_are_grouped(self):
        self._write("img_001.jpeg", b"a")
        self._write("img_002.jpeg", b"a")
        self._write("img_003.jpeg", b"a")
        self._write("img_004.jpeg", b"b")
        groups = find_duplicate_images(
            self.folder, ["img_001.jpeg", "img_002.jpeg",
                          "img_003.jpeg", "img_004.jpeg"])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0], ["img_001.jpeg", "img_002.jpeg",
                                     "img_003.jpeg"])

    def test_all_different_images_produce_no_groups(self):
        for i in range(1, 11):
            self._write(f"img_{i:03d}.jpeg", bytes([i]))
        names = [f"img_{i:03d}.jpeg" for i in range(1, 11)]
        self.assertEqual(find_duplicate_images(self.folder, names), [])
        self.assertIsNone(duplicate_image_warning([], len(names)))

    def test_warning_names_the_repeats_and_the_count(self):
        message = duplicate_image_warning(
            [["img_001.jpeg", "img_002.jpeg", "img_003.jpeg", "img_004.jpeg"]],
            10,
        )
        self.assertIn("3 of your 10 images", message)
        self.assertIn("Image 1 is also Image 2, Image 3, Image 4", message)
        self.assertIn("holds that image", message)

    def test_unreadable_file_never_breaks_an_upload(self):
        self._write("img_001.jpeg", b"a")
        groups = find_duplicate_images(
            self.folder, ["img_001.jpeg", "img_002_missing.jpeg"])
        self.assertEqual(groups, [])

    def test_four_real_files_round_trip_through_the_stored_names(self):
        # Guards the exact job: the four picks were one file saved as four
        # names, so the hashes - not the names - must drive the grouping.
        data = b"same picture bytes"
        names = [f"img_{i:03d}.jpeg" for i in range(1, 5)]
        for name in names:
            self._write(name, data)
        self.assertEqual(len(find_duplicate_images(self.folder, names)), 1)
        self.assertEqual(hashlib.sha256(data).hexdigest()[:8],
                         hashlib.sha256(
                             (self.folder / names[0]).read_bytes()
                         ).hexdigest()[:8])


if __name__ == "__main__":
    unittest.main()