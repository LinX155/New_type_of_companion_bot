import os
from typing import Optional
from .catalog import MemeCatalog


class MemeRenderer:
    def __init__(self, catalog: MemeCatalog):
        self.catalog = catalog

    def parse_react_text(self, text: str) -> dict:
        """解析 REACT text，返回类型和值"""
        if text.startswith("emoji:"):
            return {"type": "emoji", "value": text[6:]}
        if text.startswith("search_meme:"):
            parts = text[len("search_meme:"):].split(":", 1)
            category = parts[0] if parts else ""
            keywords = parts[1] if len(parts) > 1 else ""
            return {"type": "search_meme", "category": category, "keywords": keywords}
        if text.startswith("meme:"):
            return {"type": "meme", "file_stem": text[5:]}
        # 默认认为是纯 emoji 或文本
        return {"type": "text", "value": text}

    def render_meme(self, file_stem: str) -> Optional[str]:
        """
        根据 file_stem 查找图片路径
        由于 file_stem 不包含分类信息，需要在所有分类中查找
        """
        # 尝试从文件名前缀推断分类
        possible_category = file_stem.split("_")[0] if "_" in file_stem else ""
        if possible_category:
            path = self.catalog.get_image_path(possible_category, file_stem)
            if path and os.path.exists(path):
                return path

        # 如果没找到，遍历所有分类
        for cat_id in self.catalog.get_categories().keys():
            path = self.catalog.get_image_path(cat_id, file_stem)
            if path and os.path.exists(path):
                return path

        return None

    def get_meme_relative_path(self, file_stem: str) -> Optional[str]:
        """获取表情包相对于 memes/assets 的路径"""
        full_path = self.render_meme(file_stem)
        if not full_path:
            return None
        assets_dir = self.catalog.assets_dir
        try:
            rel = os.path.relpath(full_path, assets_dir)
            return rel.replace("\\", "/")
        except ValueError:
            return None
