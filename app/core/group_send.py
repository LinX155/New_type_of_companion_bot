from copy import deepcopy
from datetime import datetime, timedelta
from typing import Callable, Optional

from .sessions import normalize_session_id


DEFAULT_GROUP_SEND_CONFIG = {
    "enabled": False,
    "allowed_group_ids": [],
    "default_group_policy": {
        "observe_only": True,
        "allow_mention_reply": True,
        "allow_roll_reply": False,
        "allow_repetition": False,
        "allow_meme_send": False,
        "allow_active_message": False,
        "allow_command_reply": True,
    },
    "groups": {},
    "min_interval_seconds": 60.0,
    "dedupe_window_seconds": 300.0,
    "dedupe_recent_limit": 20,
}


NowFunc = Callable[[], datetime]


class GroupSendLimiter:
    """Final group-send switch, whitelist, rate limit, and de-duplication."""

    def __init__(self, config: Optional[dict] = None, now_func: Optional[NowFunc] = None):
        self.config = normalize_group_send_config(config)
        self.now_func = now_func or datetime.now
        self._last_sent_at: dict[str, datetime] = {}
        self._recent_fingerprints: dict[str, list[tuple[datetime, str]]] = {}

    def update_config(self, config: Optional[dict]):
        self.config = normalize_group_send_config(config)

    def evaluate(
        self,
        session_id: str,
        *,
        item_type: str,
        content: str,
        reply_to_message_id: Optional[str] = None,
        trigger_reason: Optional[str] = None,
    ) -> dict:
        sid = normalize_session_id(session_id)
        group_id = group_id_from_session_id(sid)
        config = deepcopy(self.config)
        if not group_id:
            return _deny("invalid_group_session", sid, group_id, config)

        policy = evaluate_group_send_policy(
            config,
            sid,
            trigger_reason=trigger_reason,
            item_type=item_type,
        )
        if not policy.get("ok"):
            return _deny(policy.get("reason") or "group_send_policy_blocked", sid, group_id, config, policy=policy)

        now = self.now_func()
        min_interval = float(config["min_interval_seconds"])
        last_sent = self._last_sent_at.get(sid)
        if last_sent and (now - last_sent).total_seconds() < min_interval:
            return {
                **_deny("group_send_rate_limited", sid, group_id, config),
                "last_sent_at": last_sent.isoformat(),
                "retry_after_seconds": max(0.0, min_interval - (now - last_sent).total_seconds()),
            }

        fingerprint = _fingerprint(item_type, content, reply_to_message_id)
        recent = self._pruned_recent(sid, now)
        if any(existing == fingerprint for _ts, existing in recent):
            return _deny("group_send_duplicate", sid, group_id, config)

        return {
            "ok": True,
            "reason": None,
            "session_id": sid,
            "group_id": group_id,
            "config": config,
            "policy": policy,
            "fingerprint": fingerprint,
        }

    def record_sent(
        self,
        session_id: str,
        *,
        item_type: str,
        content: str,
        reply_to_message_id: Optional[str] = None,
    ):
        sid = normalize_session_id(session_id)
        now = self.now_func()
        self._last_sent_at[sid] = now
        recent = self._pruned_recent(sid, now)
        recent.append((now, _fingerprint(item_type, content, reply_to_message_id)))
        limit = int(self.config["dedupe_recent_limit"])
        self._recent_fingerprints[sid] = recent[-limit:]

    def status(self, session_id: Optional[str] = None) -> dict:
        config = deepcopy(self.config)
        if session_id:
            sid = normalize_session_id(session_id)
            return {
                "enabled": config["enabled"],
                "phase": 10,
                "session_id": sid,
                "group_id": group_id_from_session_id(sid),
                "whitelisted": group_id_from_session_id(sid) in set(config["allowed_group_ids"]),
                "policy": group_send_policy_for_session(config, sid),
                "last_sent_at": _dt_text(self._last_sent_at.get(sid)),
                "recent_fingerprint_count": len(self._recent_fingerprints.get(sid, [])),
                "config": config,
            }
        return {
            "enabled": config["enabled"],
            "phase": 10,
            "sessions": {
                sid: {
                    "last_sent_at": _dt_text(value),
                    "recent_fingerprint_count": len(self._recent_fingerprints.get(sid, [])),
                }
                for sid, value in sorted(self._last_sent_at.items())
            },
            "config": config,
        }

    def clear(self, session_id: Optional[str] = None):
        if session_id:
            sid = normalize_session_id(session_id)
            self._last_sent_at.pop(sid, None)
            self._recent_fingerprints.pop(sid, None)
            return
        self._last_sent_at.clear()
        self._recent_fingerprints.clear()

    def _pruned_recent(self, session_id: str, now: datetime) -> list[tuple[datetime, str]]:
        window = timedelta(seconds=float(self.config["dedupe_window_seconds"]))
        recent = [
            (ts, fp)
            for ts, fp in self._recent_fingerprints.get(session_id, [])
            if now - ts <= window
        ]
        self._recent_fingerprints[session_id] = recent
        return recent


