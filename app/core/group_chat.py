import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .decisions import Action, ActionDecision, SendItem, SendItemType
from .protocol import (
    ProtocolResult,
    VisibleSafetyResult,
    classify_visible_text,
    parse_and_validate_main_output,
)


GROUP_CHAT_STATE_MACHINE = "GROUP_TYPED_ACTION_HARNESS"
GROUP_CHAT_MAX_VISIBLE_CHARS = 180
GROUP_CHAT_MAX_ITEMS = 4

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
        return (
            self.ok
            and self.decision.action in (Action.REPLY, Action.LIGHT_ACK, Action.REACT)
            and any(item.type != SendItemType.SEARCH_MEME for item in self.decision.all_items())
        )


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
        "allowed_outputs": ["WAIT", "short_text", "text_with_meme_marker", "text_with_active_message_marker"],
        "internal_typed_items": ["text", "search_meme", "meme"],
        "search_meme_resolution": "shared_meme_selector",
        "active_message_side_effect": "group_scoped_after_successful_send",
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

    structural_error = _group_structural_error(text)
    if structural_error:
        return GroupChatParseResult(
            status="guarded",
            decision=_wait_decision(),
            raw_text=text,
            safety=VisibleSafetyResult(ok=False, reason=structural_error, mode="bubble"),
            errors=[structural_error],
        )

    protocol = parse_and_validate_main_output(text, is_cold_context=False)
    if not protocol.ok or protocol.decision is None:
        reason = _protocol_error_reason(protocol)
        return GroupChatParseResult(
            status=protocol.status,
            decision=_wait_decision(),
            raw_text=text,
            safety=VisibleSafetyResult(ok=False, reason=reason, mode="model_raw"),
            errors=protocol.errors or [reason],
        )

    decision = protocol.decision
    errors = _validate_group_decision(
        decision,
        known_qids=known_qids,
        known_message_ids=known_message_ids,
    )
    if errors:
        return GroupChatParseResult(
            status="guarded",
            decision=_wait_decision(),
            raw_text=text,
            safety=VisibleSafetyResult(ok=False, reason=errors[0], mode="bubble"),
            errors=errors,
        )

    return GroupChatParseResult(
        status="ok",
        decision=decision,
        raw_text=text,
        safety=VisibleSafetyResult(ok=True, mode="bubble"),
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


def build_group_chat_repair_messages(
    base_messages: list,
    errors: list[str],
    original_raw: str,
    original_decision: Optional[ActionDecision] = None,
) -> list:
    original_payload = (
        original_decision.to_harness_payload(exclude_none=True)
        if original_decision
        else None
    )
    repair_payload = {
        "message_type": "SYSTEM_REMINDER",
        "internal_tool": "group_chat_action_harness_repair",
        "status": "GROUP_CHAT_ACTION_HARNESS_ERROR",
        "blocked_output_was_not_sent": True,
        "blocked_output_visibility": "internal_only_not_visible_to_group",
        "errors": errors,
        "original_raw_output": original_raw or "(empty)",
        "original_decision": original_payload,
        "required_output": "Return one corrected group-chat lightweight output.",
        "allowed_outputs": [
            "WAIT",
            "one short natural group-chat text",
            "one short natural group-chat text with one &&category:keywords&& marker",
            "one short natural group-chat confirmation with one &&next:YYYY-MM-DD HH:mm&& or &&daily:HH:mm&& marker",
        ],
        "repair_rules": [
            "This is an internal repair event, not a group message.",
            "The group did not see original_raw_output.",
            "Rewrite the same intended reply for the same group window; do not start a new topic.",
            "Do not output JSON, Markdown, code fences, explanations, role tags, tool tags, quote tags, or XML-like tags.",
            "Do not output qid, message_id, sender_card, sender_nickname, media_key, file names, local paths, or payload field names.",
            "Do not output <meme:...>, meme:..., search_meme:..., :meme:..., unknown, or any candidate list.",
            "If the draft intended a meme reaction, use exactly one &&category:keywords&& marker embedded in or after a short natural text.",
            "Do not output only a meme marker; if there is no natural group-chat text, output WAIT.",
            "If the draft intended an active-message time setting, use one short natural confirmation with exactly one valid &&next:YYYY-MM-DD HH:mm&& or &&daily:HH:mm&& marker.",
            "Keep the reply short, natural, and group-chat-like.",
        ],
    }
    return [
        *base_messages,
        {"role": "system", "content": json.dumps(repair_payload, ensure_ascii=False)},
        {
            "role": "system",
            "content": (
                "SYSTEM REMINDER: GROUP_CHAT_ACTION_HARNESS_ERROR. "
                "The prior assistant draft was blocked before delivery. "
                "Return only the corrected group-chat output: WAIT, one short natural text, "
                "one short natural text with one &&category:keywords&& marker, "
                "or one short natural confirmation with one valid &&next:YYYY-MM-DD HH:mm&& / &&daily:HH:mm&& marker. "
                "No JSON, no Markdown, no quote protocol, no qid/message_id, no tool/system tags."
            ),
        },
    ]


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
    if "\n" in stripped and len([line for line in stripped.splitlines() if line.strip()]) > GROUP_CHAT_MAX_ITEMS:
        return "multi_line_group_reply_not_allowed"
    return None


def _protocol_error_reason(result: ProtocolResult) -> str:
    if result.errors:
        first = str(result.errors[0] or "").strip()
        if "内部协议" in first or "系统标记" in first:
            return "internal_protocol_leak"
        if "JSON" in first or "action/items" in first:
            return "json_protocol_not_allowed"
        if "畸形表情" in first:
            return "malformed_meme_marker"
    return result.status or "group_action_harness_error"


def _validate_group_decision(
    decision: ActionDecision,
    known_qids: Optional[list[str]] = None,
    known_message_ids: Optional[list[str]] = None,
) -> list[str]:
    errors: list[str] = []
    if decision.action in (Action.ENTER_CHAT, Action.END_CHAT):
        errors.append(f"{decision.action.value.lower()}_not_allowed_in_group_chat")
    items = decision.all_items()
    if len(items) > GROUP_CHAT_MAX_ITEMS:
        errors.append("too_many_group_chat_items")

    for index, item in enumerate(items):
        if item.type == SendItemType.TEXT:
            safety = classify_group_visible_text(
                item.content,
                known_qids=known_qids,
                known_message_ids=known_message_ids,
            )
            if not safety.ok:
                errors.append(safety.reason or f"text_item_{index + 1}_unsafe")
            continue

        if item.type == SendItemType.SEARCH_MEME:
            # search_meme is allowed only after the raw marker has been parsed
            # into a typed item. It is still internal and must not be sent as text.
            continue

        if item.type == SendItemType.MEME:
            stem = item.harness_value()
            if (
                not stem
                or stem.lower() == "unknown"
                or any(token in stem for token in ("/", "\\", "<", ">", ":", " "))
            ):
                errors.append("invalid_group_meme_item")
            continue

        errors.append(f"{item.type.value}_not_allowed_in_group_chat")

    return errors


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
