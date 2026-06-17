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
    ):
        self.buffer = MessageBuffer()
        self.snapshot_manager = SnapshotManager()
        self.state = SessionState()
        self.on_decision = on_decision
        self.hot_duration_minutes = hot_duration_minutes
        self._pending_job_lock = asyncio.Lock()
        self._pending_job_id: Optional[str] = None
        self._stale_job_ids: set = set()
        self._sent_job_ids: set = set()
        self._last_decision: Optional[ActionDecision] = None
        self._last_snapshot_result: Optional[str] = None
        self._last_command: Optional[str] = None
        self._last_command_result: Optional[str] = None
        self._last_process_context: Optional[ProcessContext] = None
        self._last_command: Optional[str] = None
        self._last_command_result: Optional[str] = None

    async def handle_event(self, event: ChatEvent) -> dict:
        """处理进入的事件，返回处理结果摘要"""
        # 命令分流
        if event.event_type == EventType.COMMAND_MEM:
            return {"handled": True, "type": "command_mem", "text": event.text}
        if event.event_type == EventType.COMMAND_FORGET:
            return {"handled": True, "type": "command_forget", "text": event.text}

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
        self.state.last_user_message_at = datetime.now()
        # 如果当前是 COLD，进入 HOT
        if self.state.status != ChatStatus.HOT:
            self.state.status = ChatStatus.HOT
        self._refresh_hot_timer()
        events, version = await self.buffer.get_snapshot()
        self.state.buffer_version = version
        snapshot = self.snapshot_manager.create_snapshot(
            events=events,
            buffer_version=version,
            status=self.state.status,
            msg_index=self.state.msg_index_today,
            last_message_age=self._get_last_message_age(),
            nudge_triggered=True,
        )
        self.state.current_snapshot_id = snapshot.snapshot_id

        # 如果有未处理消息，立即触发 LLM
        if events:
            await self._dispatch_llm(snapshot)

        return {
            "handled": True,
            "type": "nudge",
            "status": self.state.status.value,
            "snapshot_id": snapshot.snapshot_id,
        }

    async def _handle_chat_message(self, event: ChatEvent) -> dict:
        """处理普通聊天消息"""
        await self.maybe_exit_hot()
        self.state.msg_index_today += 1
        self.state.last_user_message_at = event.timestamp

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
            last_message_age=self._get_last_message_age(),
        )
        self.state.current_snapshot_id = snapshot.snapshot_id

        await self._dispatch_llm(snapshot)

        return {
            "handled": True,
            "type": "processed",
            "buffer_version": new_version,
            "snapshot_id": snapshot.snapshot_id,
        }

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

    async def clear_buffer_after_visible_send(self, expected_version: int) -> bool:
        """有效可见回复发送后，清空已回应的 pending buffer。"""
        cleared, new_version = await self.buffer.clear_if_version(expected_version)
        self.state.buffer_version = new_version
        return cleared

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

    def _mark_current_job_stale(self):
        """标记当前 pending job 为 stale"""
        if self._pending_job_id:
            self._stale_job_ids.add(self._pending_job_id)

    def is_job_stale(self, job_id: str) -> bool:
        return job_id in self._stale_job_ids

    def is_job_sent(self, job_id: str) -> bool:
        return job_id in self._sent_job_ids

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

    async def maybe_exit_hot(self):
        """检查是否应该退出热聊状态"""
        if self.state.status == ChatStatus.HOT and self.state.hot_until:
            if datetime.now() > self.state.hot_until:
                self.state.status = ChatStatus.COLD
                self.state.hot_until = None

    def _get_last_message_age(self) -> Optional[str]:
        if not self.state.last_user_message_at:
            return None
        delta = datetime.now() - self.state.last_user_message_at
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
    def __init__(self, gate: EventGate, job_id: str, snapshot: ConversationSnapshot):
        self.gate = gate
        self.job_id = job_id
        self.snapshot = snapshot
        self.decision: Optional[ActionDecision] = None
        self.meme_candidates: list = []
        self.selected_meme: Optional[str] = None
        self.meme_search_used: bool = False
