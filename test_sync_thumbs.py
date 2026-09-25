"""Unit tests for filmstrip thumbnail generation (sync_pipeline)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

import sync_pipeline


def _write_image(folder: Path, name: str, size=(800, 600)) -> Path:
    path = folder / name
    Image.new("RGB", size, (20, 90, 160)).save(path)
    return path


class TestMakeThumbnail(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def test_creates_thumbnail_for_large_image(self):
        _write_image(self.folder, "img_001.jpg", (1600, 1200))
        name = sync_pipeline.make_thumbnail(self.folder, "img_001.jpg")
        self.assertEqual(name, "img_001_t.jpg")
        thumb = self.folder / name
        self.assertTrue(thumb.exists())
        with Image.open(thumb) as im:
            self.assertEqual(max(im.size), sync_pipeline.THUMB_LONG_EDGE)

    def test_thumbnail_is_much_smaller_than_the_original(self):
        _write_image(self.folder, "img_002.jpg", (2000, 1500))
        original = (self.folder / "img_002.jpg").stat().st_size
        name = sync_pipeline.make_thumbnail(self.folder, "img_002.jpg")
        self.assertLess((self.folder / name).stat().st_size, original)

    def test_small_image_is_skipped(self):
        # Already tiny: serving the original costs less than a second copy.
        _write_image(self.folder, "img_003.jpg", (320, 240))
        self.assertIsNone(sync_pipeline.make_thumbnail(self.folder, "img_003.jpg"))
        self.assertFalse((self.folder / "img_003_t.jpg").exists())

    def test_existing_thumbnail_is_reused(self):
        _write_image(self.folder, "img_004.jpg", (1600, 1200))
        first = sync_pipeline.make_thumbnail(self.folder, "img_004.jpg")
        before = (self.folder / first).read_bytes()
        second = sync_pipeline.make_thumbnail(self.folder, "img_004.jpg")
        self.assertEqual(first, second)
        self.assertEqual((self.folder / second).read_bytes(), before)

    def test_missing_source_returns_none(self):
        self.assertIsNone(sync_pipeline.make_thumbnail(self.folder, "nope.jpg"))

    def test_corrupt_file_never_raises(self):
        (self.folder / "img_005.jpg").write_bytes(b"not an image at all")
        self.assertIsNone(sync_pipeline.make_thumbnail(self.folder, "img_005.jpg"))


class TestThumbnailNames(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def test_index_aligned_with_none_for_missing(self):
        _write_image(self.folder, "img_001.jpg", (1600, 1200))
        sync_pipeline.make_thumbnail(self.folder, "img_001.jpg")
        names = sync_pipeline.thumbnail_names(
            self.folder, ["img_001.jpg", "img_002.jpg"]
        )
        self.assertEqual(names, ["img_001_t.jpg", None])

    def test_empty_and_none_inputs(self):
        self.assertEqual(sync_pipeline.thumbnail_names(self.folder, []), [])
        self.assertEqual(sync_pipeline.thumbnail_names(self.folder, None), [])

    def test_handles_extensions(self):
        _write_image(self.folder, "img_007.png", (1200, 900))
        sync_pipeline.make_thumbnail(self.folder, "img_007.png")
        self.assertEqual(
            sync_pipeline.thumbnail_names(self.folder, ["img_007.png"]),
            ["img_007_t.jpg"],
        )


if __name__ == "__main__":
    unittest.main()
