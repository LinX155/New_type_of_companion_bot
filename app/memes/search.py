import re
from typing import List
from .catalog import MemeCatalog


class MemeSearch:
    def __init__(self, catalog: MemeCatalog):
        self.catalog = catalog

    def search(self, category: str, keywords: str, top_k: int = 5) -> List[str]:
        """
        在指定分类下按关键词检索表情
        匹配规则：
        1. 文件名按 _、-、空格切分 token，统一小写
        2. keywords 同样切分 token，统一小写
        3. exact token 命中优先
        4. substring 命中次之
        5. 命中数量更多的候选优先
        """
        if top_k > 8:
            top_k = 8

        images = self.catalog.get_images_in_category(category)
        if not images:
            return []

        keyword_tokens = self._tokenize(keywords)
        if not keyword_tokens:
            # 无关键词时返回该分类下少量候选
            return images[:top_k]

        scored = []
        for stem in images:
            stem_tokens = self._tokenize(stem)
            score = self._score(stem_tokens, keyword_tokens)
            scored.append((stem, score))

        # 按分数降序，同分时按文件名稳定排序
        scored.sort(key=lambda x: (-x[1], x[0]))

        # 返回 top_k
        return [s[0] for s in scored[:top_k] if s[1] > 0] or images[:top_k]

    def _tokenize(self, text: str) -> List[str]:
        """切分 token 并统一小写"""
        text = text.lower().strip()
        tokens = re.split(r'[_\-\s]+', text)
        return [t for t in tokens if t]

    def _score(self, stem_tokens: List[str], keyword_tokens: List[str]) -> int:
        """计算匹配分数"""
        score = 0
        for kw in keyword_tokens:
            for st in stem_tokens:
                if kw == st:
                    score += 10  # exact match
                elif kw in st or st in kw:
                    score += 5   # substring match
        return score
