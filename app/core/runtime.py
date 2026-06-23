import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Optional

from .event_gate import EventGate
from .graph import CompanionGraph
from .media_jobs import MediaJobQueue
from .sessions import SessionRegistry, normalize_session_id
from .state import ChatStatus
from ..llm.client import LLMClient
from ..memory.files import MemoryFileManager
from ..memes.catalog import MemeCatalog


@dataclass
class SessionRuntime:
    session_id: str
    gate: EventGate
    graph: CompanionGraph
    memory: MemoryFileManager


class SessionRuntimeManager:
    def __init__(
        self,
        *,
        root_dir: str,
        base_llm_client: LLMClient,
        base_memory_manager: MemoryFileManager,
        meme_catalog: MemeCatalog,
        media_job_queue: Optional[MediaJobQueue],
        on_decision: Callable,
        hot_duration_minutes: int,
    ):
        self.root_dir = root_dir
        self.base_llm_client = base_llm_client
        self.base_memory_manager = base_memory_manager
        self.meme_catalog = meme_catalog
        self.media_job_queue = media_job_queue
        self.on_decision = on_decision
        self.hot_duration_minutes = hot_duration_minutes
        self.registry = SessionRegistry(root_dir)
        self._runtimes: dict[str, SessionRuntime] = {}

    def get(self, session_id: str) -> SessionRuntime:
        sid = normalize_session_id(session_id)
        if sid not in self._runtimes:
            self.registry.ensure_session(sid)
            memory = self.base_memory_manager.for_session(sid)
            gate = EventGate(
                on_decision=self.on_decision,
                hot_duration_minutes=self.hot_duration_minutes,
                session_id=sid,
            )
            graph = CompanionGraph(
                llm_client=self._make_session_llm(sid),
                memory_manager=memory,
                meme_catalog=self.meme_catalog,
                media_job_queue=self.media_job_queue,
            )
            self._runtimes[sid] = SessionRuntime(
                session_id=sid,
                gate=gate,
                graph=graph,
                memory=memory,
            )
        return self._runtimes[sid]

    def get_if_exists(self, session_id: str) -> Optional[SessionRuntime]:
        return self._runtimes.get(normalize_session_id(session_id))

    def active_session_ids(self) -> list[str]:
        return sorted(self._runtimes)

    def update_hot_duration(self, minutes: int):
        self.hot_duration_minutes = max(1, int(minutes or 1))
        for runtime in self._runtimes.values():
            runtime.gate.update_hot_duration(self.hot_duration_minutes)

    def reconfigure_llm_clients(self, reason: str = "llm_config_changed"):
        for sid, runtime in self._runtimes.items():
            runtime.graph.llm = self._make_session_llm(sid)
            runtime.graph.reset_provider_transcript(reason=reason)

    def make_llm_for_session(self, session_id: str) -> LLMClient:
        return self._make_session_llm(normalize_session_id(session_id))

    async def clear_session(self, session_id: str):
        sid = normalize_session_id(session_id)
        runtime = self.get(sid)
        gate = runtime.gate
        new_version = await gate.buffer.clear()
        gate.state.status = ChatStatus.COLD
        gate.state.buffer_version = new_version
        gate.state.msg_index_today = 0
        gate.state.hot_until = None
        gate.state.pending_job_id = None
        gate._pending_job_id = None
        gate._stale_job_ids.clear()
        gate._sent_job_ids.clear()
        gate._sent_send_keys.clear()
        gate._last_send_meta = None
        gate._last_decision = None
        gate._last_snapshot_result = None
        gate._last_command = None
        gate._last_command_result = None
        gate._last_process_context = None
        gate.clear_user_composing()
        runtime.graph.clear_prompt_state()
        if self.media_job_queue:
            self.media_job_queue.clear(sid)

    def drop_session_runtime(self, session_id: str):
        self._runtimes.pop(normalize_session_id(session_id), None)

    def _make_session_llm(self, session_id: str) -> LLMClient:
        base = self.base_llm_client
        return LLMClient(
            api_key=base.api_key,
            base_url=base.base_url,
            model=base.model,
            thinking_enabled=base.thinking_enabled,
            temperature=base.temperature,
            cache_affinity_enabled=base.cache_affinity_enabled,
            cache_session_id=self._cache_session_id(session_id),
            provider_user_id=self.registry.provider_user_id(session_id),
        )

    def _cache_session_id(self, session_id: str) -> str:
        payload = {
            "session_id": session_id,
            "base_url": self.base_llm_client.base_url,
            "model": self.base_llm_client.model,
            "thinking_enabled": self.base_llm_client.thinking_enabled,
            "temperature": self.base_llm_client.temperature,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"chat_{digest}"