def normalize_group_send_config(config: Optional[dict] = None) -> dict:
    merged = {**DEFAULT_GROUP_SEND_CONFIG, **(config or {})}
    allowed = merged.get("allowed_group_ids")
    if allowed is None:
        allowed = merged.get("whitelist_group_ids") or merged.get("enabled_group_ids") or []
    if isinstance(allowed, str):
        allowed = [part.strip() for part in allowed.split(",")]

    raw_groups = merged.get("groups") or merged.get("group_policies") or {}
    normalized_groups: dict[str, dict] = {}
    if isinstance(raw_groups, dict):
        for raw_group_id, raw_policy in raw_groups.items():
            group_id = str(raw_group_id or "").strip()
            if not group_id:
                continue
            normalized_groups[group_id] = normalize_group_policy(raw_policy)

    normalized_allowed = sorted({
        str(item).strip()
        for item in (allowed or [])
        if str(item).strip()
    } | set(normalized_groups.keys()))
    return {
        "enabled": bool(merged.get("enabled")),
        "allowed_group_ids": normalized_allowed,
        "default_group_policy": normalize_group_policy(
            merged.get("default_group_policy") or merged.get("default_policy")
        ),
        "groups": normalized_groups,
        "min_interval_seconds": max(0.0, float(merged.get("min_interval_seconds") or 0.0)),
        "dedupe_window_seconds": max(0.0, float(merged.get("dedupe_window_seconds") or 0.0)),
        "dedupe_recent_limit": max(1, int(merged.get("dedupe_recent_limit") or 1)),
    }


def normalize_group_policy(policy: Optional[dict] = None) -> dict:
    base = dict(DEFAULT_GROUP_SEND_CONFIG["default_group_policy"])
    raw = {**base, **(policy or {})} if isinstance(policy, dict) else base
    if "read_only" in raw and "observe_only" not in (policy or {}):
        raw["observe_only"] = raw.get("read_only")
    return {
        "observe_only": bool(raw.get("observe_only")),
        "allow_mention_reply": bool(raw.get("allow_mention_reply")),
        "allow_roll_reply": bool(raw.get("allow_roll_reply")),
        "allow_repetition": bool(raw.get("allow_repetition")),
        "allow_meme_send": bool(raw.get("allow_meme_send")),
        "allow_active_message": bool(raw.get("allow_active_message")),
        "allow_command_reply": bool(raw.get("allow_command_reply")),
    }


def group_send_policy_for_session(config: Optional[dict], session_id: str) -> dict:
    normalized = normalize_group_send_config(config)
    sid = normalize_session_id(session_id)
    group_id = group_id_from_session_id(sid)
    group_policy = normalized["groups"].get(group_id)
    policy = deepcopy(group_policy or normalized["default_group_policy"])
    return {
        **policy,
        "session_id": sid,
        "group_id": group_id,
        "configured": group_policy is not None,
        "whitelisted": group_id in set(normalized["allowed_group_ids"]) if group_id else False,
        "global_enabled": normalized["enabled"],
    }


def evaluate_group_send_policy(
    config: Optional[dict],
    session_id: str,
    *,
    trigger_reason: Optional[str] = None,
    item_type: Optional[str] = None,
) -> dict:
    normalized = normalize_group_send_config(config)
    sid = normalize_session_id(session_id)
    group_id = group_id_from_session_id(sid)
    policy = group_send_policy_for_session(normalized, sid)
    result = {
        "ok": True,
        "reason": None,
        "session_id": sid,
        "group_id": group_id,
        "policy": policy,
    }
    if not group_id:
        return {**result, "ok": False, "reason": "invalid_group_session"}
    if not normalized["enabled"]:
        return {**result, "ok": False, "reason": "group_send_disabled"}
    if group_id not in set(normalized["allowed_group_ids"]):
        return {**result, "ok": False, "reason": "group_not_whitelisted"}
    if policy.get("observe_only"):
        return {**result, "ok": False, "reason": "group_observe_only"}

    reason = str(trigger_reason or "").strip()
    reason_block = _trigger_policy_block_reason(policy, reason)
    if reason_block:
        return {**result, "ok": False, "reason": reason_block}

    if _is_meme_item(item_type) and not policy.get("allow_meme_send"):
        return {**result, "ok": False, "reason": "group_meme_send_disabled"}
    return result


def group_id_from_session_id(session_id: str) -> str:
    sid = normalize_session_id(session_id)
    prefix = "qq_group_"
    return sid[len(prefix):] if sid.startswith(prefix) else ""


def _deny(reason: str, session_id: str, group_id: str, config: dict, policy: Optional[dict] = None) -> dict:
    payload = {
        "ok": False,
        "reason": reason,
        "session_id": session_id,
        "group_id": group_id,
        "config": deepcopy(config),
    }
    if policy is not None:
        payload["policy"] = deepcopy(policy)
    return payload


def _trigger_policy_block_reason(policy: dict, trigger_reason: str) -> Optional[str]:
    if not trigger_reason:
        return None
    if trigger_reason == "mention" and not policy.get("allow_mention_reply"):
        return "group_mention_reply_disabled"
    if trigger_reason == "roll" and not policy.get("allow_roll_reply"):
        return "group_roll_reply_disabled"
    if trigger_reason == "group_repetition" and not policy.get("allow_repetition"):
        return "group_repetition_disabled"
    if trigger_reason in {"group.command.mem", "group.command.forget"} and not policy.get("allow_command_reply"):
        return "group_command_reply_disabled"
    if trigger_reason == "active_message" and not policy.get("allow_active_message"):
        return "group_active_message_disabled"
    return None


def _is_meme_item(item_type: Optional[str]) -> bool:
    value = str(item_type or "").strip().lower()
    return value in {"meme", "emoji", "search_meme"}


def _fingerprint(item_type: str, content: str, reply_to_message_id: Optional[str]) -> str:
    return "|".join([
        str(item_type or "text").strip().lower(),
        " ".join(str(content or "").split()),
        str(reply_to_message_id or "").strip(),
    ])


def _dt_text(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None
