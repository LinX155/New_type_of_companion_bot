import json
import re
from typing import Optional

from sqlalchemy import desc

from ..core.protocol import (
    contains_internal_visible_protocol,
    contains_malformed_visible_meme_marker,
    contains_visible_meme_marker,
)
from .db import SessionLocal
from .models import ContextCheckpoint, ConversationEvent, RawChatLog


DEFAULT_CONTEXT_CHECKPOINT_THRESHOLD_K = 500
MIN_CONTEXT_CHECKPOINT_THRESHOLD_K = 50
MAX_CONTEXT_CHECKPOINT_THRESHOLD_K = 2000

CONTEXT_CHECKPOINT_DEFAULTS = {
    "threshold_k": DEFAULT_CONTEXT_CHECKPOINT_THRESHOLD_K,
}

CONVERSATION_CONTEXT_EVENT_ROLES = {
    "user_text": "user",
    "user_image": "user",
    "user_sticker": "user",
    "nudge": "user",
    "assistant_text": "assistant",
    "assistant_react": "assistant",
}

GROUP_CONTEXT_CHECKPOINT_EVENT_ROLES = {
    "message.text": "user",
    "message.image": "user",
    "message.sticker": "user",
    "command.mem": "user",
    "command.forget": "user",
    "onebot_group_send": "assistant",
    "group_repetition": "assistant",
    "group_image_understanding": "media",
    "image_understanding_result": "media",
    "meme_intake_result": "meme",
}

GROUP_CHECKPOINT_FORBIDDEN_TEXT_TOKENS = (
    "sender_card",
    "sender_nickname",
    "平台群名片",
    "平台昵称",
    "群名片",
    "QQ昵称",
    "qq昵称",
    "message_id",
    "onebot_message_id",
    "reply_to_message_id",
    "media_key",
    "media_job_id",
    "file_unique",
    "file_id",
    "local_path",
    "file_path",
    "http://",
    "https://",
)


def normalize_context_checkpoint_config(config: Optional[dict]) -> dict:
    merged = dict(CONTEXT_CHECKPOINT_DEFAULTS)
    if isinstance(config, dict):
        merged.update(config)
    return {
        "threshold_k": _clamp_int(
            merged.get("threshold_k"),
            MIN_CONTEXT_CHECKPOINT_THRESHOLD_K,
            MAX_CONTEXT_CHECKPOINT_THRESHOLD_K,
            DEFAULT_CONTEXT_CHECKPOINT_THRESHOLD_K,
        )
    }


def latest_prompt_debug_for_session(session_id: str) -> Optional[dict]:
    db = SessionLocal()
    try:
        row = (
            db.query(RawChatLog)
            .filter(RawChatLog.session_id == session_id)
            .filter(RawChatLog.event_type == "prompt_cache_debug")
            .order_by(desc(RawChatLog.id))
            .first()
        )
        if not row:
            return None
        payload = _parse_json(row.raw_payload)
        return {
            "id": row.id,
            "created_at": row.created_at,
            "payload": payload,
            "tokens": prompt_tokens_from_debug_payload(payload),
            "estimated_tokens": estimated_tokens_from_debug_payload(payload),
            "prompt_tokens": actual_prompt_tokens_from_debug_payload(payload),
        }
    finally:
        db.close()


def latest_context_checkpoint_for_session(session_id: str) -> Optional[dict]:
    db = SessionLocal()
    try:
        row = _latest_checkpoint_row(db, session_id)
        if not row:
            return None
        return _checkpoint_to_dict(row)
    finally:
        db.close()


def load_conversation_context(session_id: str) -> dict:
    """Return the latest durable checkpoint plus visible history after it.

    Without a checkpoint this intentionally returns an empty history to preserve
    the current runtime behavior after process restart.
    """
    db = SessionLocal()
    try:
        checkpoint = _latest_checkpoint_row(db, session_id)
        if not checkpoint:
            return {"checkpoint": None, "checkpoint_text": "", "history": []}
        history = _load_history_rows_after(db, session_id, checkpoint.covered_until_event_id)
        return {
            "checkpoint": _checkpoint_to_dict(checkpoint),
            "checkpoint_text": checkpoint.checkpoint_text or "",
            "history": history,
        }
    finally:
        db.close()


