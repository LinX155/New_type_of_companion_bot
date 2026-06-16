from typing import Optional
from datetime import datetime
from .state import ConversationSnapshot, ChatStatus, ColdStartMeta
from .events import ChatEvent


class SnapshotManager:
    def __init__(self):
        self._current_id = 0

    def create_snapshot(
        self,
        events: list,
        buffer_version: int,
        status: ChatStatus,
        msg_index: int,
        last_message_age: Optional[str] = None,
        nudge_triggered: bool = False,
    ) -> ConversationSnapshot:
        self._current_id += 1
        snapshot_id = self._current_id

        cold_start_meta = None
        if status == ChatStatus.COLD:
            cold_start_meta = ColdStartMeta(
                timestamp=datetime.now().strftime("%H:%M"),
                last_user_message_age=last_message_age,
                msg_index=msg_index,
                status=status,
            )

        return ConversationSnapshot(
            session_id="default",
            snapshot_id=snapshot_id,
            buffer_version=buffer_version,
            status=status,
            events=events,
            cold_start_meta=cold_start_meta,
            nudge_triggered=nudge_triggered,
        )
