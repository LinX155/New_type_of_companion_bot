import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx

from .client import OneBotConnectionManager


SUPPORTED_MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
}
MAX_MEDIA_BYTES = 12 * 1024 * 1024


@dataclass
class MediaDownloadResult:
    status: str
    media_ref: dict
    local_path: Optional[str] = None
    sha256: Optional[str] = None
    source_field: Optional[str] = None
    content_type: Optional[str] = None
    bytes: int = 0
    error: Optional[str] = None
    attempts: list[dict] = field(default_factory=list)

    def to_debug_payload(self) -> dict:
        return {
            "status": self.status,
            "local_path": self.local_path,
            "sha256": self.sha256,
            "source_field": self.source_field,
            "content_type": self.content_type,
            "bytes": self.bytes,
            "error": self.error,
            "attempts": self.attempts,
            "media_ref": self.media_ref,
        }


class OneBotMediaDownloader:
    """Download NapCat/OneBot media refs into a local temporary media store."""

    def __init__(
        self,
        root_dir: str,
        onebot_manager: Optional[OneBotConnectionManager] = None,
        max_bytes: int = MAX_MEDIA_BYTES,
    ):
        self.root_dir = Path(root_dir).resolve()
        self.onebot_manager = onebot_manager
        self.max_bytes = max_bytes
        self.base_dir = self.root_dir / "data" / "onebot_media"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def prepare_media_ref(self, media_ref: dict, event_raw: Optional[dict] = None) -> MediaDownloadResult:
        ref = dict(media_ref or {})
        attempts: list[dict] = []

        existing = self._local_existing_path(ref.get("local_path"))
        if existing:
            return self._result_from_existing(ref, existing, "local_path", attempts)

        path_value = ref.get("path")
        if path_value:
            attempts.append({"source": "path", "value": str(path_value)})
            source_path = self._local_existing_path(path_value)
            if source_path:
                try:
                    return await self._copy_local_file(ref, source_path, "path", attempts)
                except Exception as exc:  # noqa: BLE001
                    attempts[-1]["error"] = str(exc)

        url_value = str(ref.get("url") or "").strip()
        if url_value:
            attempts.append({"source": "url", "value": self._redact_url(url_value)})
            try:
                return await self._download_url(ref, url_value, attempts)
            except Exception as exc:  # noqa: BLE001
                attempts[-1]["error"] = str(exc)

        refreshed = await self._refresh_ref_from_onebot(ref, event_raw or {}, attempts)
        if refreshed:
            return await self.prepare_media_ref(refreshed, event_raw)

        ref.update({
            "download_status": "failed",
            "download_error": "no downloadable path/url available",
        })
        return MediaDownloadResult(
            status="failed",
            media_ref=ref,
            error="no downloadable path/url available",
            attempts=attempts,
        )

    async def _copy_local_file(
        self,
        media_ref: dict,
        source_path: Path,
        source_field: str,
        attempts: list[dict],
    ) -> MediaDownloadResult:
        size = source_path.stat().st_size
        if size > self.max_bytes:
            raise ValueError(f"media is too large: {size} bytes")
        data = source_path.read_bytes()
        target = self._target_path(media_ref, data, source_path.suffix)
        shutil.copyfile(source_path, target)
        return self._success_result(media_ref, target, data, source_field, attempts)

    async def _download_url(self, media_ref: dict, url: str, attempts: list[dict]) -> MediaDownloadResult:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.content
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if len(data) > self.max_bytes:
            raise ValueError(f"media is too large: {len(data)} bytes")
        target = self._target_path(media_ref, data, Path(urlparse(url).path).suffix, content_type)
        target.write_bytes(data)
        return self._success_result(media_ref, target, data, "url", attempts, content_type)

    async def _refresh_ref_from_onebot(
        self,
        media_ref: dict,
        event_raw: dict,
        attempts: list[dict],
    ) -> Optional[dict]:
        if not self.onebot_manager:
            return None

        file_value = str(media_ref.get("file") or "").strip()
        if file_value:
            attempts.append({"source": "get_image", "file": file_value})
            try:
                response = await self.onebot_manager.get_image(file_value)
                refreshed = self._merge_action_data(media_ref, response)
                if refreshed.get("url") or refreshed.get("path"):
                    return refreshed
            except Exception as exc:  # noqa: BLE001
                attempts[-1]["error"] = str(exc)

        message_id = str(media_ref.get("onebot_message_id") or event_raw.get("onebot_message_id") or "").strip()
        if message_id:
            attempts.append({"source": "get_msg", "message_id": message_id})
            try:
                response = await self.onebot_manager.get_msg(message_id)
                refreshed = self._media_ref_from_get_msg(media_ref, response)
                if refreshed and (refreshed.get("url") or refreshed.get("path")):
                    return refreshed
            except Exception as exc:  # noqa: BLE001
                attempts[-1]["error"] = str(exc)

        file_id = str(media_ref.get("file_id") or media_ref.get("file_unique") or "").strip()
        if file_id:
            attempts.append({"source": "get_file", "file_id": file_id})
            try:
                response = await self.onebot_manager.get_file(file_id)
                refreshed = self._merge_action_data(media_ref, response)
                if refreshed.get("url") or refreshed.get("path"):
                    return refreshed
            except Exception as exc:  # noqa: BLE001
                attempts[-1]["error"] = str(exc)

        return None

    def _merge_action_data(self, media_ref: dict, response: dict) -> dict:
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            return dict(media_ref)
        merged = dict(media_ref)
        for key in ("url", "path", "file", "file_id", "file_unique", "file_size"):
            if data.get(key) is not None:
                merged[key] = data.get(key)
        return merged

    def _media_ref_from_get_msg(self, media_ref: dict, response: dict) -> Optional[dict]:
        data = response.get("data") if isinstance(response, dict) else None
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, list):
            return None

        target_index = media_ref.get("segment_index")
        for index, segment in enumerate(message):
            if not isinstance(segment, dict):
                continue
            seg_type = str(segment.get("type") or "").lower()
            if seg_type not in {"image", "mface"}:
                continue
            if target_index is not None and index != target_index:
                continue
            data = segment.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            refreshed = dict(media_ref)
            for key in ("url", "path", "file", "file_id", "file_unique", "file_size", "summary", "sub_type"):
                if data.get(key) is not None:
                    refreshed[key] = data.get(key)
            return refreshed
        return None

    def _result_from_existing(
        self,
        media_ref: dict,
        path: Path,
        source_field: str,
        attempts: list[dict],
    ) -> MediaDownloadResult:
        data = path.read_bytes()
        media_ref = dict(media_ref)
        media_ref.update({
            "local_path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "download_status": "ready",
            "download_error": None,
        })
        return MediaDownloadResult(
            status="ready",
            media_ref=media_ref,
            local_path=str(path),
            sha256=media_ref["sha256"],
            source_field=source_field,
            content_type=self._detect_content_type(data, path.suffix),
            bytes=len(data),
            attempts=attempts,
        )

    def _success_result(
        self,
        media_ref: dict,
        target: Path,
        data: bytes,
        source_field: str,
        attempts: list[dict],
        content_type: Optional[str] = None,
    ) -> MediaDownloadResult:
        sha256 = hashlib.sha256(data).hexdigest()
        detected_content_type = content_type or self._detect_content_type(data, target.suffix)
        result_ref = dict(media_ref)
        result_ref.update({
            "local_path": str(target),
            "sha256": sha256,
            "download_status": "downloaded",
            "download_error": None,
        })
        return MediaDownloadResult(
            status="downloaded",
            media_ref=result_ref,
            local_path=str(target),
            sha256=sha256,
            source_field=source_field,
            content_type=detected_content_type,
            bytes=len(data),
            attempts=attempts,
        )

    def _target_path(
        self,
        media_ref: dict,
        data: bytes,
        source_suffix: str = "",
        content_type: Optional[str] = None,
    ) -> Path:
        ext = self._detect_extension(data, source_suffix, content_type)
        day_dir = self.base_dir / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        message_id = self._safe_token(media_ref.get("onebot_message_id")) or "msg"
        segment_index = self._safe_token(media_ref.get("segment_index")) or "0"
        identity = (
            self._safe_token(media_ref.get("file_unique"))
            or self._safe_token(media_ref.get("file_id"))
            or self._safe_token(Path(str(media_ref.get("file") or "")).stem)
            or hashlib.sha256(data).hexdigest()[:12]
        )
        target = day_dir / f"{message_id}_{segment_index}_{identity}{ext}"
        if target.exists():
            target = day_dir / f"{message_id}_{segment_index}_{identity}_{uuid.uuid4().hex[:6]}{ext}"
        return target

    def _detect_extension(self, data: bytes, source_suffix: str = "", content_type: Optional[str] = None) -> str:
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        if data.startswith(b"BM"):
            return ".bmp"
        if content_type:
            mapping = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "image/bmp": ".bmp",
            }
            if content_type in mapping:
                return mapping[content_type]
        suffix = (source_suffix or "").lower()
        if suffix in SUPPORTED_MEDIA_EXTENSIONS:
            return suffix
        raise ValueError("file header is not a supported image")

    def _detect_content_type(self, data: bytes, suffix: str = "") -> str:
        ext = self._detect_extension(data, suffix)
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(ext, "application/octet-stream")

    def _local_existing_path(self, value) -> Optional[Path]:
        text = str(value or "").strip()
        if not text:
            return None
        if text.startswith("file:///"):
            parsed = urlparse(text)
            text = unquote(parsed.path or "")
            if os.name == "nt" and len(text) > 2 and text[0] == "/" and text[2] == ":":
                text = text[1:]
        path = Path(text)
        if not path.is_absolute():
            path = self.root_dir / path
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if resolved.is_file():
            return resolved
        return None

    def _safe_token(self, value) -> str:
        text = str(value or "").strip()
        text = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in text)
        return text.strip("_")[:80]

    def _redact_url(self, url: str) -> str:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        return parsed._replace(query="...").geturl()
