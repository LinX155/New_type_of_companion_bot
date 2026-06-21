import uuid
from datetime import datetime
from typing import Optional

from ...core.events import ChatEvent, EventType


def parse_onebot_event(payload: dict, default_input_ttl_ms: int = 8000) -> Optional[ChatEvent]:
    """Convert OneBot11/NapCat events into internal ChatEvent objects."""
    if not isinstance(payload, dict):
        return None

    if _is_input_status_notice(payload):
        user_id = _string_id(payload.get("user_id") or "unknown")
        event_type = _onebot_input_status_event_type(payload.get("event_type"))
        timestamp = _onebot_timestamp(payload.get("time"))
        composing = event_type != 0
        return ChatEvent(
            event_id=f"onebot_input_status_{user_id}_{int(timestamp.timestamp() * 1000)}_{uuid.uuid4().hex[:6]}",
            platform="qq",
            user_id=user_id,
            event_type=EventType.USER_COMPOSING,
            timestamp=timestamp,
            raw={
                "composing": composing,
                "ttl_ms": default_input_ttl_ms,
                "source": "onebot11",
                "onebot": payload,
                "qq_user_id": user_id,
            },
        )

    if _is_private_message(payload):
        return _parse_private_message(payload)

    return None


def _is_input_status_notice(payload: dict) -> bool:
    return (
        payload.get("post_type") == "notice"
        and payload.get("notice_type") == "notify"
        and payload.get("sub_type") == "input_status"
    )


def _is_private_message(payload: dict) -> bool:
    return (
        payload.get("post_type") == "message"
        and payload.get("message_type") == "private"
    )


def _parse_private_message(payload: dict) -> ChatEvent:
    user_id = _string_id(payload.get("user_id") or "unknown")
    message_id = _string_id(payload.get("message_id") or uuid.uuid4().hex)
    timestamp = _onebot_timestamp(payload.get("time"))
    segments = _message_segments(payload)

    text_parts: list[str] = []
    has_user_text = False
    has_image = False
    has_sticker = False
    reply_to_message_id = None
    media_refs: list[dict] = []

    for segment in segments:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "").strip().lower()
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        if seg_type == "text":
            text = str(data.get("text") or "")
            if text:
                has_user_text = True
                text_parts.append(text)
            continue

        if seg_type == "reply":
            reply_to_message_id = _string_id(data.get("id") or data.get("message_id") or "")
            continue

        if seg_type == "face":
            has_sticker = True
            face_id = _string_id(data.get("id") or "")
            text_parts.append(f"[QQ表情: face:{face_id}]" if face_id else "[QQ表情]")
            continue

        if seg_type in ("image", "mface"):
            is_sticker = _is_sticker_image(seg_type, data)
            has_sticker = has_sticker or is_sticker
            has_image = has_image or not is_sticker
            text_parts.append("[表情]" if is_sticker else "[图片]")
            media_refs.append({
                "segment_type": seg_type,
                "sub_type": data.get("sub_type"),
                "summary": data.get("summary"),
                "file": data.get("file"),
                "url": data.get("url"),
                "file_size": data.get("file_size"),
                "is_sticker": is_sticker,
            })
            continue

        if seg_type:
            text_parts.append(f"[{seg_type}]")

    text = "".join(text_parts).strip()
    if has_user_text:
        event_type = EventType.TEXT
    elif has_sticker:
        event_type = EventType.STICKER
    elif has_image:
        event_type = EventType.IMAGE
    else:
        event_type = EventType.TEXT
        text = text or str(payload.get("raw_message") or "")

    return ChatEvent(
        event_id=f"onebot_msg_{message_id}",
        platform="qq",
        user_id=user_id,
        event_type=event_type,
        text=text,
        timestamp=timestamp,
        raw={
            "source": "onebot11",
            "qq_user_id": user_id,
            "onebot_message_id": message_id,
            "reply_to_message_id": reply_to_message_id,
            "message_segments": segments,
            "media_refs": media_refs,
            "onebot": payload,
        },
    )


def _message_segments(payload: dict) -> list[dict]:
    message = payload.get("message")
    if isinstance(message, list):
        return message
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": payload.get("raw_message") or message}}]
    raw_message = payload.get("raw_message")
    if isinstance(raw_message, str) and raw_message:
        return [{"type": "text", "data": {"text": raw_message}}]
    return []


def _is_sticker_image(seg_type: str, data: dict) -> bool:
    if seg_type == "mface":
        return True
    summary = str(data.get("summary") or "")
    sub_type = str(data.get("sub_type") or "")
    return sub_type == "1" or "表情" in summary


def _string_id(value) -> str:
    if value is None:
        return ""
    return str(value)


def _onebot_input_status_event_type(value) -> int:
    if value is None:
        return 1
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _onebot_timestamp(value) -> datetime:
    try:
        return datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError):
        return datetime.now()