def load_visible_events_for_checkpoint(
    session_id: str,
    after_event_id: Optional[int] = None,
    through_event_id: Optional[int] = None,
) -> list[dict]:
    db = SessionLocal()
    try:
        query = (
            db.query(ConversationEvent)
            .filter(ConversationEvent.session_id == session_id)
            .filter(ConversationEvent.is_visible.is_(True))
            .filter(ConversationEvent.event_type.in_(tuple(CONVERSATION_CONTEXT_EVENT_ROLES.keys())))
        )
        if after_event_id:
            query = query.filter(ConversationEvent.id > after_event_id)
        if through_event_id:
            query = query.filter(ConversationEvent.id <= through_event_id)
        rows = query.order_by(ConversationEvent.id).all()
        items = []
        for row in rows:
            item = _event_to_checkpoint_item(row)
            if item:
                items.append(item)
        return items
    finally:
        db.close()


def load_group_visible_events_for_checkpoint(
    session_id: str,
    after_event_id: Optional[int] = None,
    through_event_id: Optional[int] = None,
) -> list[dict]:
    db = SessionLocal()
    try:
        query = (
            db.query(RawChatLog)
            .filter(RawChatLog.session_id == session_id)
            .filter(RawChatLog.event_type.in_(tuple(GROUP_CONTEXT_CHECKPOINT_EVENT_ROLES.keys())))
        )
        if after_event_id:
            query = query.filter(RawChatLog.id > after_event_id)
        if through_event_id:
            query = query.filter(RawChatLog.id <= through_event_id)
        rows = query.order_by(RawChatLog.id).all()
        items = []
        for row in rows:
            item = _raw_group_event_to_checkpoint_item(row)
            if item:
                items.append(item)
        return items
    finally:
        db.close()


def latest_visible_event_id(session_id: str) -> Optional[int]:
    db = SessionLocal()
    try:
        row = (
            db.query(ConversationEvent.id)
            .filter(ConversationEvent.session_id == session_id)
            .filter(ConversationEvent.is_visible.is_(True))
            .filter(ConversationEvent.event_type.in_(tuple(CONVERSATION_CONTEXT_EVENT_ROLES.keys())))
            .order_by(desc(ConversationEvent.id))
            .first()
        )
        return int(row[0]) if row else None
    finally:
        db.close()


def latest_group_visible_event_id(session_id: str) -> Optional[int]:
    db = SessionLocal()
    try:
        row = (
            db.query(RawChatLog.id)
            .filter(RawChatLog.session_id == session_id)
            .filter(RawChatLog.event_type.in_(tuple(GROUP_CONTEXT_CHECKPOINT_EVENT_ROLES.keys())))
            .order_by(desc(RawChatLog.id))
            .first()
        )
        return int(row[0]) if row else None
    finally:
        db.close()


def prompt_tokens_from_debug_payload(payload: dict) -> int:
    actual = actual_prompt_tokens_from_debug_payload(payload)
    if actual is not None:
        return actual
    return estimated_tokens_from_debug_payload(payload) or 0


def actual_prompt_tokens_from_debug_payload(payload: dict) -> Optional[int]:
    usage = payload.get("llm_usage") if isinstance(payload.get("llm_usage"), dict) else {}
    value = payload.get("prompt_tokens") or usage.get("prompt_tokens") or usage.get("input_tokens")
    return value if isinstance(value, int) else None


def estimated_tokens_from_debug_payload(payload: dict) -> Optional[int]:
    value = payload.get("estimated_prompt_tokens") or payload.get("estimated_tokens")
    return value if isinstance(value, int) else None


