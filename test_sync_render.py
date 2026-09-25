"""Tests for the two bugs behind "10 images but the video looks out of
sync": duplicate uploads that cannot cut, and render cuts quantized onto
a coarse frame grid.

Run:  python -m unittest test_sync_render -v
"""

import tempfile
import unittest
from pathlib import Path

from sync_pipeline import (
    _frame_durations,
    _render_fps,
    duplicate_image_warning,
    find_duplicate_images,
)


class TestDuplicateImages(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, data):
        (self.folder / name).write_bytes(data)

    def test_identical_files_are_grouped(self):
        # The real job: img_001..img_004 were one photo, uploaded 4x.
        pic = b"\xff\xd8same-photo-bytes\xff\xd9"
        self._write("img_001.jpeg", pic)
        self._write("img_002.jpeg", pic)
        self._write("img_003.jpeg", pic)
        self._write("img_004.jpeg", pic)
        self._write("img_005.jpg", b"other")
        groups = find_duplicate_images(
            self.folder,
            ["img_001.jpeg", "img_002.jpeg", "img_003.jpeg",
             "img_004.jpeg", "img_005.jpg"],
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0], ["img_001.jpeg", "img_002.jpeg",
                                     "img_003.jpeg", "img_004.jpeg"])

    def test_all_distinct_returns_nothing(self):
        for i in range(1, 4):
            self._write(f"img_{i:03d}.jpeg", bytes([i]) * 8)
        self.assertEqual(
            find_duplicate_images(
                self.folder,
                ["img_001.jpeg", "img_002.jpeg", "img_003.jpeg"],
            ),
            [],
        )

    def test_missing_file_never_raises(self):
        self.assertEqual(
            find_duplicate_images(self.folder, ["img_001.jpeg"]), [])

    def test_warning_names_the_repeats_and_the_scope(self):
        groups = [["img_001.jpeg", "img_002.jpeg", "img_003.jpeg",
                   "img_004.jpeg"]]
        msg = duplicate_image_warning(groups, 10)
        self.assertIn("3 of your 10 images", msg)
        self.assertIn("Image 1", msg)
        self.assertIn("Image 2, Image 3, Image 4", msg)

    def test_warning_explains_why_a_repeat_cannot_cut(self):
        msg = duplicate_image_warning([["img_001.jpeg", "img_002.jpeg"]], 2)
        self.assertIn("cannot cut to itself", msg)

    def test_no_groups_means_no_warning(self):
        self.assertIsNone(duplicate_image_warning([], 10))
        self.assertIsNone(duplicate_image_warning(None))


class TestFrameGrid(unittest.TestCase):

    def test_default_grid_is_frame_accurate(self):
        self.assertEqual(_render_fps(), 30)

    def test_durations_land_on_the_frame_grid(self):
        durs = [4.28, 3.86, 6.36, 5.2, 5.76, 2.62, 7.12, 7.62, 5.5, 4.78]
        for fps in (2, 30):
            out = _frame_durations(durs, fps)
            self.assertEqual(len(out), len(durs))
            for d in out:
                self.assertAlmostEqual(d * fps, round(d * fps), places=6)

    def test_cumulative_cuts_match_the_timeline(self):
        # The old per-segment rounding drifted 0.2s+ past 10 images; the
        # cumulative form must keep every boundary within half a frame.
        durs = [4.28, 3.86, 6.36, 5.2, 5.76, 2.62, 7.12, 7.62, 5.5, 4.78]
        fps = 30
        out = _frame_durations(durs, fps)
        cum = 0.0
        target = 0.0
        for dur, got in zip(durs, out):
            cum += got
            target += dur
            self.assertLessEqual(abs(cum - target), 0.5 / fps + 1e-9)

    def test_tiny_durations_get_the_minimum_screen_time(self):
        # _frame_durations floors each duration at 0.05s before gridding
        # (a 0.01s image must still be visible), so the total reflects the
        # floored plan and every piece is at least one frame long.
        out = _frame_durations([0.01, 0.01], 30)
        self.assertAlmostEqual(sum(out), 0.1, places=6)   # 2 x 0.05s floor
        for d in out:
            self.assertGreaterEqual(d, 1 / 30 - 1e-9)

    def test_total_length_is_preserved(self):
        durs = [3.0, 3.0, 3.0]
        self.assertAlmostEqual(sum(_frame_durations(durs, 30)), 9.0, places=6)


if __name__ == "__main__":
    unittest.main()
