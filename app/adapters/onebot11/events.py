from datetime import datetime
from typing import Optional

from ...core.events import ChatEvent, EventType


def parse_onebot_event(payload: dict, default_input_ttl_ms: int = 8000) -> Optional[ChatEvent]:
    """Convert a small subset of OneBot11 events into internal ChatEvent objects.

    This is intentionally narrow for now. The first QQ-facing signal we need is
    NapCat's input_status notice, which maps to USER_COMPOSING.
    """
    if not isinstance(payload, dict):
        return None

    if _is_input_status_notice(payload):
        user_id = str(payload.get("user_id") or "unknown")
        event_type = int(payload.get("event_type") or 1)
        timestamp = _onebot_timestamp(payload.get("time"))
        composing = event_type != 0
        return ChatEvent(
            event_id=f"onebot_input_status_{user_id}_{int(timestamp.timestamp() * 1000)}",
            platform="qq",
            user_id=user_id,
            event_type=EventType.USER_COMPOSING,
            timestamp=timestamp,
            raw={
                "composing": composing,
                "ttl_ms": default_input_ttl_ms,
                "source": "onebot11",
                "onebot": payload,
            },
        )

    return None


def _is_input_status_notice(payload: dict) -> bool:
    return (
        payload.get("post_type") == "notice"
        and payload.get("notice_type") == "notify"
        and payload.get("sub_type") == "input_status"
    )


def _onebot_timestamp(value) -> datetime:
    try:
        return datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError):
        return datetime.now()
