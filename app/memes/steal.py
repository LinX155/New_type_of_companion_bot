import base64
import json
import mimetypes
import os
import re
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field

from ..llm.client import LLMClient
from ..llm.prompts import build_meme_steal_analysis_messages
from .catalog import MemeCatalog


SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class MemeStealAnalysis(BaseModel):
    should_steal: bool = False
    category: Optional[str] = None
    save_name: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    reason: str = ""
    safety: str = "unclear"
    raw_output: str = ""
    image_ref: str = ""
    normalized: bool = True
    warnings: list[str] = Field(default_factory=list)


class MemeStealAnalyzer:
    """分析用户发来的图片是否适合偷表情，并生成语义化入库文件名。

    当前模块只做无副作用分析，不保存文件。未来 OneBot 接入层下载 image/mface
    到本地临时文件后，可直接复用 analyze() 的命名结果进入入库工具。
    """

    def __init__(self, catalog: MemeCatalog, root_dir: str):
        self.catalog = catalog
        self.root_dir = os.path.abspath(root_dir)
        self.allowed_local_roots = [
            os.path.abspath(self.catalog.assets_dir),
            os.path.abspath(os.path.join(self.catalog.base_dir, "inbox")),
            os.path.abspath(os.path.join(self.catalog.base_dir, "tmp")),
            os.path.abspath(os.path.join(self.root_dir, "data", "onebot_media")),
        ]
        for path in self.allowed_local_roots[1:]:
            os.makedirs(path, exist_ok=True)

    async def analyze(
        self,
        image_ref: str,
        llm_client: LLMClient,
        context_text: str = "",
    ) -> MemeStealAnalysis:
        if not image_ref or not image_ref.strip():
            raise ValueError("image_ref is required")

        image_url = self._image_ref_to_model_url(image_ref.strip())
        messages = build_meme_steal_analysis_messages(
            image_url=image_url,
            categories_text=self._categories_text(),
            context_text=context_text,
        )
        raw_output = await llm_client.chat_completion(
            messages=messages,
            temperature=0.2,
        )
        parsed = self._parse_json(raw_output)
        return self._normalize(parsed, raw_output, image_ref.strip())

    def _categories_text(self) -> str:
        categories = self.catalog.get_categories()
        lines = []
        for cat_id in self.catalog.get_category_display_order():
            info = categories.get(cat_id, {})
            name = info.get("name", cat_id)
            description = info.get("description", "")
            lines.append(f"- {cat_id}: {name}；{description}")
        return "\n".join(lines)

    def _image_ref_to_model_url(self, image_ref: str) -> str:
        if image_ref.startswith("data:image/"):
            return image_ref

        if re.match(r"^[A-Za-z]:[\\/]", image_ref):
            parsed = urlparse("")
        else:
            parsed = urlparse(image_ref)

        if parsed.scheme in ("http", "https"):
            return image_ref

        path = self._local_path_from_ref(image_ref, parsed)
        if not self._is_allowed_local_path(path):
            raise ValueError(
                "local image_ref must be under an allowed meme media directory: "
                "memes/assets, memes/inbox, memes/tmp, or data/onebot_media"
            )
        if not os.path.isfile(path):
            raise ValueError(f"image file not found: {image_ref}")

        ext = os.path.splitext(path)[1].lower()
        if ext not in SUPPORTED_IMAGE_EXTS:
            raise ValueError(f"unsupported image extension: {ext or '(none)'}")

        size = os.path.getsize(path)
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"image is too large: {size} bytes")

        with open(path, "rb") as f:
            image_bytes = f.read()

        mime = self._detect_mime(image_bytes, ext)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _local_path_from_ref(self, image_ref: str, parsed) -> str:
        if parsed.scheme == "file":
            path = unquote(parsed.path or "")
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
                path = path[1:]
            return os.path.abspath(path)

        if parsed.scheme:
            raise ValueError(f"unsupported image_ref scheme: {parsed.scheme}")

        path = image_ref
        if not os.path.isabs(path):
            path = os.path.join(self.root_dir, path)
        return os.path.abspath(path)

    def _is_allowed_local_path(self, path: str) -> bool:
        candidate = os.path.normcase(os.path.abspath(path))
        for root in self.allowed_local_roots:
            allowed = os.path.normcase(os.path.abspath(root))
            try:
                if os.path.commonpath([candidate, allowed]) == allowed:
                    return True
            except ValueError:
                continue
        return False

    def _detect_mime(self, image_bytes: bytes, ext: str) -> str:
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return "image/webp"
        if image_bytes.startswith(b"BM"):
            return "image/bmp"

        guessed = mimetypes.guess_type(f"file{ext}")[0]
        if guessed and guessed.startswith("image/"):
            return guessed
        raise ValueError("file header is not a supported image")

    def _parse_json(self, raw_output: str) -> dict[str, Any]:
        text = (raw_output or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                raise ValueError("LLM did not return a JSON object")
            data = json.loads(match.group(0))

        if not isinstance(data, dict):
            raise ValueError("LLM JSON result must be an object")
        return data

    def _normalize(
        self,
        data: dict[str, Any],
        raw_output: str,
        image_ref: str,
    ) -> MemeStealAnalysis:
        warnings: list[str] = []
        categories = set(self.catalog.get_categories().keys())

        should_steal = bool(data.get("should_steal"))
        category = self._clean_optional_text(data.get("category"))
        save_name = self._clean_optional_text(data.get("save_name"))
        safety = self._clean_optional_text(data.get("safety")) or "unclear"
        reason = self._clean_optional_text(data.get("reason")) or ""
        keywords = self._normalize_keywords(data.get("keywords"))

        if not should_steal:
            return MemeStealAnalysis(
                should_steal=False,
                category=None,
                save_name=None,
                keywords=keywords,
                reason=reason,
                safety=safety,
                raw_output=raw_output,
                image_ref=image_ref,
                warnings=warnings,
            )

        if category not in categories:
            warnings.append(f"invalid category: {category}")
            category = "miscellaneous" if "miscellaneous" in categories else None

        if not category:
            should_steal = False
            save_name = None
            warnings.append("missing valid category")
        else:
            save_name = self._normalize_save_name(save_name, category, keywords)
            if not save_name:
                should_steal = False
                warnings.append("missing valid save_name")

        return MemeStealAnalysis(
            should_steal=should_steal,
            category=category if should_steal else None,
            save_name=save_name if should_steal else None,
            keywords=keywords,
            reason=reason,
            safety=safety,
            raw_output=raw_output,
            image_ref=image_ref,
            warnings=warnings,
        )

    def _normalize_save_name(
        self,
        value: Optional[str],
        category: str,
        keywords: list[str],
    ) -> Optional[str]:
        text = (value or "").lower()
        text = os.path.splitext(text)[0]
        text = re.sub(r"[^a-z0-9_]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")

        empty_tokens = {"generic", "random", "image", "sticker", "meme"}
        tokens = [token for token in text.split("_") if token and token not in empty_tokens]

        if not tokens:
            tokens = [category, *keywords[:4]]
        elif tokens[0] != category:
            tokens = [category, *tokens]

        tokens = [token for token in tokens if re.fullmatch(r"[a-z0-9]+", token)]
        if len(tokens) < 3:
            tokens.extend(token for token in keywords if token not in tokens)

        tokens = tokens[:6]
        if len(tokens) < 3:
            return None
        return "_".join(tokens)

    def _normalize_keywords(self, value) -> list[str]:
        if isinstance(value, str):
            raw_items = re.split(r"[,，\s]+", value)
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = []

        keywords: list[str] = []
        for item in raw_items:
            token = str(item).lower().strip()
            token = re.sub(r"[^a-z0-9]+", "_", token)
            token = re.sub(r"_+", "_", token).strip("_")
            if token and token not in keywords:
                keywords.append(token)
        return keywords[:8]

    def _clean_optional_text(self, value) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
