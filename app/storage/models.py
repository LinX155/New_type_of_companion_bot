from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, text
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
    raw_payload = Column(Text, nullable=True)
    llm_raw_output = Column(Text, nullable=True)
    parsed_payload = Column(Text, nullable=True)
    action = Column(String, nullable=True)
    final_text = Column(Text, nullable=True)
    item_type = Column(String, nullable=True)
    send_index = Column(Integer, nullable=True)
    send_count = Column(Integer, nullable=True)
    send_key = Column(String, nullable=True)
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
    session_id = Column(String, index=True, default="default")
    job_type = Column(String)  # memory_analysis, midnight_cleanup, active_message
    status = Column(String)  # running, completed, failed
    start_time = Column(DateTime(timezone=True))
    end_time = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ContextCheckpoint(Base):
    __tablename__ = "context_checkpoints"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, index=True, default="default")
    checkpoint_text = Column(Text, nullable=False)
    covered_until_event_id = Column(Integer, nullable=True)
    source_prompt_debug_id = Column(Integer, nullable=True)
    estimated_tokens_before = Column(Integer, nullable=True)
    prompt_tokens_before = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SystemConfig(Base):
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True)
    value = Column(Text)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


def ensure_storage_schema(engine):
    """Lightweight SQLite column backfill for MVP local databases.

    Alembic is still a planned storage cleanup. Until then, existing local
    SQLite files need nullable audit columns added without dropping data.
    """
    if not str(engine.url).startswith("sqlite"):
        return

    with engine.begin() as conn:
        raw_expected = {
            "raw_payload": "TEXT",
            "parsed_payload": "TEXT",
            "item_type": "VARCHAR",
            "send_index": "INTEGER",
            "send_count": "INTEGER",
            "send_key": "VARCHAR",
        }
        rows = conn.execute(text("PRAGMA table_info(raw_chat_logs)")).fetchall()
        existing = {row[1] for row in rows}
        for column, column_type in raw_expected.items():
            if column not in existing:
                conn.execute(text(f"ALTER TABLE raw_chat_logs ADD COLUMN {column} {column_type}"))

        scheduled_expected = {
            "session_id": "VARCHAR",
        }
        rows = conn.execute(text("PRAGMA table_info(scheduled_job_logs)")).fetchall()
        existing = {row[1] for row in rows}
        for column, column_type in scheduled_expected.items():
            if column not in existing:
                conn.execute(text(f"ALTER TABLE scheduled_job_logs ADD COLUMN {column} {column_type}"))
