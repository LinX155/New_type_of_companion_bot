import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import uuid
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


class MemeStealSaveResult(BaseModel):
    status: str = "skipped"
    saved: bool = False
    duplicate: bool = False
    category: Optional[str] = None
    file_stem: Optional[str] = None
    file_path: Optional[str] = None
    matched_file: Optional[str] = None
    distance: Optional[int] = None
    reason: str = ""
    error: Optional[str] = None
    analysis: Optional[MemeStealAnalysis] = None

    def to_internal_payload(self) -> dict:
        return {
            "status": self.status,
            "saved": self.saved,
            "duplicate": self.duplicate,
            "category": self.category,
            "file_stem": self.file_stem,
            "file_path": self.file_path,
            "matched_file": self.matched_file,
            "distance": self.distance,
            "reason": self.reason,
            "error": self.error,
            "analysis": self.analysis.model_dump(mode="json") if self.analysis else None,
        }


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

    def to_model_image_url(self, image_ref: str) -> str:
        return self._image_ref_to_model_url(image_ref)

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


class MemeStealSaver:
    """Save analyzed sticker images into the formal meme asset library."""

    def __init__(
        self,
        catalog: MemeCatalog,
        root_dir: str,
        duplicate_threshold: int = 8,
    ):
        self.catalog = catalog
        self.root_dir = os.path.abspath(root_dir)
        self.duplicate_threshold = duplicate_threshold
        self._write_lock = asyncio.Lock()
        self.allowed_local_roots = [
            os.path.abspath(self.catalog.assets_dir),
            os.path.abspath(os.path.join(self.catalog.base_dir, "inbox")),
            os.path.abspath(os.path.join(self.catalog.base_dir, "tmp")),
            os.path.abspath(os.path.join(self.root_dir, "data", "onebot_media")),
        ]
        for path in self.allowed_local_roots[1:]:
            os.makedirs(path, exist_ok=True)

    async def save_from_analysis(
        self,
        image_ref: str,
        analysis: MemeStealAnalysis,
    ) -> MemeStealSaveResult:
        if not analysis.should_steal:
            return MemeStealSaveResult(
                status="skipped",
                saved=False,
                category=analysis.category,
                reason=analysis.reason or "analysis marked should_steal=false",
                analysis=analysis,
            )
        if not analysis.category or not analysis.save_name:
            return MemeStealSaveResult(
                status="failed",
                category=analysis.category,
                reason=analysis.reason,
                error="analysis missing category or save_name",
                analysis=analysis,
            )

        async with self._write_lock:
            return await asyncio.to_thread(self._save_from_analysis_sync, image_ref, analysis)

    def _save_from_analysis_sync(
        self,
        image_ref: str,
        analysis: MemeStealAnalysis,
    ) -> MemeStealSaveResult:
        try:
            source_path = self._local_path_from_ref(image_ref)
            self._validate_source(source_path)
            source_file = os.fspath(source_path)
            with open(source_file, "rb") as f:
                image_bytes = f.read()
            ext = self._detect_extension(image_bytes, os.path.splitext(source_file)[1])
        except Exception as exc:  # noqa: BLE001
            return MemeStealSaveResult(
                status="failed",
                category=analysis.category,
                reason=analysis.reason,
                error=str(exc),
                analysis=analysis,
            )

        try:
            self.catalog.sync_dhash_index()
            duplicate = self._find_duplicate(source_path)
            if duplicate:
                matched_path, distance = duplicate
                return MemeStealSaveResult(
                    status="duplicate",
                    saved=False,
                    duplicate=True,
                    category=analysis.category,
                    file_stem=os.path.splitext(os.path.basename(matched_path))[0],
                    matched_file=matched_path,
                    distance=distance,
                    reason="这个表情包已经在本地库里有近似重复",
                    analysis=analysis,
                )

            target_dir = os.path.join(self.catalog.assets_dir, analysis.category)
            os.makedirs(target_dir, exist_ok=True)
            target_path = self._unique_target_path(target_dir, analysis.save_name, ext, image_bytes)
            shutil.copyfile(source_path, target_path)
            self.catalog.sync_dhash_index()
            return MemeStealSaveResult(
                status="saved",
                saved=True,
                duplicate=False,
                category=analysis.category,
                file_stem=os.path.splitext(os.path.basename(target_path))[0],
                file_path=target_path,
                reason=analysis.reason,
                analysis=analysis,
            )
        except Exception as exc:  # noqa: BLE001
            return MemeStealSaveResult(
                status="failed",
                saved=False,
                category=analysis.category,
                reason=analysis.reason,
                error=str(exc),
                analysis=analysis,
            )

    def _local_path_from_ref(self, image_ref: str) -> str:
        text = (image_ref or "").strip()
        if not text:
            raise ValueError("image_ref is required")
        parsed = urlparse(text) if not re.match(r"^[A-Za-z]:[\\/]", text) else urlparse("")
        if parsed.scheme == "file":
            path = unquote(parsed.path or "")
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
                path = path[1:]
        elif parsed.scheme:
            raise ValueError(f"unsupported image_ref scheme for saving: {parsed.scheme}")
        else:
            path = text
        if not os.path.isabs(path):
            path = os.path.join(self.root_dir, path)
        return os.path.abspath(path)

    def _validate_source(self, path: str) -> None:
        if not self._is_allowed_local_path(path):
            raise ValueError(
                "local image_ref must be under an allowed meme media directory: "
                "memes/assets, memes/inbox, memes/tmp, or data/onebot_media"
            )
        if not os.path.isfile(path):
            raise ValueError(f"image file not found: {path}")
        size = os.path.getsize(path)
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"image is too large: {size} bytes")
        if os.path.splitext(path)[1].lower() not in SUPPORTED_IMAGE_EXTS:
            raise ValueError(f"unsupported image extension: {os.path.splitext(path)[1] or '(none)'}")

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

    def _detect_extension(self, image_bytes: bytes, fallback_ext: str) -> str:
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return ".webp"
        if image_bytes.startswith(b"BM"):
            return ".bmp"
        fallback = fallback_ext.lower()
        if fallback in SUPPORTED_IMAGE_EXTS:
            return fallback
        raise ValueError("file header is not a supported image")

    def _find_duplicate(self, source_path: str) -> Optional[tuple[str, int]]:
        try:
            source_hash = self.catalog._compute_dhash(source_path)
        except OSError:
            return None
        index = self.catalog._read_dhash_index()
        best: Optional[tuple[str, int]] = None
        for key, hash_value in index.items():
            try:
                distance = _hamming_hex(source_hash, hash_value)
            except ValueError:
                continue
            if distance > self.duplicate_threshold:
                continue
            matched_path = self._path_from_index_key(key)
            if not matched_path:
                continue
            if best is None or distance < best[1]:
                best = (matched_path, distance)
        return best

    def _path_from_index_key(self, key: str) -> str:
        normalized = key.replace("\\", "/").lstrip("./")
        base_name = os.path.basename(os.path.normpath(self.catalog.base_dir))
        if base_name and normalized.startswith(f"{base_name}/"):
            rel = normalized[len(base_name) + 1:]
            candidate = os.path.join(self.catalog.base_dir, *rel.split("/"))
        else:
            candidate = os.path.join(self.catalog.base_dir, *normalized.split("/"))
        return candidate if os.path.isfile(candidate) else ""

    def _unique_target_path(self, target_dir: str, save_name: str, ext: str, image_bytes: bytes) -> str:
        stem = self._safe_stem(save_name)
        target_path = os.path.join(target_dir, f"{stem}{ext}")
        if not os.path.exists(target_path):
            return target_path

        digest = hashlib.sha256(image_bytes).hexdigest()[:8]
        target_path = os.path.join(target_dir, f"{stem}_{digest}{ext}")
        if not os.path.exists(target_path):
            return target_path

        return os.path.join(target_dir, f"{stem}_{digest}_{uuid.uuid4().hex[:6]}{ext}")

    def _safe_stem(self, value: str) -> str:
        text = os.path.splitext(value or "")[0].lower()
        text = re.sub(r"[^a-z0-9_]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")
        if not text:
            raise ValueError("save_name is empty after normalization")
        return text


def _hamming_hex(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()
