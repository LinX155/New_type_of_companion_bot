import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime
from .events import ChatEvent, EventType


class MessageBuffer:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._events: List[Dict[str, Any]] = []
        self._version: int = 0

    async def append(self, event: ChatEvent) -> int:
        async with self._lock:
            self._events.append({
                "event_id": event.event_id,
                "platform": event.platform,
                "user_id": event.user_id,
                "event_type": event.event_type.value,
                "text": event.text,
                "timestamp": event.timestamp.isoformat(),
                "raw": event.raw,
            })
            self._version += 1
            return self._version

    async def remove_by_event_id(self, event_id: str) -> int:
        async with self._lock:
            self._events = [e for e in self._events if e.get("event_id") != event_id]
            self._version += 1
            return self._version

    async def get_snapshot(self) -> tuple[List[Dict[str, Any]], int]:
        async with self._lock:
            return list(self._events), self._version

    async def clear(self) -> int:
        async with self._lock:
            self._events.clear()
            self._version += 1
            return self._version

    async def clear_if_version(self, expected_version: int) -> tuple[bool, int]:
        async with self._lock:
            if self._version != expected_version:
                return False, self._version
            self._events.clear()
            self._version += 1
            return True, self._version

    async def mark_responded(self) -> int:
        async with self._lock:
            # 保留 events 但标记为已回应，实际实现中可以选择清空或保留历史
            # 这里保留历史用于上下文
            self._version += 1
            return self._version