def _load_history_rows_after(db, session_id: str, covered_until_event_id: Optional[int]) -> list[dict]:
    query = (
        db.query(ConversationEvent)
        .filter(ConversationEvent.session_id == session_id)
        .filter(ConversationEvent.is_visible.is_(True))
        .filter(ConversationEvent.event_type.in_(tuple(CONVERSATION_CONTEXT_EVENT_ROLES.keys())))
    )
    if covered_until_event_id:
        query = query.filter(ConversationEvent.id > covered_until_event_id)
    rows = query.order_by(ConversationEvent.id).all()
    items = []
    for row in rows:
        item = _event_to_history_item(row)
        if item:
            items.append(item)
    return items


def _event_to_history_item(row: ConversationEvent) -> Optional[dict]:
    role = CONVERSATION_CONTEXT_EVENT_ROLES.get(row.event_type)
    text = (row.text or "").strip()
    if not role or not text:
        return None
    if _is_internal_visible_leak(text):
        return None
    return {"role": role, "text": text}


def _event_to_checkpoint_item(row: ConversationEvent) -> Optional[dict]:
    role = CONVERSATION_CONTEXT_EVENT_ROLES.get(row.event_type)
    text = (row.text or "").strip()
    if not role or not text:
        return None
    if _is_internal_visible_leak(text):
        return None
    return {
        "id": row.id,
        "role": role,
        "event_type": row.event_type,
        "text": text,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _raw_group_event_to_checkpoint_item(row: RawChatLog) -> Optional[dict]:
    role = GROUP_CONTEXT_CHECKPOINT_EVENT_ROLES.get(row.event_type)
    if not role:
        return None
    payload = _parse_json(row.raw_payload)
    raw_root = _group_raw_root(payload)
    message_ids = _group_message_ids_from_payload(payload)
    text = _group_checkpoint_text(row, payload)
    media_items = _group_media_summaries(payload, row.event_type)
    qid = _group_sender_qid(payload)

    if text:
        if _is_internal_visible_leak(text):
            return None
        if _contains_group_checkpoint_forbidden_text(text):
            return None
        if any(message_id and message_id in text for message_id in message_ids):
            return None

    if row.event_type in {"message.image", "message.sticker"} and not media_items:
        kind = "meme" if row.event_type == "message.sticker" else "image"
        media_items = [{"kind": kind, "status": "received"}]

    if row.event_type in {"group_image_understanding", "image_understanding_result"}:
        media_items = _group_image_understanding_summary(payload)
        text = ""
    elif row.event_type == "meme_intake_result":
        media_items = _group_meme_intake_summary(payload)
        text = ""

    if not text and not media_items:
        return None

    item = {
        "id": row.id,
        "role": role,
        "event_type": row.event_type,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }
    if text:
        item["text"] = text
    if qid and re.fullmatch(r"\d{4,}", qid):
        item["qid"] = qid
    if row.item_type:
        item["item_type"] = row.item_type
    if row.action:
        item["action"] = row.action
    if media_items:
        item["media"] = media_items
    if raw_root.get("mentions_bot") is not None:
        item["mentions_bot"] = bool(raw_root.get("mentions_bot"))
    return item


def _group_checkpoint_text(row: RawChatLog, payload: dict) -> str:
    text = (row.input_text or row.final_text or "").strip()
    if text:
        return text
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    for key in ("text", "response", "content", "final_text"):
        value = nested.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = payload.get("text")
    if isinstance(value, str):
        return value.strip()
    return ""


def _group_sender_qid(payload: dict) -> str:
    raw_root = _group_raw_root(payload)
    for source in (payload, raw_root):
        for key in ("sender_qid", "qq_user_id", "user_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _group_raw_root(payload: dict) -> dict:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else None
    return raw if raw is not None else payload


def _group_message_ids_from_payload(payload: dict) -> set[str]:
    raw_root = _group_raw_root(payload)
    ids = set()
    for source in (payload, raw_root):
        for key in ("message_id", "onebot_message_id", "reply_to_message_id"):
            value = str(source.get(key) or "").strip()
            if value:
                ids.add(value)
    media_refs = raw_root.get("media_refs")
    if isinstance(media_refs, list):
        for ref in media_refs:
            if not isinstance(ref, dict):
                continue
            value = str(ref.get("onebot_message_id") or ref.get("message_id") or "").strip()
            if value:
                ids.add(value)
    return ids


def _group_media_summaries(payload: dict, event_type: str) -> list[dict]:
    raw_root = _group_raw_root(payload)
    refs = raw_root.get("media_refs")
    if not isinstance(refs, list):
        refs = payload.get("media_refs") if isinstance(payload.get("media_refs"), list) else []
    result = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        kind = "meme" if ref.get("is_sticker") else "image"
        summary = {
            "kind": kind,
            "status": str(ref.get("download_status") or "received"),
        }
        segment_type = str(ref.get("segment_type") or "").strip()
        if segment_type:
            summary["segment_type"] = segment_type
        ref_summary = str(ref.get("summary") or "").strip()
        if (
            ref_summary
            and not _is_internal_visible_leak(ref_summary)
            and not _contains_group_checkpoint_forbidden_text(ref_summary)
        ):
            summary["summary"] = ref_summary[:300]
        result.append(summary)
    if result:
        return result
    if event_type == "message.image":
        return [{"kind": "image", "status": "received"}]
    if event_type == "message.sticker":
        return [{"kind": "meme", "status": "unknown"}]
    return []


def _group_image_understanding_summary(payload: dict) -> list[dict]:
    status = str(payload.get("status") or "completed").strip() or "completed"
    summary = {"kind": "image", "status": status}
    result = payload.get("result")
    if result is not None:
        text = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
        text = text.strip()
        if (
            text
            and not _is_internal_visible_leak(text)
            and not _contains_group_checkpoint_forbidden_text(text)
        ):
            summary["summary"] = text[:800]
    return [summary]


def _group_meme_intake_summary(payload: dict) -> list[dict]:
    status = str(payload.get("status") or "unknown").strip() or "unknown"
    meme_name = ""
    save_result = payload.get("save_result") if isinstance(payload.get("save_result"), dict) else {}
    for key in ("file_stem", "stem", "name"):
        value = str(save_result.get(key) or "").strip()
        if value:
            meme_name = value
            break
    if not meme_name and status != "duplicate":
        analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
        value = str(analysis.get("name") or analysis.get("suggested_name") or "").strip()
        if value:
            meme_name = value
    item = {"kind": "meme", "status": status}
    if meme_name and not _contains_group_checkpoint_forbidden_text(meme_name):
        item["meme"] = meme_name[:120]
    elif status in {"completed", "saved", "duplicate"}:
        item["meme"] = "unknown"
    return [item]


def _is_internal_visible_leak(text: str) -> bool:
    return (
        contains_internal_visible_protocol(text)
        or contains_malformed_visible_meme_marker(text)
        or contains_visible_meme_marker(text)
    )


def _contains_group_checkpoint_forbidden_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(token.lower() in lowered for token in GROUP_CHECKPOINT_FORBIDDEN_TEXT_TOKENS)


def _latest_checkpoint_row(db, session_id: str) -> Optional[ContextCheckpoint]:
    return (
        db.query(ContextCheckpoint)
        .filter(ContextCheckpoint.session_id == session_id)
        .order_by(desc(ContextCheckpoint.id))
        .first()
    )


def _checkpoint_to_dict(row: ContextCheckpoint) -> dict:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "checkpoint_text": row.checkpoint_text,
        "covered_until_event_id": row.covered_until_event_id,
        "source_prompt_debug_id": row.source_prompt_debug_id,
        "estimated_tokens_before": row.estimated_tokens_before,
        "prompt_tokens_before": row.prompt_tokens_before,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _parse_json(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _clamp_int(value, min_value: int, max_value: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))
