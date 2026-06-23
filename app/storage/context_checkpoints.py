import json
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


def _is_internal_visible_leak(text: str) -> bool:
    return (
        contains_internal_visible_protocol(text)
        or contains_malformed_visible_meme_marker(text)
        or contains_visible_meme_marker(text)
    )


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
