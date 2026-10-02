"""Tests for chunked sync renders: big jobs split into small FFmpeg
processes and join with stream-copy concat, so a 150-image job never
holds 150 inputs in one graph on a 512MB box.

Run:  python -m unittest test_sync_chunks -v
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sync_pipeline

class ChunkSplitTest(unittest.TestCase):
    """150 images at 3s each -> 5 chunks of 30; durations conserved."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.images = [f"img_{i:03d}.jpg" for i in range(1, 151)]
        for name in self.images:
            (self.folder / name).write_bytes(b"\xff\xd8fake\xff\xd9")
        self.manifest = {"images": self.images, "clips": [],
                         "segments": [], "status": "awaiting_audio"}

    def tearDown(self):
        self._tmp.cleanup()

    def _chunks(self, **kw):
        with mock.patch.object(sync_pipeline, "job_dir",
                               return_value=self.folder):
            with mock.patch.object(
                    sync_pipeline, "load_manifest",
                    return_value=dict(self.manifest)):
                with tempfile.TemporaryDirectory() as tmp:
                    return sync_pipeline.build_chunk_commands(
                        "unit_test", Path(tmp), **kw)

    def test_150_images_split_into_5_chunks_of_30(self):
        chunk_cmds, join, total = self._chunks()
        self.assertEqual(len(chunk_cmds), 5)
        for _cmd, dur, part in chunk_cmds:
            self.assertEqual(part.name[:5], "part_")
            self.assertAlmostEqual(dur, 90.0)  # 30 x 3s
        self.assertAlmostEqual(total, 450.0)  # 150 x 3s
        self.assertAlmostEqual(sum(d for _, d, _ in chunk_cmds), total)

    def test_small_job_is_one_chunk(self):
        self.images = self.images[:10]
        self.manifest["images"] = self.images
        chunk_cmds, _join, total = self._chunks()
        self.assertEqual(len(chunk_cmds), 1)
        self.assertAlmostEqual(total, 30.0)

    def test_remainder_chunk_holds_the_rest(self):
        self.images = self.images[:35]
        self.manifest["images"] = self.images
        chunk_cmds, _join, _total = self._chunks()
        self.assertEqual(len(chunk_cmds), 2)
        self.assertAlmostEqual(chunk_cmds[1][1], 15.0)  # 5 x 3s

    def test_needs_chunked_render_threshold(self):
        with mock.patch.object(
                sync_pipeline, "_render_plan",
                return_value=([(0, 3.0)] * 30, None, 90.0)):
            self.assertFalse(sync_pipeline.needs_chunked_render("x"))
        with mock.patch.object(
                sync_pipeline, "_render_plan",
                return_value=([(0, 3.0)] * 31, None, 93.0)):
            self.assertTrue(sync_pipeline.needs_chunked_render("x"))

    def test_each_chunk_graph_is_small_and_silent(self):
        chunk_cmds, _join, _total = self._chunks()
        for cmd, _dur, part in chunk_cmds:
            graph = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("concat=n=30:v=1:a=0", graph)
            self.assertIn("-an", cmd)  # silent intermediary
            self.assertEqual(cmd[-1], str(part))

    def test_join_is_stream_copy_without_reencode(self):
        _chunks, join, _total = self._chunks()
        self.assertIn("concat", join)
        self.assertIn("-safe", join)
        self.assertIn("copy", join)
        self.assertNotIn("libx264", join)  # no video re-encode at join

    def test_join_muxes_audio_once_when_present(self):
        audio = self.folder / "audio.mp3"
        audio.write_bytes(b"fake-audio")
        self.manifest = {**self.manifest, "audio": "audio.mp3",
                         "audio_name": "audio.mp3", "status": "ready",
                         "segments": [{"start": 0.0, "end": 450.0,
                                        "image": 0}],
                         "matched": True}
        with mock.patch.object(sync_pipeline, "job_dir",
                               return_value=self.folder):
            with mock.patch.object(
                    sync_pipeline, "load_manifest",
                    return_value=dict(self.manifest)):
                with mock.patch.object(
                        sync_pipeline, "get_media_duration",
                        return_value=450.0):
                    with tempfile.TemporaryDirectory() as tmp:
                        _chunks, join, _total = (
                            sync_pipeline.build_chunk_commands(
                                "unit_test", Path(tmp)))
        self.assertIn("audio.mp3", " ".join(join))
        self.assertIn("-c:v", join)
        self.assertIn("copy", join)  # video still stream-copied

    def test_frame_counts_conserved_across_chunks(self):
        # Chunk-local frame counts must sum to the single-graph counts for
        # the same durations: no drift at chunk seams.
        from sync_pipeline import _frame_counts, _render_fps
        fps = _render_fps()
        durs = [3.0] * 150
        whole = _frame_counts(durs, fps)
        split = []
        for s in range(0, 150, 30):
            split += _frame_counts(durs[s:s + 30], fps)
        self.assertEqual(split, whole)

    def test_invalid_job_raises_like_single_render(self):
        with mock.patch.object(sync_pipeline, "job_dir",
                               return_value=self.folder):
            with mock.patch.object(sync_pipeline, "load_manifest",
                                   return_value=None):
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(FileNotFoundError):
                        sync_pipeline.build_chunk_commands("nope", Path(tmp))


if __name__ == "__main__":
    unittest.main()

