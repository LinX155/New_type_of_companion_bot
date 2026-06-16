import os
import json
from typing import Dict, List


class MemeCatalog:
    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir or os.path.join(os.path.dirname(__file__), "..", "..", "memes")
        self.assets_dir = os.path.join(self.base_dir, "assets")
        self.data_path = os.path.join(self.base_dir, "memes_data.json")
        self.dhash_path = os.path.join(self.base_dir, "image_dhash_index.json")
        self.filtered_path = os.path.join(self.base_dir, "filtered.json")
        self._data: Dict = {}
        self._load()

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
        cat_dir = os.path.join(self.assets_dir, category_id)
        if not os.path.isdir(cat_dir):
            return []
        stems = []
        for fname in os.listdir(cat_dir):
            if fname.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")):
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
        result = {}
        for cat_id in self._data.keys():
            result[cat_id] = self.get_images_in_category(cat_id)
        return result

    def get_stats(self) -> Dict:
        """返回统计信息"""
        total = 0
        cat_counts = {}
        for cat_id in self._data.keys():
            count = len(self.get_images_in_category(cat_id))
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
                os.remove(os.path.join(cat_dir, fname))
                return True
        return False

    def get_category_display_order(self) -> List[str]:
        """返回分类展示顺序，miscellaneous 置后"""
        cats = [k for k in self._data.keys() if k != "miscellaneous"]
        if "miscellaneous" in self._data:
            cats.append("miscellaneous")
        return cats
