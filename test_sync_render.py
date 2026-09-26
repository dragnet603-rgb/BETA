"""Tests for the two bugs behind "10 images but the video looks out of
sync": duplicate uploads that cannot cut, and render cuts quantized onto
a coarse frame grid. Also locks the render GRAPH itself: each still is
decoded and scaled once and then duplicated to an exact frame count, the
encoder thread count follows the deployment env, and the legacy per-frame
graph stays reachable behind SYNC_RENDER_LEGACY_GRAPH.

Run:  python -m unittest test_sync_render -v
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sync_pipeline
from sync_pipeline import (
    _frame_counts,
    _frame_durations,
    _render_filter_threads,
    _render_fps,
    _render_threads,
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


class TestFrameCounts(unittest.TestCase):
    """The render graph consumes whole frames, not floating-point seconds."""

    DURS = [4.28, 3.86, 6.36, 5.2, 5.76, 2.62, 7.12, 7.62, 5.5, 4.78]

    def test_counts_are_whole_frames_on_the_grid(self):
        counts = _frame_counts(self.DURS, 30)
        self.assertEqual(len(counts), len(self.DURS))
        for c in counts:
            self.assertIsInstance(c, int)
            self.assertGreaterEqual(c, 1)

    def test_cumulative_cuts_stay_within_half_a_frame(self):
        counts = _frame_counts(self.DURS, 30)
        fps = 30
        cum_frames = 0
        target = 0.0
        for dur, c in zip(self.DURS, counts):
            cum_frames += c
            target += dur
            self.assertLessEqual(
                abs(cum_frames / fps - target), 0.5 / fps + 1e-9
            )

    def test_durations_are_the_same_grid_in_seconds(self):
        self.assertEqual(
            _frame_durations(self.DURS, 30),
            [c / 30 for c in _frame_counts(self.DURS, 30)],
        )

    def test_total_frames_match_the_planned_total(self):
        # 3 + 3 + 3 seconds at 30 fps is 270 frames, i.e. exactly 9.0s.
        self.assertEqual(sum(_frame_counts([3.0, 3.0, 3.0], 30)), 270)

    def test_tiny_durations_get_at_least_one_frame(self):
        counts = _frame_counts([0.01, 0.01], 30)
        self.assertTrue(all(c >= 1 for c in counts))
        self.assertEqual(sum(counts), 3)   # 2 x 0.05s floor -> 2 + 1 frames


class TestRenderThreads(unittest.TestCase):
    """Encoder/filter threads are deployment knobs with safe defaults."""

    ENV = ("SYNC_FFMPEG_THREADS", "FFMPEG_THREADS", "SYNC_FFMPEG_FILTER_THREADS")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        for key in self.ENV:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_stays_two(self):
        # The 512MB-host default must not drift: raising it is opt-in.
        self.assertEqual(_render_threads(), 2)

    def test_sync_var_wins_over_the_shared_one(self):
        os.environ["FFMPEG_THREADS"] = "6"
        os.environ["SYNC_FFMPEG_THREADS"] = "3"
        self.assertEqual(_render_threads(), 3)

    def test_shared_app_var_is_honoured(self):
        os.environ["FFMPEG_THREADS"] = "6"
        self.assertEqual(_render_threads(), 6)

    def test_zero_means_let_x264_decide(self):
        os.environ["SYNC_FFMPEG_THREADS"] = "0"
        self.assertEqual(_render_threads(), 0)

    def test_garbage_falls_back_and_huge_values_are_clamped(self):
        os.environ["SYNC_FFMPEG_THREADS"] = "lots"
        self.assertEqual(_render_threads(), 2)
        os.environ["SYNC_FFMPEG_THREADS"] = "999"
        self.assertEqual(_render_threads(), sync_pipeline._SYNC_THREADS_MAX)
        os.environ["SYNC_FFMPEG_THREADS"] = ""
        self.assertEqual(_render_threads(), 2)

    def test_filter_threads_default_and_override(self):
        self.assertEqual(_render_filter_threads(), 1)
        os.environ["SYNC_FFMPEG_FILTER_THREADS"] = "4"
        self.assertEqual(_render_filter_threads(), 4)
        os.environ["SYNC_FFMPEG_FILTER_THREADS"] = "nope"
        self.assertEqual(_render_filter_threads(), 1)


class TestRenderGraph(unittest.TestCase):
    """The command builder: one decode+scale per still, exact frame counts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        # Contents never matter: the builder only asks whether the file
        # exists (and _shrink_for_render returns undecodable files as-is).
        self.images = ["img_001.jpg", "img_002.jpg"]
        for name in self.images:
            (self.folder / name).write_bytes(b"not really a jpeg")
        self.manifest = {
            "images": self.images,
            "clips": [
                {"image": 0, "duration": 3.0},
                {"image": 1, "duration": 3.0},
            ],
            "segments": [],
            "matched": False,
            "audio": None,
            "status": "ready",
        }
        self._saved = {
            k: os.environ.get(k)
            for k in ("SYNC_RENDER_LEGACY_GRAPH", "SYNC_RENDER_FPS",
                      "SYNC_FFMPEG_THREADS", "FFMPEG_THREADS")
        }
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _command(self):
        out = self.folder / "out.mp4"
        with mock.patch.object(
            sync_pipeline, "load_manifest", return_value=self.manifest
        ), mock.patch.object(
            sync_pipeline, "job_dir", return_value=self.folder
        ):
            return sync_pipeline.build_render_command("unit_test", out)

    @staticmethod
    def _graph(cmd):
        return cmd[cmd.index("-filter_complex") + 1]

    def test_each_still_is_decoded_and_scaled_once(self):
        cmd, total = self._command()
        graph = self._graph(cmd)
        # No per-frame demuxer looping any more: one read per still.
        self.assertEqual(cmd.count("-loop"), 0)
        # Exactly one scale/pad per still, then duplicate + exact trim.
        self.assertEqual(graph.count("scale=1920:1080"), 2)
        self.assertEqual(graph.count("loop=loop=-1:size=1:start=0"), 2)
        self.assertEqual(graph.count("trim=end_frame=90"), 2)   # 3.0s @30fps
        self.assertEqual(graph.count("setpts=PTS-STARTPTS"), 2)
        self.assertAlmostEqual(total, 6.0)

    def test_concat_normalises_on_the_render_fps_grid(self):
        cmd, _ = self._command()
        self.assertIn("concat=n=2:v=1:a=0,fps=30[vout]", self._graph(cmd))
        with mock.patch.dict(os.environ, {"SYNC_RENDER_FPS": "12"}):
            cmd, _ = self._command()
        graph = self._graph(cmd)
        self.assertIn("fps=12[vout]", graph)
        self.assertEqual(graph.count("trim=end_frame=36"), 2)   # 3.0s @12fps

    def test_legacy_graph_is_still_reachable(self):
        with mock.patch.dict(os.environ, {"SYNC_RENDER_LEGACY_GRAPH": "1"}):
            cmd, _ = self._command()
        graph = self._graph(cmd)
        self.assertEqual(cmd.count("-loop"), 2)          # -loop 1 per input
        self.assertNotIn("loop=loop=-1", graph)
        self.assertNotIn("trim=end_frame=", graph)

    def test_threads_flag_follows_the_env(self):
        cmd, _ = self._command()
        self.assertEqual(cmd[cmd.index("-threads") + 1], "2")   # default
        with mock.patch.dict(os.environ, {"SYNC_FFMPEG_THREADS": "5"}):
            cmd, _ = self._command()
        self.assertEqual(cmd[cmd.index("-threads") + 1], "5")


if __name__ == "__main__":
    unittest.main()
