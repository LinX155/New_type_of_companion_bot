import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.memes.catalog import MemeCatalog


class MemeCatalogTest(unittest.TestCase):
    def test_delete_image_removes_dhash_index_entries(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir, category_dir = self._make_catalog_dir(tmp_dir)
            target = self._write_image(category_dir / "amused_test.jpg", (250, 10, 10))
            keep = self._write_image(category_dir / "amused_keep.jpg", (10, 250, 10))
            index_path = base_dir / "image_dhash_index.json"
            index_path.write_text(
                json.dumps(
                    {
                        "memes/assets/amused/amused_test.jpg": "1111",
                        "assets/amused/amused_test.jpg": "2222",
                        "memes/assets/amused/amused_keep.jpg": "3333",
                    }
                ),
                encoding="utf-8",
            )

            catalog = MemeCatalog(str(base_dir))

            self.assertTrue(catalog.delete_image("amused", "amused_test"))
            self.assertFalse(target.exists())
            self.assertEqual(
                json.loads(index_path.read_text(encoding="utf-8")),
                {"memes/assets/amused/amused_keep.jpg": catalog._compute_dhash(str(keep))},
            )

    def test_sync_dhash_index_removes_files_deleted_outside_webui(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir, category_dir = self._make_catalog_dir(tmp_dir)
            target = self._write_image(category_dir / "amused_test.jpg", (250, 10, 10))
            keep = self._write_image(category_dir / "amused_keep.jpg", (10, 250, 10))

            catalog = MemeCatalog(str(base_dir))
            target.unlink()

            self.assertEqual(catalog.get_stats()["total"], 1)
            self.assertEqual(
                json.loads((base_dir / "image_dhash_index.json").read_text(encoding="utf-8")),
                {"memes/assets/amused/amused_keep.jpg": catalog._compute_dhash(str(keep))},
            )

    def test_sync_dhash_index_adds_files_created_outside_webui(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir, category_dir = self._make_catalog_dir(tmp_dir)
            catalog = MemeCatalog(str(base_dir))

            added = self._write_image(category_dir / "amused_added.jpg", (10, 10, 250))

            self.assertEqual(catalog.get_images_in_category("amused"), ["amused_added"])
            index = json.loads((base_dir / "image_dhash_index.json").read_text(encoding="utf-8"))
            self.assertEqual(
                index,
                {"memes/assets/amused/amused_added.jpg": catalog._compute_dhash(str(added))},
            )

    def test_sync_dhash_index_recalculates_replaced_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir, category_dir = self._make_catalog_dir(tmp_dir)
            target = self._write_image(category_dir / "amused_test.jpg", (250, 10, 10))
            catalog = MemeCatalog(str(base_dir))
            old_index = json.loads((base_dir / "image_dhash_index.json").read_text(encoding="utf-8"))

            self._write_image(target, (10, 10, 250), reverse=True)
            catalog.get_images_in_category("amused")
            new_index = json.loads((base_dir / "image_dhash_index.json").read_text(encoding="utf-8"))

            self.assertNotEqual(old_index, new_index)
            self.assertEqual(new_index["memes/assets/amused/amused_test.jpg"], catalog._compute_dhash(str(target)))

    def _make_catalog_dir(self, tmp_dir: str) -> tuple[Path, Path]:
        base_dir = Path(tmp_dir) / "memes"
        category_dir = base_dir / "assets" / "amused"
        category_dir.mkdir(parents=True)
        (base_dir / "memes_data.json").write_text(
            json.dumps({"amused": {"name": "amused"}}),
            encoding="utf-8",
        )
        return base_dir, category_dir

    def _write_image(self, path: Path, color: tuple[int, int, int], reverse: bool = False) -> Path:
        image = Image.new("RGB", (12, 12), color)
        for x in range(12):
            shade = 255 - x * 20 if reverse else x * 20
            for y in range(12):
                image.putpixel((x, y), (shade, shade, shade))
        image.save(path)
        return path


if __name__ == "__main__":
    unittest.main()
