"""Tests for chunked sync renders: big jobs split into small FFmpeg
processes and join with stream-copy concat, so a 150-image job never
holds 150 inputs in one graph on a 512MB box.

Run:  python -m unittest test_sync_chunks -v
"""

import os
import shutil
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

    def test_200_images_split_into_7_chunks(self):
        # 200 = 6 x 30 + 1 x 20: the remainder chunk holds the last 20.
        extra = [f"img_{i:03d}.jpg" for i in range(151, 201)]
        for name in extra:
            (self.folder / name).write_bytes(b"\xff\xd8fake\xff\xd9")
        self.images += extra
        self.manifest["images"] = list(self.images)
        chunk_cmds, _join, total = self._chunks()
        self.assertEqual(len(chunk_cmds), 7)
        durs = [d for _c, d, _p in chunk_cmds]
        self.assertEqual(durs[:6], [90.0] * 6)  # full chunks of 30
        self.assertAlmostEqual(durs[6], 60.0)    # remainder: 20 x 3s
        self.assertAlmostEqual(total, 600.0)     # 200 x 3s
        self.assertAlmostEqual(sum(durs), total)
        for _cmd, _dur, part in chunk_cmds:
            self.assertTrue(part.name.startswith("part_"))

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
        # the same durations: no drift at chunk seams (checked at the full
        # 200-image job size, 7 chunks).
        from sync_pipeline import _frame_counts, _render_fps
        fps = _render_fps()
        durs = [3.0] * 200
        whole = _frame_counts(durs, fps)
        split = []
        for s in range(0, 200, 30):
            split += _frame_counts(durs[s:s + 30], fps)
        self.assertEqual(split, whole)

    def test_join_paths_resolved_from_relative_job_dir(self):
        # job_dir() is relative (static/uploads/<id>) and the join runs
        # with cwd=tmpdir: without resolving, ffmpeg would look for the
        # voiceover INSIDE the temp dir - and a relative output= would be
        # written there too (regression: chunked joins failed with
        # "Error opening output ... No such file or directory").
        rel_folder = Path("static/uploads") / "_test_chunk_join"
        rel_folder.mkdir(parents=True, exist_ok=True)
        (rel_folder / "audio.mp3").write_bytes(b"fake-audio")
        for name in self.images:
            (rel_folder / name).write_bytes(b"\xff\xd8fake\xff\xd9")
        try:
            self.manifest = {**self.manifest, "audio": "audio.mp3",
                             "audio_name": "audio.mp3", "status": "ready",
                             "segments": [{"start": 0.0, "end": 450.0,
                                           "image": 0}],
                             "matched": True}
            with mock.patch.object(sync_pipeline, "job_dir",
                                   return_value=rel_folder):
                with mock.patch.object(
                        sync_pipeline, "load_manifest",
                        return_value=dict(self.manifest)):
                    with mock.patch.object(
                            sync_pipeline, "get_media_duration",
                            return_value=450.0):
                        with tempfile.TemporaryDirectory() as tmp:
                            _chunks, join, _total = (
                                sync_pipeline.build_chunk_commands(
                                    "unit_test", Path(tmp),
                                    output=Path("static/outputs/j.mp4")))
        finally:
            shutil.rmtree(rel_folder, ignore_errors=True)
        inputs = [join[i + 1] for i, a in enumerate(join) if a == "-i"]
        self.assertEqual(inputs[0], "parts.txt")  # basename: cwd=tmpdir
        audio_ins = [p for p in inputs if p != "parts.txt"]
        self.assertEqual(len(audio_ins), 1)
        self.assertTrue(Path(audio_ins[0]).is_absolute())
        self.assertTrue(audio_ins[0].endswith("audio.mp3"))
        out = Path(join[-1])
        self.assertTrue(out.is_absolute())
        self.assertEqual(out.parts[-3:], ("static", "outputs", "j.mp4"))

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

