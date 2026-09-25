"""Tests for the duplicate-picture check that explains "the sync looks
wrong" when the same file is uploaded twice (job 0d3be1a34c3f had four
byte-identical images, so the first 19.5s could not cut at all).

Run:  python -m unittest test_sync_images -v
"""

import tempfile
import unittest
from pathlib import Path

from sync_pipeline import (
    duplicate_image_warning,
    find_duplicate_images,
    manifest_image_warning,
    save_manifest,
    sync_image_warning,
)

# One tiny valid JPEG header is enough: the check hashes bytes, it never
# decodes the picture.
BYTES_A = b"\xff\xd8\xff\xe0" + b"picture-A" * 4
BYTES_B = b"\xff\xd8\xff\xe0" + b"picture-B" * 4


def write(folder, name, data):
    (folder / name).write_bytes(data)
    return name


class TestFindDuplicates(unittest.TestCase):

    def test_identical_files_are_grouped(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = [
                write(folder, "img_001.jpeg", BYTES_A),
                write(folder, "img_002.jpeg", BYTES_A),
                write(folder, "img_003.jpeg", BYTES_A),
                write(folder, "img_004.jpeg", BYTES_A),
                write(folder, "img_005.jpg", BYTES_B),
            ]
            groups = find_duplicate_images(folder, names)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0], ["img_001.jpeg", "img_002.jpeg",
                                         "img_003.jpeg", "img_004.jpeg"])

    def test_all_different_images_have_no_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = [write(folder, f"img_{i:03d}.jpeg", BYTES_A + bytes([i]))
                     for i in range(1, 11)]
            self.assertEqual(find_duplicate_images(folder, names), [])

    def test_a_missing_file_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = [write(folder, "img_001.jpeg", BYTES_A), "img_999.jpeg"]
            self.assertEqual(find_duplicate_images(folder, names), [])


class TestWarningText(unittest.TestCase):

    def test_no_duplicates_is_silent(self):
        self.assertIsNone(duplicate_image_warning([], 10))

    def test_names_the_repeated_images(self):
        msg = duplicate_image_warning(
            [["img_001.jpeg", "img_002.jpeg"]], 10)
        self.assertIsNotNone(msg)
        self.assertIn("1 of your 10 images", msg)
        self.assertIn("Image 1 is also Image 2", msg)
        # The point the creator needs to hear: it cannot cut, it holds.
        self.assertIn("cannot", msg)

    def test_extra_count_and_overflow_are_stated(self):
        groups = [["img_001.jpeg", "img_002.jpeg"],
                  ["img_003.jpeg", "img_004.jpeg", "img_005.jpeg"],
                  ["img_006.jpeg", "img_007.jpeg"],
                  ["img_008.jpeg", "img_009.jpeg"]]
        msg = duplicate_image_warning(groups, 9)
        self.assertIn("5 of your 9 images", msg)   # 1 + 2 + 1 + 1 extras
        self.assertIn("1 more", msg)               # only 3 groups listed

    def test_unknown_filename_falls_back_to_the_raw_name(self):
        msg = duplicate_image_warning([["beat-one.png", "beat-two.png"]], 2)
        self.assertIn("beat-one.png is also beat-two.png", msg)


class TestManifestWarning(unittest.TestCase):

    def test_stored_value_wins_without_touching_disk(self):
        manifest = {"image_warning": "stored note"}
        self.assertEqual(
            manifest_image_warning(manifest, "no-such-job"), "stored note")

    def test_old_manifest_is_hashed_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_id = Path(tmp).name
            uploads = Path(__file__).resolve().parent / "static" / "uploads"
            folder = uploads / job_id
            folder.mkdir(parents=True, exist_ok=True)
            try:
                write(folder, "img_001.jpeg", BYTES_A)
                write(folder, "img_002.jpeg", BYTES_A)
                save_manifest(job_id, {"images": ["img_001.jpeg",
                                                  "img_002.jpeg"]})
                computed = sync_image_warning(job_id)
                self.assertIsNotNone(computed)
                self.assertIn("Image 1 is also Image 2", computed)
                # A manifest without the field behaves the same way.
                self.assertEqual(
                    manifest_image_warning({"images": []}, job_id), computed)
                self.assertIsNone(
                    manifest_image_warning({"image_warning": None}, job_id))
            finally:
                import shutil
                shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
