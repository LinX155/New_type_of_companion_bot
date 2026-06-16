from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


class ChatStatus(str, Enum):
    COLD = "COLD"
    HOT = "HOT"


class ColdStartMeta(BaseModel):
    timestamp: str
    last_user_message_age: Optional[str] = None
    msg_index: int = 1
    status: ChatStatus = ChatStatus.COLD


class ConversationSnapshot(BaseModel):
    session_id: str = "default"
    snapshot_id: int = 0
    buffer_version: int = 0
    status: ChatStatus = ChatStatus.COLD
    events: List[Dict[str, Any]] = Field(default_factory=list)
    cold_start_meta: Optional[ColdStartMeta] = None
    nudge_triggered: bool = False


class SessionState(BaseModel):
    session_id: str = "default"
    status: ChatStatus = ChatStatus.COLD
    buffer_version: int = 0
    current_snapshot_id: int = 0
    pending_job_id: Optional[str] = None
    hot_until: Optional[datetime] = None
    msg_index_today: int = 0
    last_user_message_at: Optional[datetime] = None
    daily_active_message_count: int = 0
