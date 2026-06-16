from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel
from datetime import datetime


class EventType(str, Enum):
    TEXT = "message.text"
    IMAGE = "message.image"
    STICKER = "message.sticker"
    NUDGE = "nudge"
    RECALL = "recall"
    COMMAND_MEM = "command.mem"
    COMMAND_FORGET = "command.forget"


class ChatEvent(BaseModel):
    event_id: str
    platform: str = "webui"
    user_id: str = "user"
    event_type: EventType
    text: Optional[str] = None
    timestamp: datetime
    raw: Optional[dict] = None
