import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .decisions import Action, ActionDecision, SendItem, SendItemType
from .protocol import VisibleSafetyResult, classify_visible_text


GROUP_CHAT_STATE_MACHINE = "WAIT_OR_TEXT"
GROUP_CHAT_MAX_VISIBLE_CHARS = 180

GROUP_INTERNAL_TOKEN_RE = re.compile(
    r"("
    r"<\s*/?\s*meme\b|"
    r"\bmeme\s*:|"
    r"\bsearch_meme\s*:|"
    r"\bunknown\b|"
    r"\binternal_event_harness\b|"
    r"\bmeme_intake_result\b|"
    r"\bsticker_pending\b|"
    r"\bsticker_processed\b|"
    r"\bmedia_key\b|"
    r"\bmessage_id\b|"
    r"\breply_to_message_id\b|"
    r"\bsender_card\b|"
    r"\bsender_nickname\b|"
    r"平台群名片|"
    r"平台昵称"
    r")",
    re.IGNORECASE,
)


@dataclass
class GroupChatParseResult:
    status: str
    decision: ActionDecision
    raw_text: str = ""
    safety: VisibleSafetyResult = field(default_factory=lambda: VisibleSafetyResult(ok=True, mode="bubble"))
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def should_send(self) -> bool:
        return self.ok and self.decision.action == Action.REPLY


def group_chat_runtime_status() -> dict:
    return {
        "enabled": True,
        "phase": 10,
        "prompt_phase": 4,
        "state_machine": GROUP_CHAT_STATE_MACHINE,
        "send_layer_enabled": True,
        "send_enabled": "config_gated",
        "trigger_scheduler_enabled": True,
        "image_understanding_enabled": True,
        "group_memory_enabled": True,
        "repetition_enabled": True,
        "activity_dynamic_cooldown_enabled": True,
        "uses_private_hot_cold": False,
        "uses_private_input_gate": False,
        "allowed_outputs": ["WAIT", "short_text"],
        "safety_guards": [
            "visible_text_classifier",
            "known_qid_leak",
            "platform_group_card_leak",
            "internal_meme_event_leak",
            "quote_or_system_protocol_leak",
        ],
    }


def parse_group_chat_output(
    raw_output: str,
    known_qids: Optional[list[str]] = None,
    known_message_ids: Optional[list[str]] = None,
) -> GroupChatParseResult:
    text = str(raw_output or "").strip()
    if not text:
        return GroupChatParseResult(
            status="empty_wait",
            decision=_wait_decision(),
            raw_text=text,
        )

    if text.upper() == "WAIT":
        return GroupChatParseResult(
            status="ok",
            decision=_wait_decision(),
            raw_text=text,
        )

    structural_error = _group_structural_error(text)
    if structural_error:
        return GroupChatParseResult(
            status="guarded",
            decision=_wait_decision(),
            raw_text=text,
            safety=VisibleSafetyResult(ok=False, reason=structural_error, mode="bubble"),
            errors=[structural_error],
        )

    safety = classify_group_visible_text(
        text,
        known_qids=known_qids,
        known_message_ids=known_message_ids,
    )
    if not safety.ok:
        return GroupChatParseResult(
            status="guarded",
            decision=_wait_decision(),
            raw_text=text,
            safety=safety,
            errors=[safety.reason or "unsafe_group_visible_text"],
        )

    return GroupChatParseResult(
        status="ok",
        decision=ActionDecision(
            action=Action.REPLY,
            items=[SendItem(type=SendItemType.TEXT, content=text)],
        ),
        raw_text=text,
        safety=safety,
    )


def classify_group_visible_text(
    text: str,
    known_qids: Optional[list[str]] = None,
    known_message_ids: Optional[list[str]] = None,
) -> VisibleSafetyResult:
    base = classify_visible_text(text, mode="bubble")
    if not base.ok:
        return base

    content = str(text or "")
    if len(content.strip()) > GROUP_CHAT_MAX_VISIBLE_CHARS:
        return VisibleSafetyResult(ok=False, reason="group_reply_too_long", mode="bubble")

    qid = _first_leaked_qid(content, known_qids or [])
    if qid:
        return VisibleSafetyResult(ok=False, reason="known_qid_leak", mode="bubble")

    message_id = _first_leaked_qid(content, known_message_ids or [])
    if message_id:
        return VisibleSafetyResult(ok=False, reason="known_message_id_leak", mode="bubble")

    if GROUP_INTERNAL_TOKEN_RE.search(content):
        return VisibleSafetyResult(ok=False, reason="group_internal_event_leak", mode="bubble")

    return VisibleSafetyResult(ok=True, mode="bubble")


def known_qids_from_group_window(
    group_window: list[dict[str, Any]],
    qid_to_nickname: Optional[dict[str, str]] = None,
) -> list[str]:
    qids: set[str] = set()
    for item in group_window or []:
        if not isinstance(item, dict):
            continue
        qid = _normalize_qid(item.get("qid"))
        if qid:
            qids.add(qid)
    for qid in (qid_to_nickname or {}).keys():
        normalized = _normalize_qid(qid)
        if normalized:
            qids.add(normalized)
    return sorted(qids)


def known_message_ids_from_group_window(
    group_window: list[dict[str, Any]],
    extra_message_ids: Optional[list[str]] = None,
) -> list[str]:
    message_ids: set[str] = set()
    for item in group_window or []:
        if not isinstance(item, dict):
            continue
        for key in ("message_id", "reply_to_message_id"):
            message_id = _normalize_qid(item.get(key))
            if message_id:
                message_ids.add(message_id)
        for media in item.get("media") or []:
            if not isinstance(media, dict):
                continue
            message_id = _normalize_qid(media.get("message_id"))
            if message_id:
                message_ids.add(message_id)
    for message_id in extra_message_ids or []:
        normalized = _normalize_qid(message_id)
        if normalized:
            message_ids.add(normalized)
    return sorted(message_ids)


def _wait_decision() -> ActionDecision:
    return ActionDecision(action=Action.WAIT)


def _group_structural_error(text: str) -> Optional[str]:
    stripped = text.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        return "markdown_code_fence_not_allowed"
    if stripped.startswith("{") or stripped.startswith("["):
        return "json_protocol_not_allowed"
    if "\n" in stripped and len([line for line in stripped.splitlines() if line.strip()]) > 2:
        return "multi_line_group_reply_not_allowed"
    return None


def _first_leaked_qid(content: str, known_qids: list[str]) -> Optional[str]:
    for raw_qid in known_qids:
        qid = _normalize_qid(raw_qid)
        if not qid:
            continue
        if re.search(rf"(?<!\d){re.escape(qid)}(?!\d)", content):
            return qid
    return None


def _normalize_qid(value: Any) -> str:
    qid = str(value or "").strip()
    if not qid or not qid.isdigit() or len(qid) < 4:
        return ""
    return qid
