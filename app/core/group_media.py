import copy
from datetime import datetime
from typing import Optional

from .events import ChatEvent
from .sessions import normalize_session_id


class GroupImageUnderstandingCache:
    """Short-lived in-memory storage for group image refs and understanding.

    Raw media refs are kept out of the group model window and status payloads.
    They are only used later if a group trigger actually asks the LLM to read
    the window.
    """

    def __init__(self, max_items_per_session: int = 120):
        self.max_items_per_session = max(1, int(max_items_per_session or 120))
        self._refs_by_session: dict[str, dict[str, dict]] = {}
        self._payloads_by_session: dict[str, dict[str, dict]] = {}
        self._updated_at_by_session: dict[str, dict[str, str]] = {}

    def remember_event(self, event: ChatEvent) -> dict:
        raw = event.raw if isinstance(event.raw, dict) else {}
        session_id = normalize_session_id(event.session_id)
        media_refs = raw.get("media_refs") or []
        if not isinstance(media_refs, list):
            return {"remembered": 0, "media_keys": []}

        remembered = []
        refs = self._refs_by_session.setdefault(session_id, {})
        for index, media_ref in enumerate(media_refs):
            if not isinstance(media_ref, dict) or media_ref.get("is_sticker"):
                continue
            key = group_image_media_key(
                session_id,
                media_ref.get("onebot_message_id") or raw.get("onebot_message_id"),
                media_ref.get("segment_index", index),
            )
            if not key:
                continue
            refs[key] = copy.deepcopy(media_ref)
            remembered.append(key)
        self._trim_session(session_id)
        return {"remembered": len(remembered), "media_keys": remembered}

    def get_ref(self, session_id: str, media_key: str) -> Optional[dict]:
        ref = self._refs_by_session.get(normalize_session_id(session_id), {}).get(media_key)
        return copy.deepcopy(ref) if ref else None

    def get_payload(self, session_id: str, media_key: str) -> Optional[dict]:
        payload = self._payloads_by_session.get(normalize_session_id(session_id), {}).get(media_key)
        return copy.deepcopy(payload) if payload else None

    def set_payload(self, session_id: str, media_key: str, payload: dict):
        sid = normalize_session_id(session_id)
        self._payloads_by_session.setdefault(sid, {})[media_key] = copy.deepcopy(payload)
        self._updated_at_by_session.setdefault(sid, {})[media_key] = datetime.now().isoformat()
        self._trim_session(sid)

    def clear(self, session_id: Optional[str] = None):
        if session_id:
            sid = normalize_session_id(session_id)
            self._refs_by_session.pop(sid, None)
            self._payloads_by_session.pop(sid, None)
            self._updated_at_by_session.pop(sid, None)
            return
        self._refs_by_session.clear()
        self._payloads_by_session.clear()
        self._updated_at_by_session.clear()

    def status(self, session_id: Optional[str] = None) -> dict:
        if session_id:
            sid = normalize_session_id(session_id)
            refs = self._refs_by_session.get(sid, {})
            payloads = self._payloads_by_session.get(sid, {})
            return {
                "enabled": True,
                "session_id": sid,
                "ref_count": len(refs),
                "understanding_count": len(payloads),
                "media_keys": sorted(payloads),
            }
        return {
            "enabled": True,
            "sessions": {
                sid: {
                    "ref_count": len(refs),
                    "understanding_count": len(self._payloads_by_session.get(sid, {})),
                }
                for sid, refs in sorted(self._refs_by_session.items())
            },
        }

    def _trim_session(self, session_id: str):
        sid = normalize_session_id(session_id)
        refs = self._refs_by_session.get(sid, {})
        payloads = self._payloads_by_session.get(sid, {})
        updated = self._updated_at_by_session.get(sid, {})
        while len(refs) > self.max_items_per_session:
            key = next(iter(refs))
            refs.pop(key, None)
            payloads.pop(key, None)
            updated.pop(key, None)
        while len(payloads) > self.max_items_per_session:
            key = next(iter(payloads))
            payloads.pop(key, None)
            updated.pop(key, None)


def group_image_media_key(session_id: str, message_id, segment_index) -> str:
    sid = normalize_session_id(session_id)
    msg = str(message_id or "").strip()
    if not sid or not msg:
        return ""
    segment = "0" if segment_index is None else str(segment_index)
    return f"{sid}:image:{msg}:{segment}"
