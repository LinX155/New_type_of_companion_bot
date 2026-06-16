from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, create_engine
from sqlalchemy.sql import func
from .db import Base


class RawChatLog(Base):
    __tablename__ = "raw_chat_logs"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String, index=True)
    session_id = Column(String, index=True, default="default")
    event_type = Column(String)
    platform = Column(String, default="webui")
    user_id = Column(String, default="user")
    input_text = Column(Text, nullable=True)
    llm_raw_output = Column(Text, nullable=True)
    action = Column(String, nullable=True)
    final_text = Column(Text, nullable=True)
    snapshot_id = Column(Integer, nullable=True)
    buffer_version = Column(Integer, nullable=True)
    job_id = Column(String, nullable=True)
    status = Column(String, nullable=True)  # sent, dropped, stale_dropped, error
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ConversationEvent(Base):
    __tablename__ = "conversation_events"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, index=True, default="default")
    event_type = Column(String)  # user_text, assistant_text, nudge, react, etc.
    text = Column(Text, nullable=True)
    is_visible = Column(Boolean, default=True)
    action = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ScheduledJobLog(Base):
    __tablename__ = "scheduled_job_logs"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(String, index=True)
    job_type = Column(String)  # memory_analysis, midnight_cleanup, active_message
    status = Column(String)  # running, completed, failed
    start_time = Column(DateTime(timezone=True))
    end_time = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SystemConfig(Base):
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True)
    value = Column(Text)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
