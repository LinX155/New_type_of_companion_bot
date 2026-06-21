import asyncio
import uuid
from typing import Optional, Callable, Awaitable
from datetime import datetime, timedelta

from .buffer import MessageBuffer
from .snapshots import SnapshotManager
from .state import SessionState, ChatStatus, ConversationSnapshot
from .events import ChatEvent, EventType
from .decisions import ActionDecision


class EventGate:
    def __init__(
        self,
        on_decision: Callable[["ProcessContext"], Awaitable[None]],
        hot_duration_minutes: int = 30,
        session_id: str = "default",
    ):
        self.session_id = session_id
        self.buffer = MessageBuffer()
        self.snapshot_manager = SnapshotManager(session_id=session_id)
        self.state = SessionState(session_id=session_id)
        self.on_decision = on_decision
        self.hot_duration_minutes = hot_duration_minutes
        self._pending_job_lock = asyncio.Lock()
        self._pending_job_id: Optional[str] = None
        self._stale_job_ids: set = set()
        self._sent_job_ids: set = set()
        self._sent_send_keys: set = set()
        self._last_decision: Optional[ActionDecision] = None
        self._last_snapshot_result: Optional[str] = None
        self._last_send_meta: Optional[dict] = None
        self._last_command: Optional[str] = None
        self._last_command_result: Optional[str] = None
        self._last_process_context: Optional[ProcessContext] = None
        self._user_composing_until: Optional[datetime] = None
        self._last_composing_event: Optional[dict] = None

    async def handle_event(self, event: ChatEvent) -> dict:
        """处理进入的事件，返回处理结果摘要"""
        # 命令分流
        if event.event_type == EventType.COMMAND_MEM:
            return {"handled": True, "type": "command_mem", "text": event.text}
        if event.event_type == EventType.COMMAND_FORGET:
            return {"handled": True, "type": "command_forget", "text": event.text}

        # 用户正在输入 / 正在组织下一条消息：只影响发送权，不进入聊天 buffer。
        if event.event_type == EventType.USER_COMPOSING:
            return await self._handle_user_composing(event)

        # 撤回处理
        if event.event_type == EventType.RECALL:
            raw = event.raw or {}
            new_version = await self.buffer.remove_by_event_id(raw.get("recalled_event_id", ""))
            self.state.buffer_version = new_version
            self._mark_current_job_stale()
            return {"handled": True, "type": "recall", "buffer_version": new_version}

        # 拍一拍 / 戳一戳
        if event.event_type == EventType.NUDGE:
            return await self._handle_nudge(event)

        # 普通消息
        if event.event_type in (EventType.TEXT, EventType.IMAGE, EventType.STICKER):
            return await self._handle_chat_message(event)

        return {"handled": False, "type": "unknown"}

    async def _handle_nudge(self, event: ChatEvent) -> dict:
        """拍一拍强制刷新注意力"""
        self.clear_user_composing(event, reason="nudge_received")
        if not event.text:
            event.text = "拍了拍你"

        self.state.last_user_message_at = event.timestamp
        # 如果当前是 COLD，进入 HOT
        if self.state.status != ChatStatus.HOT:
            self.state.status = ChatStatus.HOT
        self._refresh_hot_timer()
        new_version = await self.buffer.append(event)
        events, version = await self.buffer.get_snapshot()
        self.state.buffer_version = version

        async with self._pending_job_lock:
            has_pending = self._pending_job_id is not None

        if has_pending:
            self._mark_current_job_stale()
            return {
                "handled": True,
                "type": "nudge",
                "status": self.state.status.value,
                "buffer_version": new_version,
                "pending": True,
            }

        snapshot = self.snapshot_manager.create_snapshot(
            events=events,
            buffer_version=version,
            status=self.state.status,
            msg_index=self.state.msg_index_today,
            last_message_age=self._get_last_message_age(),
            nudge_triggered=True,
        )
        self.state.current_snapshot_id = snapshot.snapshot_id

        await self._dispatch_llm(snapshot)

        return {
            "handled": True,
            "type": "nudge",
            "status": self.state.status.value,
            "snapshot_id": snapshot.snapshot_id,
        }

    async def _handle_chat_message(self, event: ChatEvent) -> dict:
        """处理普通聊天消息"""
        self.clear_user_composing(event, reason="message_received")
        await self.maybe_exit_hot()
        previous_user_message_at = self.state.last_user_message_at
        self.state.msg_index_today += 1
        self.state.last_user_message_at = event.timestamp
        last_message_age = self._format_message_age(previous_user_message_at, event.timestamp)

        # HOT 中继续互动才刷新热聊生命周期；COLD 普通消息由 LLM 决定是否进入 HOT。
        if self.state.status == ChatStatus.HOT:
            self._refresh_hot_timer()

        new_version = await self.buffer.append(event)
        self.state.buffer_version = new_version
        events, _ = await self.buffer.get_snapshot()

        # 检查当前是否有 pending job
        async with self._pending_job_lock:
            has_pending = self._pending_job_id is not None

        if has_pending:
            # 标记当前 job 为 stale
            self._mark_current_job_stale()
            return {
                "handled": True,
                "type": "buffered",
                "buffer_version": new_version,
                "pending": True,
            }

        # 没有 pending job，创建 snapshot 并启动 LLM
        snapshot = self.snapshot_manager.create_snapshot(
            events=events,
            buffer_version=new_version,
            status=self.state.status,
            msg_index=self.state.msg_index_today,
            last_message_age=last_message_age,
        )
        self.state.current_snapshot_id = snapshot.snapshot_id

        await self._dispatch_llm(snapshot)

        return {
            "handled": True,
            "type": "processed",
            "buffer_version": new_version,
            "snapshot_id": snapshot.snapshot_id,
        }

    async def _handle_user_composing(self, event: ChatEvent) -> dict:
        raw = event.raw or {}
        composing = self._parse_bool(raw.get("composing", True))
        ttl_ms = int(raw.get("ttl_ms") or 6000)
        ttl_ms = max(500, min(ttl_ms, 30000))
        now = datetime.now()

        if composing:
            self._user_composing_until = now + timedelta(milliseconds=ttl_ms)
        else:
            self._user_composing_until = None

        self._set_last_composing_event(
            event=event,
            composing=composing,
            ttl_ms=ttl_ms,
            reason="input_status",
        )
        return {
            "handled": True,
            "type": "user_composing",
            "composing": self.is_user_composing(),
            "until": self._user_composing_until.isoformat() if self._user_composing_until else None,
        }

    def _set_last_composing_event(
        self,
        event: ChatEvent,
        composing: bool,
        ttl_ms: int = 0,
        reason: str = "",
    ):
        self._last_composing_event = {
            "platform": event.platform,
            "user_id": event.user_id,
            "composing": composing,
            "ttl_ms": ttl_ms,
            "until": self._user_composing_until.isoformat() if self._user_composing_until else None,
        }
        if reason:
            self._last_composing_event["reason"] = reason

    def _parse_bool(self, value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off", "")
        return bool(value)

    async def _dispatch_llm(self, snapshot: ConversationSnapshot):
        """调度 LLM 处理 snapshot"""
        job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        async with self._pending_job_lock:
            self._pending_job_id = job_id

        # 构建上下文
        ctx = ProcessContext(
            gate=self,
            job_id=job_id,
            snapshot=snapshot,
        )
        self._last_process_context = ctx

        # 异步调用 LLM（不阻塞事件处理）
        asyncio.create_task(self._run_llm_task(ctx))

    async def dispatch_internal_followup(self, source: str, payload: dict) -> dict:
        """调度不进入用户 buffer 的内部补发，例如后台看图完成后的补话。"""
        source = source or "internal_followup"
        job_id = f"{source}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        async with self._pending_job_lock:
            if self._pending_job_id is not None:
                return {"dispatched": False, "reason": "pending_job", "pending_job_id": self._pending_job_id}
            self._pending_job_id = job_id

        _, version = await self.buffer.get_snapshot()
        self.state.buffer_version = version
        snapshot = self.snapshot_manager.create_snapshot(
            events=[],
            buffer_version=version,
            status=self.state.status,
            msg_index=self.state.msg_index_today,
            last_message_age=self._get_last_message_age(),
        )
        self.state.current_snapshot_id = snapshot.snapshot_id

        ctx = ProcessContext(
            gate=self,
            job_id=job_id,
            snapshot=snapshot,
            internal_source=source,
            internal_payload=payload,
            skip_buffer_clear=True,
            bypass_snapshot_stale=True,
            bypass_send_group_gate=True,
            bypass_user_composing_gate=True,
        )
        self._last_process_context = ctx
        asyncio.create_task(self._run_llm_task(ctx))
        return {"dispatched": True, "job_id": job_id, "snapshot_id": snapshot.snapshot_id}

    async def reserve_active_message_job(self) -> Optional[str]:
        """为主动消息占用一次发送权。

        主动消息不进入聊天 buffer，但生成期间仍需要占用 pending job，
        这样用户突然发消息时会把主动任务标记为 stale，避免撞车。
        """
        await self.maybe_exit_hot()
        async with self._pending_job_lock:
            if self._pending_job_id is not None:
                return None

            events, version = await self.buffer.get_snapshot()
            self.state.buffer_version = version
            if events or self.state.status != ChatStatus.COLD:
                return None

            job_id = f"active_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            self._pending_job_id = job_id
            return job_id

    async def can_accept_active_message(self) -> bool:
        await self.maybe_exit_hot()
        async with self._pending_job_lock:
            if self._pending_job_id is not None:
                return False

        events, version = await self.buffer.get_snapshot()
        self.state.buffer_version = version
        return not events and self.state.status == ChatStatus.COLD

    async def is_active_message_job_current(self, job_id: str) -> bool:
        if self.is_job_stale(job_id) or self.is_job_sent(job_id):
            return False

        async with self._pending_job_lock:
            if self._pending_job_id != job_id:
                return False

        events, version = await self.buffer.get_snapshot()
        self.state.buffer_version = version
        return not events and self.state.status == ChatStatus.COLD

    async def finish_active_message_job(self, job_id: str, result: str):
        async with self._pending_job_lock:
            if self._pending_job_id == job_id:
                self._pending_job_id = None

        if result == "sent":
            self.mark_job_sent(job_id)
        elif result == "stale_dropped":
            self.mark_job_stale_dropped(job_id)
        else:
            self.mark_job_dropped(job_id)

    async def _run_llm_task(self, ctx: "ProcessContext"):
        """运行 LLM 任务并处理结果"""
        try:
            await self.on_decision(ctx)
        except Exception as e:
            print(f"LLM task error: {e}")
        finally:
            async with self._pending_job_lock:
                if self._pending_job_id == ctx.job_id:
                    self._pending_job_id = None

    async def is_snapshot_current(self, ctx: "ProcessContext") -> bool:
        """检查当前结果是否仍然拥有发送权。"""
        if self.is_job_stale(ctx.job_id) or self.is_job_sent(ctx.job_id):
            return False
        _, current_version = await self.buffer.get_snapshot()
        self.state.buffer_version = current_version
        return current_version == ctx.snapshot.buffer_version

    async def clear_buffer_after_visible_send(self, expected_version: int) -> tuple[bool, int]:
        """有效可见回复发送后，清空已回应的 pending buffer。"""
        cleared, new_version = await self.buffer.clear_if_version(expected_version)
        self.state.buffer_version = new_version
        return cleared, new_version

    async def is_send_group_current(self, ctx: "ProcessContext", expected_buffer_version: int) -> bool:
        """检查同一轮多气泡发送期间是否仍然拥有发送权。"""
        if self.is_job_stale(ctx.job_id) or self.is_job_sent(ctx.job_id):
            return False
        _, current_version = await self.buffer.get_snapshot()
        self.state.buffer_version = current_version
        return current_version == expected_buffer_version

    def is_user_composing(self) -> bool:
        if not self._user_composing_until:
            return False
        if datetime.now() <= self._user_composing_until:
            return True
        self._user_composing_until = None
        if self._last_composing_event and self._last_composing_event.get("composing"):
            self._last_composing_event = {
                **self._last_composing_event,
                "composing": False,
                "until": None,
                "reason": "expired",
            }
        return False

    def clear_user_composing(self, event: Optional[ChatEvent] = None, reason: str = "cleared"):
        self._user_composing_until = None
        if event:
            self._set_last_composing_event(
                event=event,
                composing=False,
                ttl_ms=0,
                reason=reason,
            )

    def get_user_composing_meta(self) -> dict:
        return {
            "active": self.is_user_composing(),
            "until": self._user_composing_until.isoformat() if self._user_composing_until else None,
            "last_event": self._last_composing_event,
        }

    async def dispatch_latest_after_stale(self, stale_job_id: str):
        """旧 job 作废后，如仍有未回应输入，用最新 buffer 重新启动一轮。"""
        async with self._pending_job_lock:
            if self._pending_job_id == stale_job_id:
                self._pending_job_id = None
            has_pending = self._pending_job_id is not None

        if has_pending:
            return

        events, version = await self.buffer.get_snapshot()
        self.state.buffer_version = version
        if not events:
            return

        snapshot = self.snapshot_manager.create_snapshot(
            events=events,
            buffer_version=version,
            status=self.state.status,
            msg_index=self.state.msg_index_today,
            last_message_age=self._get_last_message_age(),
        )
        self.state.current_snapshot_id = snapshot.snapshot_id
        await self._dispatch_llm(snapshot)

    def mark_job_sent(self, job_id: str):
        """标记 job 已发送"""
        self._sent_job_ids.add(job_id)
        self._last_snapshot_result = "sent"

    def mark_job_dropped(self, job_id: str):
        """标记 job 被丢弃"""
        self._last_snapshot_result = "dropped"

    def mark_job_stale_dropped(self, job_id: str):
        """标记 job 因 stale 被丢弃"""
        self._stale_job_ids.add(job_id)
        self._last_snapshot_result = "stale_dropped"

    def mark_job_deferred(self, job_id: str, reason: str = "deferred_user_composing"):
        """标记 job 已生成但因发送门禁暂缓。"""
        self._last_snapshot_result = reason

    def _mark_current_job_stale(self):
        """标记当前 pending job 为 stale"""
        if self._pending_job_id:
            self._stale_job_ids.add(self._pending_job_id)

    def is_job_stale(self, job_id: str) -> bool:
        return job_id in self._stale_job_ids

    def is_job_sent(self, job_id: str) -> bool:
        return job_id in self._sent_job_ids

    def build_send_key(self, ctx: "ProcessContext", send_index: int) -> str:
        return f"{ctx.job_id}:{ctx.snapshot.snapshot_id}:{send_index}"

    def is_send_key_sent(self, send_key: str) -> bool:
        return send_key in self._sent_send_keys

    def mark_send_key_sent(self, send_key: str):
        self._sent_send_keys.add(send_key)

    def record_send_meta(self, ctx: "ProcessContext", send_index: int, send_count: int, item_type: Optional[str] = None):
        self._last_send_meta = {
            "job_id": ctx.job_id,
            "snapshot_id": ctx.snapshot.snapshot_id,
            "send_index": send_index,
            "send_count": send_count,
            "item_type": item_type,
        }

    def get_last_send_meta(self) -> Optional[dict]:
        return self._last_send_meta

    def update_hot_duration(self, minutes: int):
        """更新热聊持续时间"""
        self.hot_duration_minutes = max(1, minutes)
        # 如果当前处于 HOT，从用户最后消息时间重新计算
        if self.state.status == ChatStatus.HOT:
            self._refresh_hot_timer()

    def _refresh_hot_timer(self):
        """刷新 HOT 计时器：从用户最后一条消息时间开始计时"""
        if self.state.last_user_message_at:
            self.state.hot_until = self.state.last_user_message_at + timedelta(minutes=self.hot_duration_minutes)
        else:
            self.state.hot_until = datetime.now() + timedelta(minutes=self.hot_duration_minutes)

    async def _enter_hot(self):
        """进入热聊状态（由 LLM 决策触发）"""
        self.state.status = ChatStatus.HOT
        self._refresh_hot_timer()

    async def _exit_hot(self):
        """结束热聊状态，回到离线生活态。"""
        self.state.status = ChatStatus.COLD
        self.state.hot_until = None

    async def maybe_exit_hot(self):
        """检查是否应该退出热聊状态"""
        if self.state.status == ChatStatus.HOT and self.state.hot_until:
            if datetime.now() > self.state.hot_until:
                await self._exit_hot()

    def _get_last_message_age(self) -> Optional[str]:
        if not self.state.last_user_message_at:
            return None
        return self._format_message_age(self.state.last_user_message_at, datetime.now())

    def _format_message_age(self, then: Optional[datetime], now: datetime) -> Optional[str]:
        if not then:
            return None
        delta = now - then
        minutes = int(delta.total_seconds() / 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"

    def record_decision(self, decision: ActionDecision):
        self._last_decision = decision

    def get_last_decision(self) -> Optional[ActionDecision]:
        return self._last_decision

    def get_last_snapshot_result(self) -> Optional[str]:
        return self._last_snapshot_result

    def record_command(self, command: str, result: str):
        self._last_command = command
        self._last_command_result = result

    def get_last_command(self) -> Optional[str]:
        return self._last_command

    def get_last_command_result(self) -> Optional[str]:
        return self._last_command_result


class ProcessContext:
    def __init__(
        self,
        gate: EventGate,
        job_id: str,
        snapshot: ConversationSnapshot,
        internal_source: Optional[str] = None,
        internal_payload: Optional[dict] = None,
        skip_buffer_clear: bool = False,
        bypass_snapshot_stale: bool = False,
        bypass_send_group_gate: bool = False,
        bypass_user_composing_gate: bool = False,
    ):
        self.gate = gate
        self.job_id = job_id
        self.snapshot = snapshot
        self.internal_source = internal_source
        self.internal_payload = internal_payload
        self.skip_buffer_clear = skip_buffer_clear
        self.bypass_snapshot_stale = bypass_snapshot_stale
        self.bypass_send_group_gate = bypass_send_group_gate
        self.bypass_user_composing_gate = bypass_user_composing_gate
        self.decision: Optional[ActionDecision] = None
        self.meme_candidates: list = []
        self.selected_meme: Optional[str] = None
        self.selected_memes: list[str] = []
        self.meme_search_used: bool = False
        self.missing_meme_repair_used: bool = False
        self.contextual_repair_used: bool = False
