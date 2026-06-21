import os
import json
from typing import Dict, List

from PIL import Image


SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


class MemeCatalog:
    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir or os.path.join(os.path.dirname(__file__), "..", "..", "memes")
        self.assets_dir = os.path.join(self.base_dir, "assets")
        self.data_path = os.path.join(self.base_dir, "memes_data.json")
        self.dhash_path = os.path.join(self.base_dir, "image_dhash_index.json")
        self.filtered_path = os.path.join(self.base_dir, "filtered.json")
        self._data: Dict = {}
        self._dhash_sync_signature = None
        self._load()
        self.sync_dhash_index()

    def _load(self):
        if os.path.exists(self.data_path):
            with open(self.data_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {}

    def get_categories(self) -> Dict[str, Dict]:
        """返回所有分类信息"""
        return self._data

    def get_category(self, category_id: str) -> Dict:
        return self._data.get(category_id, {})

    def get_images_in_category(self, category_id: str) -> List[str]:
        """返回某分类下所有图片的 file_stem 列表"""
        self._sync_dhash_index_if_needed()
        return self._get_images_in_category(category_id)

    def _get_images_in_category(self, category_id: str) -> List[str]:
        cat_dir = os.path.join(self.assets_dir, category_id)
        if not os.path.isdir(cat_dir):
            return []
        stems = []
        for fname in sorted(os.listdir(cat_dir)):
            if self._is_supported_image_file(fname):
                stem = os.path.splitext(fname)[0]
                stems.append(stem)
        return stems

    def get_image_path(self, category_id: str, file_stem: str) -> str:
        """返回图片完整路径"""
        cat_dir = os.path.join(self.assets_dir, category_id)
        if not os.path.isdir(cat_dir):
            return ""
        for fname in os.listdir(cat_dir):
            if os.path.splitext(fname)[0] == file_stem:
                return os.path.join(cat_dir, fname)
        return ""

    def get_all_images(self) -> Dict[str, List[str]]:
        """返回所有分类下的图片"""
        self._sync_dhash_index_if_needed()
        result = {}
        for cat_id in self._data.keys():
            result[cat_id] = self._get_images_in_category(cat_id)
        return result

    def get_stats(self) -> Dict:
        """返回统计信息"""
        self._sync_dhash_index_if_needed()
        total = 0
        cat_counts = {}
        for cat_id in self._data.keys():
            count = len(self._get_images_in_category(cat_id))
            cat_counts[cat_id] = count
            total += count
        return {
            "total": total,
            "categories": cat_counts,
            "data_dir": self.base_dir,
        }

    def delete_image(self, category_id: str, file_stem: str) -> bool:
        """删除图片"""
        cat_dir = os.path.join(self.assets_dir, category_id)
        if not os.path.isdir(cat_dir):
            return False
        for fname in os.listdir(cat_dir):
            if os.path.splitext(fname)[0] == file_stem:
                image_path = os.path.join(cat_dir, fname)
                os.remove(image_path)
                self._remove_dhash_index_entry(image_path)
                return True
        return False

    def _remove_dhash_index_entry(self, image_path: str) -> None:
        index = self._read_dhash_index()
        if not index:
            self._dhash_sync_signature = self._current_assets_signature()
            return

        target_keys = self._dhash_index_key_candidates(image_path)
        updated = {
            key: value
            for key, value in index.items()
            if self._normalize_index_key(key) not in target_keys
        }
        if len(updated) == len(index):
            return

        self._write_dhash_index(updated)
        self._dhash_sync_signature = self._current_assets_signature()

    def sync_dhash_index(self) -> Dict[str, int]:
        """让 dHash index 与 memes/assets 的真实文件保持一致。"""
        existing = self._read_dhash_index()
        updated: Dict[str, str] = {}
        skipped = 0

        for image_path in self._iter_image_paths():
            key = self._dhash_index_key(image_path)
            try:
                updated[key] = self._compute_dhash(image_path)
            except OSError:
                fallback = self._existing_dhash_for_path(existing, image_path)
                if fallback:
                    updated[key] = fallback
                else:
                    skipped += 1

        if updated != existing:
            self._write_dhash_index(updated)

        self._dhash_sync_signature = self._current_assets_signature()
        return {
            "entries": len(updated),
            "removed": max(len(existing) - len(updated), 0),
            "added_or_changed": sum(1 for key, value in updated.items() if existing.get(key) != value),
            "skipped": skipped,
        }

    def _sync_dhash_index_if_needed(self) -> None:
        signature = self._current_assets_signature()
        if signature != self._dhash_sync_signature:
            self.sync_dhash_index()

    def _read_dhash_index(self) -> Dict[str, str]:
        if not os.path.exists(self.dhash_path):
            return {}
        try:
            with open(self.dhash_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in data.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _write_dhash_index(self, index: Dict[str, str]) -> None:
        os.makedirs(os.path.dirname(self.dhash_path), exist_ok=True)
        tmp_path = f"{self.dhash_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, self.dhash_path)

    def _iter_image_paths(self) -> List[str]:
        if not os.path.isdir(self.assets_dir):
            return []

        paths = []
        for root, dirs, files in os.walk(self.assets_dir):
            dirs.sort()
            for fname in sorted(files):
                if self._is_supported_image_file(fname):
                    paths.append(os.path.join(root, fname))
        return paths

    def _current_assets_signature(self) -> tuple:
        signature = []
        for image_path in self._iter_image_paths():
            try:
                stat = os.stat(image_path)
            except OSError:
                continue
            signature.append((self._dhash_index_key(image_path), stat.st_size, stat.st_mtime_ns))
        return tuple(signature)

    def _compute_dhash(self, image_path: str, hash_size: int = 8) -> str:
        with Image.open(image_path) as image:
            if getattr(image, "is_animated", False):
                image.seek(0)
            image = image.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
            pixels = list(image.getdata())

        value = 0
        for y in range(hash_size):
            row_start = y * (hash_size + 1)
            row = pixels[row_start:row_start + hash_size + 1]
            for x in range(hash_size):
                value = (value << 1) | int(row[x] > row[x + 1])
        return f"{value:016x}"

    def _existing_dhash_for_path(self, index: Dict[str, str], image_path: str) -> str:
        normalized_index = {
            self._normalize_index_key(key): value
            for key, value in index.items()
        }
        for key in self._dhash_index_key_candidates(image_path):
            if key in normalized_index:
                return normalized_index[key]
        return ""

    def _dhash_index_key(self, image_path: str) -> str:
        rel_to_base = os.path.relpath(os.path.normpath(image_path), self.base_dir)
        base_name = os.path.basename(os.path.normpath(self.base_dir))
        if base_name:
            return self._normalize_index_key(os.path.join(base_name, rel_to_base))
        return self._normalize_index_key(rel_to_base)

    def _dhash_index_key_candidates(self, image_path: str) -> set[str]:
        normalized = os.path.normpath(image_path)
        candidates = {
            self._normalize_index_key(normalized),
            self._normalize_index_key(os.path.abspath(normalized)),
        }

        try:
            rel_to_base = os.path.relpath(normalized, self.base_dir)
        except ValueError:
            rel_to_base = ""
        if rel_to_base and not rel_to_base.startswith(".."):
            rel_to_base = self._normalize_index_key(rel_to_base)
            candidates.add(rel_to_base)
            base_name = os.path.basename(os.path.normpath(self.base_dir))
            if base_name:
                candidates.add(self._normalize_index_key(os.path.join(base_name, rel_to_base)))

        try:
            rel_to_cwd = os.path.relpath(normalized, os.getcwd())
        except ValueError:
            rel_to_cwd = ""
        if rel_to_cwd and not rel_to_cwd.startswith(".."):
            candidates.add(self._normalize_index_key(rel_to_cwd))

        return candidates

    def _normalize_index_key(self, key: str) -> str:
        return key.replace("\\", "/").lstrip("./")

    def _is_supported_image_file(self, filename: str) -> bool:
        return filename.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS)

    def get_category_display_order(self) -> List[str]:
        """返回分类展示顺序，miscellaneous 置后"""
        cats = [k for k in self._data.keys() if k != "miscellaneous"]
        if "miscellaneous" in self._data:
            cats.append("miscellaneous")
        return cats
