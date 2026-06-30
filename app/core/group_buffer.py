import asyncio
import copy
from datetime import datetime
from typing import Optional

from .events import ChatEvent, EventType
from .sessions import normalize_session_id


class GroupChatBuffer:
    """Group chat window isolated from the private-chat runtime.

    This deliberately does not reuse EventGate. Group chat does not use private
    HOT/COLD, composing, stale-send, or private send-group semantics.
    """

    def __init__(self, max_events: int = 200):
        self.max_events = max(1, int(max_events or 200))
        self._lock = asyncio.Lock()
        self._entries_by_session: dict[str, list[dict]] = {}
        self._versions_by_session: dict[str, int] = {}

    async def append(self, event: ChatEvent) -> dict:
        entry = summarize_group_event(event)
        session_id = entry["session_id"]
        async with self._lock:
            entries = self._entries_by_session.setdefault(session_id, [])
            entries.append(entry)
            if len(entries) > self.max_events:
                del entries[: len(entries) - self.max_events]
            version = self._versions_by_session.get(session_id, 0) + 1
            self._versions_by_session[session_id] = version
            return {
                "session_id": session_id,
                "buffer_version": version,
                "buffered_events": len(entries),
                "latest": copy.deepcopy(entry),
            }

    def get_window(self, session_id: str, limit: Optional[int] = None) -> list[dict]:
        sid = normalize_session_id(session_id)
        entries = self._entries_by_session.get(sid, [])
        if limit is not None:
            entries = entries[-max(0, int(limit)):]
        return copy.deepcopy(entries)

    async def update_meme_result(
        self,
        session_id: str,
        message_id: str,
        segment_index,
        meme: str,
        intake_status: Optional[str] = None,
        media_key: Optional[str] = None,
    ) -> dict:
        sid = normalize_session_id(session_id)
        normalized_message_id = str(message_id or "")
        normalized_meme = str(meme or "").strip() or "unknown"
        async with self._lock:
            entries = self._entries_by_session.get(sid, [])
            for entry in reversed(entries):
                for media in entry.get("media") or []:
                    if not isinstance(media, dict) or media.get("kind") != "meme":
                        continue
                    if str(media.get("message_id") or "") != normalized_message_id:
                        continue
                    if not _same_segment_index(media.get("segment_index"), segment_index):
                        continue
                    entry["meme"] = normalized_meme
                    entry["updated_at"] = datetime.now().isoformat()
                    media["meme"] = normalized_meme
                    if intake_status:
                        media["meme_intake_status"] = str(intake_status)
                    if media_key:
                        media["media_key"] = str(media_key)
                    version = self._versions_by_session.get(sid, 0) + 1
                    self._versions_by_session[sid] = version
                    return {
                        "updated": True,
                        "session_id": sid,
                        "buffer_version": version,
                        "meme": normalized_meme,
                        "latest": copy.deepcopy(entry),
                    }
        return {
            "updated": False,
            "session_id": sid,
            "buffer_version": self._versions_by_session.get(sid, 0),
            "meme": normalized_meme,
        }

    def get_model_window(
        self,
        session_id: str,
        qid_to_nickname: Optional[dict[str, str]] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        known_names = {
            str(qid): str(name).strip()
            for qid, name in (qid_to_nickname or {}).items()
            if str(qid).strip() and str(name).strip()
        }
        window: list[dict] = []
        for entry in self.get_window(session_id, limit):
            qid = str(entry.get("sender_qid") or "")
            item = {"qid": qid}
            nickname = known_names.get(qid)
            if nickname:
                item["nickname"] = nickname
            if entry.get("reply_to_message_id"):
                item["reply_to_message_id"] = entry["reply_to_message_id"]
            if entry.get("text"):
                item["text"] = entry["text"]
            if entry.get("media"):
                item["media"] = _model_media_summaries(entry["media"])
            if entry.get("meme"):
                item["meme"] = entry["meme"]
            window.append(item)
        return window

    def status(self, session_id: Optional[str] = None) -> dict:
        if session_id:
            sid = normalize_session_id(session_id)
            entries = self._entries_by_session.get(sid, [])
            latest = entries[-1] if entries else None
            return {
                "enabled": True,
                "session_id": sid,
                "buffer_version": self._versions_by_session.get(sid, 0),
                "buffered_events": len(entries),
                "latest": copy.deepcopy(latest),
            }
        return {
            "enabled": True,
            "sessions": {
                sid: {
                    "buffer_version": self._versions_by_session.get(sid, 0),
                    "buffered_events": len(entries),
                    "latest": copy.deepcopy(entries[-1]) if entries else None,
                }
                for sid, entries in sorted(self._entries_by_session.items())
            },
        }

    async def clear(self, session_id: Optional[str] = None):
        async with self._lock:
            if session_id:
                sid = normalize_session_id(session_id)
                self._entries_by_session.pop(sid, None)
                self._versions_by_session.pop(sid, None)
                return
            self._entries_by_session.clear()
            self._versions_by_session.clear()


def summarize_group_event(event: ChatEvent) -> dict:
    raw = event.raw if isinstance(event.raw, dict) else {}
    session_id = normalize_session_id(event.session_id)
    event_type = event.event_type.value if isinstance(event.event_type, EventType) else str(event.event_type)
    text = str(event.text or "")
    summary = {
        "session_id": session_id,
        "event_id": event.event_id,
        "event_type": event_type,
        "timestamp": event.timestamp.isoformat() if event.timestamp else datetime.now().isoformat(),
        "group_id": str(raw.get("qq_group_id") or ""),
        "sender_qid": str(raw.get("qq_user_id") or event.user_id or ""),
        "message_id": str(raw.get("onebot_message_id") or ""),
        "reply_to_message_id": str(raw.get("reply_to_message_id") or ""),
        "text": text if event.event_type == EventType.TEXT else "",
        "media": _safe_media_summaries(raw),
        "meme": _meme_summary(event, raw),
    }
    return summary


def _safe_media_summaries(raw: dict) -> list[dict]:
    media: list[dict] = []
    for ref in raw.get("media_refs") or []:
        if not isinstance(ref, dict):
            continue
        media.append({
            "kind": "meme" if ref.get("is_sticker") else "image",
            "message_id": str(ref.get("onebot_message_id") or raw.get("onebot_message_id") or ""),
            "segment_index": ref.get("segment_index"),
            "download_status": ref.get("download_status"),
        })
    for key, kind in (("audio_refs", "audio"), ("video_refs", "video")):
        for ref in raw.get(key) or []:
            if not isinstance(ref, dict):
                continue
            media.append({
                "kind": kind,
                "message_id": str(ref.get("onebot_message_id") or raw.get("onebot_message_id") or ""),
                "segment_index": ref.get("segment_index"),
                "prepare_status": ref.get("prepare_status"),
            })
    return media


def _meme_summary(event: ChatEvent, raw: dict) -> Optional[str]:
    if event.event_type == EventType.STICKER:
        return "unknown"
    for ref in raw.get("media_refs") or []:
        if isinstance(ref, dict) and ref.get("is_sticker"):
            return "unknown"
    return None


def _same_segment_index(left, right) -> bool:
    if left is None and right is None:
        return True
    return str(left) == str(right)


def _model_media_summaries(media_items: list[dict]) -> list[dict]:
    result: list[dict] = []
    for media in media_items or []:
        if not isinstance(media, dict):
            continue
        item = {
            "kind": media.get("kind"),
            "message_id": media.get("message_id"),
            "segment_index": media.get("segment_index"),
        }
        if media.get("meme"):
            item["meme"] = media.get("meme")
        result.append({key: value for key, value in item.items() if value is not None})
    return result
