import re
from copy import deepcopy
from datetime import datetime
from typing import Optional

from .events import ChatEvent, EventType
from .sessions import normalize_session_id


DEFAULT_GROUP_REPETITION_CONFIG = {
    "enabled": True,
    "threshold": 3,
    "recent_window_events": 24,
    "cooldown_seconds": 300.0,
    "dedupe_window_seconds": 3600.0,
    "min_text_chars": 1,
    "max_text_chars": 80,
}


class GroupRepetitionDetector:
    def __init__(self, config: Optional[dict] = None, now_func=None):
        self.config = normalize_group_repetition_config(config)
        self.now_func = now_func or datetime.now
        self._last_trigger_at: dict[str, datetime] = {}
        self._dedupe_by_session: dict[str, dict[str, datetime]] = {}
        self._last_result_by_session: dict[str, dict] = {}

    def evaluate(self, event: ChatEvent, group_window: list[dict]) -> dict:
        session_id = normalize_session_id(event.session_id)
        now = event.timestamp or self.now_func()
        if not self.config["enabled"]:
            return self._remember(session_id, {"status": "skipped", "reason": "disabled", "session_id": session_id})
        if event.event_type != EventType.TEXT:
            return self._remember(session_id, {"status": "skipped", "reason": "not_text", "session_id": session_id})

        raw = event.raw if isinstance(event.raw, dict) else {}
        sender_qid = str(raw.get("qq_user_id") or event.user_id or "").strip()
        self_qid = str(raw.get("qq_self_id") or "").strip()
        if self_qid and sender_qid == self_qid:
            return self._remember(session_id, {"status": "skipped", "reason": "self_message", "session_id": session_id})

        text = normalize_repeated_text(event.text or "")
        if not text:
            return self._remember(session_id, {"status": "skipped", "reason": "empty_text", "session_id": session_id})
        if text.startswith("/"):
            return self._remember(session_id, {"status": "skipped", "reason": "command_text", "session_id": session_id})
        if len(text) < self.config["min_text_chars"] or len(text) > self.config["max_text_chars"]:
            return self._remember(session_id, {
                "status": "skipped",
                "reason": "text_length_out_of_range",
                "session_id": session_id,
                "text": text,
            })

        cooldown = self._cooldown_result(session_id, now)
        if cooldown:
            cooldown["text"] = text
            return self._remember(session_id, cooldown)
        dedupe = self._dedupe_result(session_id, text, now)
        if dedupe:
            return self._remember(session_id, dedupe)

        matches = self._matching_entries(group_window, text)
        distinct_qids = sorted({item["sender_qid"] for item in matches if item.get("sender_qid")})
        threshold = int(self.config["threshold"])
        if len(distinct_qids) < threshold:
            return self._remember(session_id, {
                "status": "skipped",
                "reason": "below_threshold",
                "session_id": session_id,
                "text": text,
                "distinct_qid_count": len(distinct_qids),
                "threshold": threshold,
            })

        result = {
            "status": "selected",
            "reason": "group_repetition",
            "session_id": session_id,
            "text": text,
            "distinct_qids": distinct_qids,
            "distinct_qid_count": len(distinct_qids),
            "threshold": threshold,
            "message_ids": [item.get("message_id") for item in matches if item.get("message_id")],
            "latest_message_id": str(raw.get("onebot_message_id") or ""),
            "cooldown_seconds": self.config["cooldown_seconds"],
            "dedupe_window_seconds": self.config["dedupe_window_seconds"],
        }
        self._last_trigger_at[session_id] = now
        self._dedupe_by_session.setdefault(session_id, {})[text] = now
        self._prune_dedupe(session_id, now)
        return self._remember(session_id, result)

    def status(self, session_id: Optional[str] = None) -> dict:
        if session_id:
            sid = normalize_session_id(session_id)
            return {
                "enabled": self.config["enabled"],
                "phase": 9,
                "session_id": sid,
                "last_trigger_at": _dt_to_text(self._last_trigger_at.get(sid)),
                "dedupe_count": len(self._dedupe_by_session.get(sid, {})),
                "last_result": deepcopy(self._last_result_by_session.get(sid)),
                "config": deepcopy(self.config),
            }
        return {
            "enabled": self.config["enabled"],
            "phase": 9,
            "sessions": sorted(set(self._last_trigger_at) | set(self._dedupe_by_session)),
            "config": deepcopy(self.config),
        }

    def clear(self, session_id: Optional[str] = None):
        if session_id:
            sid = normalize_session_id(session_id)
            self._last_trigger_at.pop(sid, None)
            self._dedupe_by_session.pop(sid, None)
            self._last_result_by_session.pop(sid, None)
            return
        self._last_trigger_at.clear()
        self._dedupe_by_session.clear()
        self._last_result_by_session.clear()

    def _matching_entries(self, group_window: list[dict], text: str) -> list[dict]:
        recent = (group_window or [])[-int(self.config["recent_window_events"]):]
        matches = []
        for entry in recent:
            if not isinstance(entry, dict):
                continue
            if normalize_repeated_text(entry.get("text") or "") != text:
                continue
            qid = str(entry.get("sender_qid") or "").strip()
            if not qid:
                continue
            matches.append({
                "sender_qid": qid,
                "message_id": str(entry.get("message_id") or ""),
            })
        return matches

    def _cooldown_result(self, session_id: str, now: datetime) -> Optional[dict]:
        last = self._last_trigger_at.get(session_id)
        if not last:
            return None
        elapsed = (now - last).total_seconds()
        cooldown = float(self.config["cooldown_seconds"])
        if elapsed >= cooldown:
            return None
        return {
            "status": "skipped",
            "reason": "cooldown",
            "session_id": session_id,
            "remaining_seconds": max(0.0, cooldown - elapsed),
        }

    def _dedupe_result(self, session_id: str, text: str, now: datetime) -> Optional[dict]:
        self._prune_dedupe(session_id, now)
        repeated_at = self._dedupe_by_session.get(session_id, {}).get(text)
        if not repeated_at:
            return None
        return {
            "status": "skipped",
            "reason": "duplicate_repeated_text",
            "session_id": session_id,
            "text": text,
            "last_repeated_at": repeated_at.isoformat(),
        }

    def _prune_dedupe(self, session_id: str, now: datetime):
        window = float(self.config["dedupe_window_seconds"])
        dedupe = self._dedupe_by_session.get(session_id)
        if not dedupe:
            return
        for key, ts in list(dedupe.items()):
            if (now - ts).total_seconds() >= window:
                dedupe.pop(key, None)

    def _remember(self, session_id: str, result: dict) -> dict:
        self._last_result_by_session[session_id] = deepcopy(result)
        return result


def normalize_group_repetition_config(config: Optional[dict] = None) -> dict:
    raw = {**DEFAULT_GROUP_REPETITION_CONFIG, **(config or {})}
    return {
        "enabled": bool(raw["enabled"]),
        "threshold": max(2, int(raw["threshold"])),
        "recent_window_events": max(3, int(raw["recent_window_events"])),
        "cooldown_seconds": max(0.0, float(raw["cooldown_seconds"])),
        "dedupe_window_seconds": max(0.0, float(raw["dedupe_window_seconds"])),
        "min_text_chars": max(1, int(raw["min_text_chars"])),
        "max_text_chars": max(1, int(raw["max_text_chars"])),
    }


def normalize_repeated_text(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def _dt_to_text(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None
