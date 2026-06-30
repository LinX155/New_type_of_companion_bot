import os
import asyncio
import copy
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import desc, func

from ..adapters.onebot11 import OneBotConnectionManager, OneBotMediaDownloader, parse_onebot_event
from ..active.messages import (
    count_candidates_by_status,
    expire_stale_candidates,
    mark_candidate_status,
    parse_active_decision,
    select_pending_candidate,
)
from ..active.settings import (
    ACTIVE_SETTING_NONE,
    fallback_active_message_setting_from_text,
    format_active_message_setting_response,
    looks_like_active_message_setting_request,
    normalize_active_message_setting,
    parse_active_message_setting_output,
)
from ..core.event_gate import EventGate, ProcessContext, USER_COMPOSING_MAX_BLOCK_SECONDS
from ..core.events import ChatEvent, EventType
from ..core.decisions import Action, ActionDecision, SendItem, SendItemType
from ..core.graph import CompanionGraph
from ..core.group_buffer import GroupChatBuffer
from ..core.group_activity import GroupActivityTracker
from ..core.group_chat import (
    classify_group_visible_text,
    group_chat_runtime_status,
    known_message_ids_from_group_window,
    known_qids_from_group_window,
    parse_group_chat_output,
)
from ..core.group_media import GroupImageUnderstandingCache, group_image_media_key
from ..core.group_memory import GroupMemoryManager
from ..core.group_repetition import GroupRepetitionDetector
from ..core.group_scheduler import GroupReplyScheduler
from ..core.group_send import (
    GroupSendLimiter,
    evaluate_group_send_policy,
    group_id_from_session_id,
    group_send_policy_for_session,
    normalize_group_policy,
    normalize_group_send_config,
)
from ..core.media_jobs import MediaJob, MediaJobQueue
from ..core.protocol import (
    classify_visible_text,
)
from ..core.repetition_guard import (
    RECENT_REPETITION_WINDOW,
    RepetitionRemoval,
    build_repetition_guard_system_reminder,
    filter_recent_repeated_reactions,
)
from ..core.runtime import SessionRuntimeManager, SessionRuntime
from ..core.sessions import (
    WEBUI_DEFAULT_SESSION_ID,
    SessionRegistry,
    identity_for_webui,
    infer_identity,
    normalize_session_id,
    webui_session_id,
)
from ..core.state import ChatStatus
from ..core.settings import load_settings, save_settings
from ..llm.client import LLMClient
from ..llm.prompts import (
    build_active_message_messages,
    build_active_message_setting_messages,
    build_group_chat_messages,
)
from ..memory.files import MemoryFileManager, TOMORROW_TOPICS_TEMPLATE
from ..memes.catalog import MemeCatalog
from ..memes.steal import MemeStealAnalyzer, MemeStealSaver
from ..scheduler.jobs import SchedulerManager
from ..storage.context_checkpoints import (
    load_conversation_context,
    normalize_context_checkpoint_config,
)
from ..storage.db import get_db, engine
from ..storage.models import Base, ContextCheckpoint, RawChatLog, ConversationEvent, ensure_storage_schema

Base.metadata.create_all(bind=engine)
ensure_storage_schema(engine)

router = APIRouter()

# 全局实例（MVP 阶段简化）
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_SESSION_ID = WEBUI_DEFAULT_SESSION_ID
TEMPERATURE_MIN = 0.0
DEEPSEEK_TEMPERATURE_MAX = 2.0
MIMO_TEMPERATURE_MAX = 1.5
MINIMAX_TEMPERATURE_MAX = 2.0
KIMI_TEMPERATURE_MAX = 1.0
DEFAULT_TEMPERATURE = 1.0
DISPLAY_SPLIT_DISABLE_ITEM_COUNT = 4
DISPLAY_SPLIT_MIN_CHINESE_CHARS = 8
DISPLAY_SPLIT_TRIGGERS = ("...", "——", "？", "，", ",")
MIMO_WEB_SEARCH_MODES = {"off", "adaptive", "force"}
DEFAULT_MIMO_WEB_SEARCH_MODE = "off"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _temperature_max_for_provider(base_url: str, model: str) -> float:
    if "kimi" in str(model or "").lower():
        return KIMI_TEMPERATURE_MAX
    identity = f"{base_url or ''} {model or ''}".lower()
    if "minimax" in identity or "minimaxi" in identity:
        return MINIMAX_TEMPERATURE_MAX
    if "xiaomimimo" in identity or "mimo" in identity:
        return MIMO_TEMPERATURE_MAX
    if "deepseek" in identity:
        return DEEPSEEK_TEMPERATURE_MAX
    return DEEPSEEK_TEMPERATURE_MAX


def _normalize_temperature_for_provider(value, base_url: str, model: str) -> float:
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        temperature = DEFAULT_TEMPERATURE
    if temperature != temperature:
        temperature = DEFAULT_TEMPERATURE
    max_temperature = _temperature_max_for_provider(base_url, model)
    return max(TEMPERATURE_MIN, min(max_temperature, temperature))


def _normalize_mimo_web_search_mode(value) -> str:
    mode = str(value or DEFAULT_MIMO_WEB_SEARCH_MODE).strip().lower()
    if mode in {"true", "on", "enabled", "enable"}:
        return "adaptive"
    if mode in {"false", "none", "disabled", "disable"}:
        return "off"
    return mode if mode in MIMO_WEB_SEARCH_MODES else DEFAULT_MIMO_WEB_SEARCH_MODE


# NapCat exposes set_input_status.event_type as a raw number and its public docs
# do not list the enum. Keep the observed working code configurable so testing
# can continue without changing the adapter surface.
ONEBOT_LLM_INPUT_STATUS_EVENT_TYPE = _env_int("ONEBOT_LLM_INPUT_STATUS_EVENT_TYPE", 1)
ONEBOT_LLM_INPUT_STATUS_REFRESH_SECONDS = max(
    1.0,
    _env_float("ONEBOT_LLM_INPUT_STATUS_REFRESH_SECONDS", 3.0),
)
ONEBOT_LLM_INPUT_STATUS_MAX_SECONDS = max(
    ONEBOT_LLM_INPUT_STATUS_REFRESH_SECONDS,
    _env_float("ONEBOT_LLM_INPUT_STATUS_MAX_SECONDS", 120.0),
)
_onebot_input_status_tasks: dict[str, asyncio.Task] = {}

# 启动时加载持久化的用户配置（API key 等）
_persisted = load_settings()


def _load_default_llm_config() -> dict:
    """加载 LLM 默认配置：持久化配置 > api_key.txt > 环境变量。"""
    config = {
        "api_key": os.getenv("LLM_API_KEY", ""),
        "base_url": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
        "temperature": _env_float("LLM_TEMPERATURE", DEFAULT_TEMPERATURE),
    }
    path = os.path.join(ROOT_DIR, "api_key.txt")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            api_key_match = re.search(r"api_key\s*=\s*[\"']?([A-Za-z0-9_\-]+)", content)
            base_url_match = re.search(r"base_url\s*=\s*[\"']([^\"']+)", content)
            model_match = re.search(r"model\s*=\s*[\"']([^\"']+)", content)
            temperature_match = re.search(r"temperature\s*=\s*[\"']?([0-9]+(?:\.[0-9]+)?)", content)
            if api_key_match:
                config["api_key"] = api_key_match.group(1)
            if base_url_match:
                config["base_url"] = base_url_match.group(1)
            if model_match:
                config["model"] = model_match.group(1)
            if temperature_match:
                config["temperature"] = float(temperature_match.group(1))
        except Exception:
            pass

    # 持久化配置优先级最高：前端手动保存过的值覆盖上面
    if _persisted.get("api_key"):
        config["api_key"] = _persisted["api_key"]
    if _persisted.get("base_url"):
        config["base_url"] = _persisted["base_url"]
    if _persisted.get("model"):
        config["model"] = _persisted["model"]
    config["thinking_enabled"] = _persisted.get("thinking_enabled", True)
    config["mimo_web_search_mode"] = _normalize_mimo_web_search_mode(
        _persisted.get("mimo_web_search_mode")
        or os.getenv("MIMO_WEB_SEARCH_MODE")
        or os.getenv("LLM_MIMO_WEB_SEARCH_MODE")
        or os.getenv("LLM_MIMO_WEB_SEARCH")
    )
    if "temperature" in _persisted:
        config["temperature"] = _persisted.get("temperature")
    config["temperature"] = _normalize_temperature_for_provider(
        config.get("temperature"),
        config.get("base_url", ""),
        config.get("model", ""),
    )
    return config


llm_client = LLMClient(**_load_default_llm_config())
memory_manager = MemoryFileManager()
meme_catalog = MemeCatalog()
meme_steal_analyzer = MemeStealAnalyzer(meme_catalog, ROOT_DIR)
meme_steal_saver = MemeStealSaver(meme_catalog, ROOT_DIR)
scheduler_manager = SchedulerManager(memory_manager, llm_client)
onebot_manager = OneBotConnectionManager()
onebot_media_downloader = OneBotMediaDownloader(ROOT_DIR, onebot_manager)

companion_graph: Optional[CompanionGraph] = None
event_gate: Optional[EventGate] = None
media_job_queue: Optional[MediaJobQueue] = None
runtime_manager: Optional[SessionRuntimeManager] = None
group_chat_buffer = GroupChatBuffer()
group_image_cache = GroupImageUnderstandingCache()
group_memory_manager = GroupMemoryManager(ROOT_DIR)
group_repetition_detector = GroupRepetitionDetector()
group_activity_tracker = GroupActivityTracker()
group_send_limiter = GroupSendLimiter()
group_reply_scheduler: Optional[GroupReplyScheduler] = None
session_registry = SessionRegistry(ROOT_DIR)
_media_followup_keys: set[str] = set()

# Store for WebUI callbacks
message_callbacks = []

ACTIVE_MESSAGE_DEFAULTS = {
    "enabled": False,
    "hour": 10,
    "minute": 0,
    "daily_limit": 1,
    "sessions": {},
}
ACTIVE_MESSAGE_NEXT_FALLBACK_TEXT = "到你说的时间了，我来找你一下。"
ACTIVE_MESSAGE_NEXT_RETRY_DELAY_SECONDS = max(
    0.0,
    _env_float("ACTIVE_MESSAGE_NEXT_RETRY_DELAY_SECONDS", 180.0),
)
ACTIVE_MESSAGE_NEXT_LLM_RETRY_COUNT = max(
    0,
    _env_int("ACTIVE_MESSAGE_NEXT_LLM_RETRY_COUNT", 5),
)


class ApiConfig(BaseModel):
    api_key: str
    base_url: str
    model: str
    thinking_enabled: bool = True
    temperature: float = DEFAULT_TEMPERATURE
    mimo_web_search_mode: str = DEFAULT_MIMO_WEB_SEARCH_MODE


class ChatMessage(BaseModel):
    text: str
    session_id: Optional[str] = None


class OneBotDebugSendRequest(BaseModel):
    user_id: str
    text: str


class SoulConfig(BaseModel):
    content: str


class MemoryScheduleConfig(BaseModel):
    memory_analysis_day_hour: int
    memory_analysis_day_minute: int
    memory_analysis_night_hour: int
    memory_analysis_night_minute: int
    midnight_cleanup_hour: int
    midnight_cleanup_minute: int


class HotDurationConfig(BaseModel):
    hot_duration_minutes: int


class ActiveMessageConfig(BaseModel):
    enabled: bool = False
    hour: int = 10
    minute: int = 0
    daily_limit: int = 1
    sessions: Optional[dict] = None


class ContextCheckpointConfig(BaseModel):
    threshold_k: int = 500


class GroupChatOpsConfig(BaseModel):
    enabled: Optional[bool] = None
    group_id: Optional[str] = None
    observe_only: Optional[bool] = None
    allow_mention_reply: Optional[bool] = None
    allow_roll_reply: Optional[bool] = None
    allow_repetition: Optional[bool] = None
    allow_meme_send: Optional[bool] = None
    allow_command_reply: Optional[bool] = None


class InputStatusConfig(BaseModel):
    composing: bool = True
    ttl_ms: int = 3000
    session_id: Optional[str] = None


class SessionSelectRequest(BaseModel):
    session_id: str


class NudgeRequest(BaseModel):
    session_id: Optional[str] = None


class MemeStealAnalyzeRequest(BaseModel):
    image_ref: str
    context_text: str = ""


def init_gate():
    global event_gate, companion_graph, media_job_queue, runtime_manager
    if runtime_manager is None:
        queue = _ensure_media_job_queue()
        runtime_manager = SessionRuntimeManager(
            root_dir=ROOT_DIR,
            base_llm_client=llm_client,
            base_memory_manager=memory_manager,
            meme_catalog=meme_catalog,
            media_job_queue=queue,
            on_decision=on_decision,
            hot_duration_minutes=_persisted.get("hot_duration_minutes", 30),
            conversation_context_loader=_load_conversation_context_for_runtime,
        )
        queue.set_llm_client_factory(runtime_manager.make_internal_llm_for_session)
        scheduler_manager.set_llm_client_factory(runtime_manager.make_background_llm_for_session)
        default_runtime = runtime_manager.get(DEFAULT_SESSION_ID)
        event_gate = default_runtime.gate
        companion_graph = default_runtime.graph
        # 启动时把持久化的调度时间应用到调度器
        for job_id in ("memory_analysis_day", "memory_analysis_night", "midnight_cleanup"):
            cfg = _persisted.get(job_id)
            if isinstance(cfg, dict):
                scheduler_manager.update_schedule(job_id, cfg.get("hour"), cfg.get("minute"))
        active_cfg = _normalize_active_message_config(_persisted.get("active_message"))
        scheduler_manager.set_active_message_callback(run_scheduled_active_messages)
        scheduler_manager.set_active_message_setting_callback(_apply_active_message_setting)
        scheduler_manager.set_group_activity_callback(run_group_activity_update)
        scheduler_manager.set_session_ids_provider(_scheduler_session_ids)
        scheduler_manager.set_context_checkpoint_callback(_apply_context_checkpoint_to_runtime)
        _refresh_active_message_jobs(active_cfg)
        scheduler_manager.start()
    elif media_job_queue:
        media_job_queue.start()


def _ensure_media_job_queue() -> MediaJobQueue:
    global media_job_queue
    if media_job_queue is None:
        media_job_queue = MediaJobQueue(
            llm_client=llm_client,
            media_downloader=onebot_media_downloader,
            meme_steal_analyzer=meme_steal_analyzer,
            meme_steal_saver=meme_steal_saver,
            on_payloads=_record_background_media_payloads,
            max_workers=1,
        )
    media_job_queue.start()
    return media_job_queue


def _ensure_group_reply_scheduler() -> GroupReplyScheduler:
    global group_reply_scheduler
    if group_reply_scheduler is None:
        group_reply_scheduler = GroupReplyScheduler(
            group_buffer=group_chat_buffer,
            decision_callback=_run_group_reply_decision,
            send_callback=_send_group_reply_candidate,
            debug_callback=_record_group_reply_scheduler_event,
            activity_callback=group_activity_tracker.roll_cooldown_adjustment,
        )
    return group_reply_scheduler


def _current_group_send_config() -> dict:
    return normalize_group_send_config(load_settings().get("group_chat_send"))


def _save_group_send_config(config: dict) -> dict:
    normalized = normalize_group_send_config(config)
    save_settings({"group_chat_send": normalized})
    group_send_limiter.update_config(normalized)
    return normalized


def _group_policy_gate(
    session_id: str,
    *,
    trigger_reason: Optional[str] = None,
    item_type: Optional[str] = None,
) -> dict:
    return evaluate_group_send_policy(
        _current_group_send_config(),
        normalize_session_id(session_id),
        trigger_reason=trigger_reason,
        item_type=item_type,
    )


def _group_trigger_reason_for_event(event: ChatEvent) -> str:
    raw = event.raw if isinstance(event.raw, dict) else {}
    return "mention" if raw.get("mentions_bot") else "roll"


def _refresh_group_send_limiter() -> GroupSendLimiter:
    group_send_limiter.update_config(_current_group_send_config())
    return group_send_limiter


def _runtime_for_session(session_id: Optional[str]) -> SessionRuntime:
    init_gate()
    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    runtime = runtime_manager.get(sid) if runtime_manager else None
    if not runtime:
        raise RuntimeError("session runtime manager is not initialized")
    return runtime


def _internal_llm_for_session(session_id: Optional[str]) -> LLMClient:
    init_gate()
    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    if runtime_manager:
        return runtime_manager.make_internal_llm_for_session(sid)
    return LLMClient(
        api_key=llm_client.api_key,
        base_url=llm_client.base_url,
        model=llm_client.model,
        thinking_enabled=llm_client.thinking_enabled,
        temperature=llm_client.temperature,
        cache_affinity_enabled=False,
        provider_user_id="",
    )


def _runtime_for_event(event: ChatEvent) -> SessionRuntime:
    return _runtime_for_session(event.session_id)


def _load_conversation_context_for_runtime(session_id: str) -> dict:
    return load_conversation_context(normalize_session_id(session_id or DEFAULT_SESSION_ID))


def _apply_context_checkpoint_to_runtime(session_id: str, checkpoint_text: str, history: list[dict]):
    if not runtime_manager:
        return
    runtime = runtime_manager.get_if_exists(session_id)
    if not runtime:
        return
    runtime.graph.load_conversation_context(checkpoint_text, history)


def _refresh_active_message_jobs(config: Optional[dict] = None, persist: bool = True) -> dict:
    normalized = _normalize_active_message_config(
        config if config is not None else load_settings().get("active_message")
    )
    scheduler_manager.update_schedule("active_message", normalized["hour"], normalized["minute"])
    refreshed, changed = scheduler_manager.refresh_active_message_jobs(normalized)
    if changed and persist:
        save_settings({"active_message": refreshed})
    return refreshed


def _session_id_from_query(session_id: Optional[str]) -> str:
    return webui_session_id(session_id)


def _is_group_session_id(session_id: Optional[str]) -> bool:
    return normalize_session_id(session_id or "").startswith("qq_group_")


def _list_sessions() -> list[dict]:
    seen: dict[str, dict] = {}

    def add(session_id: str, **extra):
        sid = normalize_session_id(session_id)
        identity = infer_identity(sid)
        current = seen.setdefault(sid, {
            "session_id": sid,
            "platform": identity.platform,
            "user_id": identity.user_id,
            "label": identity.label,
            "active_runtime": False,
            "message_count": 0,
            "last_message_at": None,
        })
        for key, value in extra.items():
            if value is not None:
                current[key] = value

    add(DEFAULT_SESSION_ID, label="WebUI 默认")
    for item in session_registry.list_file_sessions():
        extra = dict(item)
        sid = extra.pop("session_id")
        add(sid, **extra)

    if runtime_manager:
        for sid in runtime_manager.active_session_ids():
            add(sid, active_runtime=True)

    active_cfg = _normalize_active_message_config(load_settings().get("active_message"))
    for sid in (active_cfg.get("sessions") or {}):
        add(sid)

    db = next(get_db())
    try:
        rows = (
            db.query(
                ConversationEvent.session_id,
                func.count(ConversationEvent.id),
                func.max(ConversationEvent.created_at),
            )
            .group_by(ConversationEvent.session_id)
            .all()
        )
        for sid, count, last_at in rows:
            add(
                sid,
                message_count=int(count or 0),
                last_message_at=last_at.isoformat() if last_at else None,
            )
    finally:
        db.close()

    return sorted(
        seen.values(),
        key=lambda item: (
            item.get("platform") != "webui",
            item.get("last_message_at") or "",
            item.get("session_id") or "",
        ),
    )


def _scheduler_session_ids() -> list[str]:
    return [item["session_id"] for item in _list_sessions() if not _is_group_session_id(item["session_id"])]


def _clear_media_followup_keys(session_id: Optional[str] = None):
    if not session_id:
        _media_followup_keys.clear()
        return
    prefix = f"{normalize_session_id(session_id)}:"
    for key in list(_media_followup_keys):
        if key.startswith(prefix):
            _media_followup_keys.discard(key)


async def shutdown_background_workers():
    if media_job_queue:
        await media_job_queue.shutdown()


def _json_dumps(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _event_payload(event: ChatEvent) -> dict:
    return event.model_dump(mode="json")


def _ensure_process_context_defaults(ctx: ProcessContext):
    defaults = {
        "internal_source": None,
        "internal_payload": None,
        "skip_buffer_clear": False,
        "bypass_snapshot_stale": False,
        "bypass_send_group_gate": False,
        "bypass_user_composing_gate": False,
    }
    for key, value in defaults.items():
        if not hasattr(ctx, key):
            setattr(ctx, key, value)


def _graph_for_context(ctx: ProcessContext) -> Optional[CompanionGraph]:
    if runtime_manager:
        runtime = runtime_manager.get_if_exists(ctx.snapshot.session_id)
        if runtime:
            return runtime.graph
    return companion_graph


async def on_decision(ctx: ProcessContext):
    """LLM 决策回调"""
    _ensure_process_context_defaults(ctx)
    gate = ctx.gate
    graph = _graph_for_context(ctx)
    target = _message_target_from_context(ctx)
    llm_target = {**target, "session_id": ctx.snapshot.session_id}

    if gate.is_job_stale(ctx.job_id):
        gate.mark_job_stale_dropped(ctx.job_id)
        await gate.dispatch_latest_after_stale(ctx.job_id)
        _record_job_state(ctx, "stale_dropped", {"reason": "job_already_stale"})
        await _emit_state({"session_id": ctx.snapshot.session_id, "job_id": ctx.job_id, "result": "stale_dropped"})
        return

    try:
        await _emit_llm_started(llm_target)
        if not graph:
            raise RuntimeError(f"session graph is not initialized: {ctx.snapshot.session_id}")
        if ctx.internal_source == "media_followup":
            decision = await graph.run_media_followup(ctx, ctx.internal_payload or {})
        else:
            decision = await graph.run(ctx)
        _record_prompt_cache_debug(ctx, graph.get_prompt_observability())
        media_debug = (
            graph.get_last_media_debug()
            if graph and hasattr(graph, "get_last_media_debug")
            else None
        )
        _record_media_harness_debug(ctx, media_debug)
    except Exception as e:
        await _emit_llm_finished(llm_target, "error")
        gate.mark_job_dropped(ctx.job_id)
        _record_llm_error(ctx, str(e))
        await _emit_state({
            "job_id": ctx.job_id,
            "snapshot_id": ctx.snapshot.snapshot_id,
            "result": "error",
        })
        return

    if not ctx.bypass_snapshot_stale and not await gate.is_snapshot_current(ctx):
        await _emit_llm_finished(llm_target, "stale_dropped")
        gate.mark_job_stale_dropped(ctx.job_id)
        await gate.dispatch_latest_after_stale(ctx.job_id)
        _record_job_state(ctx, "stale_dropped", {"reason": "snapshot_not_current"})
        await _emit_state({"session_id": ctx.snapshot.session_id, "job_id": ctx.job_id, "result": "stale_dropped"})
        return

    decision, repetition_removed = _apply_recent_repetition_guard(ctx, decision)
    if repetition_removed:
        _record_repetition_guard_filter(ctx, decision, repetition_removed)
        if graph:
            graph.append_internal_system_reminder(
                build_repetition_guard_system_reminder(repetition_removed)
            )

    decision, internal_removed = _apply_internal_output_guard(decision)
    if internal_removed:
        _record_internal_output_guard_filter(ctx, decision, internal_removed)
        if graph:
            graph.reset_provider_transcript(reason="internal_output_guard_filtered")

    gate.record_decision(decision)

    # 路由决策
    from ..core.router import ActionRouter
    router_inst = ActionRouter()
    result = router_inst.route(decision)
    items = list(result.get("items") or [])

    send_group_version = ctx.snapshot.buffer_version
    visible_send_claimed = not (result["visible"] and items and not ctx.skip_buffer_clear)

    async def claim_visible_send(reason: str) -> bool:
        nonlocal send_group_version, visible_send_claimed
        if visible_send_claimed:
            return True
        cleared, new_version = await gate.clear_buffer_after_visible_send(ctx.snapshot.buffer_version)
        if not cleared:
            await _emit_llm_finished(llm_target, "stale_dropped")
            gate.mark_job_stale_dropped(ctx.job_id)
            await gate.dispatch_latest_after_stale(ctx.job_id)
            _record_job_state(ctx, "stale_dropped", {
                "reason": "buffer_version_changed_before_send",
                "claim_reason": reason,
            })
            await _emit_state({"session_id": ctx.snapshot.session_id, "job_id": ctx.job_id, "result": "stale_dropped"})
            return False
        send_group_version = new_version
        visible_send_claimed = True
        return True

    if decision.action == Action.ENTER_CHAT:
        await gate._enter_hot()
    elif decision.action == Action.END_CHAT:
        await gate._exit_hot()

    if not result["visible"] or not items:
        await _emit_llm_finished(llm_target, "dropped")
        _record_decision_drop(ctx, decision)
        _record_active_message_setting_side_effect(ctx, decision, "skipped", {
            "active_schedule_marker_detected": True,
            "active_message_setting": decision.active_message_setting(),
            "visibility": "internal_debug_only",
            "reason": "not_visible",
            "sent_item_count": 0,
        })
        gate.mark_job_dropped(ctx.job_id)
        await _emit_state({
            "session_id": ctx.snapshot.session_id,
            "job_id": ctx.job_id,
            "snapshot_id": ctx.snapshot.snapshot_id,
            "action": decision.action.value,
            "result": "dropped",
        })
        return

    display_units = _build_display_send_units(items)
    sent_items: list[SendItem] = []
    sent_original_indices: set[int] = set()
    committed_sent_count = 0

    def commit_sent_progress():
        nonlocal committed_sent_count
        if not graph or committed_sent_count >= len(sent_items):
            return
        pending_items = sent_items[committed_sent_count:]
        if committed_sent_count == 0:
            graph.commit_sent_items(ctx, list(sent_items))
        else:
            graph.commit_assistant_items(pending_items)
        committed_sent_count = len(sent_items)

    send_count = len(display_units)
    for send_index, unit in enumerate(display_units):
        original_index = unit["original_index"]
        original_item = unit["original_item"]
        commit_item = original_item
        item = unit["display_item"]
        if not ctx.bypass_send_group_gate and not await gate.is_send_group_current(ctx, send_group_version):
            await _emit_llm_finished(llm_target, "stale_dropped")
            commit_sent_progress()
            gate.mark_job_stale_dropped(ctx.job_id)
            await gate.dispatch_latest_after_stale(ctx.job_id)
            _record_job_state(ctx, "stale_dropped", {
                "reason": "send_group_changed",
                "send_index": send_index,
                "send_count": send_count,
            })
            await _emit_state({
                "session_id": ctx.snapshot.session_id,
                "job_id": ctx.job_id,
                "snapshot_id": ctx.snapshot.snapshot_id,
                "send_index": send_index,
                "send_count": send_count,
                "result": "stale_dropped",
            })
            return

        send_key = gate.build_send_key(ctx, send_index)
        if gate.is_send_key_sent(send_key):
            continue

        if item.type == SendItemType.SEARCH_MEME:
            if not graph:
                continue
            resolved_item = await graph.resolve_search_meme_item(ctx, item)
            if not resolved_item:
                continue
            item = resolved_item
            commit_item = resolved_item
            if not ctx.bypass_send_group_gate and not await gate.is_send_group_current(ctx, send_group_version):
                await _emit_llm_finished(llm_target, "stale_dropped")
                commit_sent_progress()
                gate.mark_job_stale_dropped(ctx.job_id)
                await gate.dispatch_latest_after_stale(ctx.job_id)
                _record_job_state(ctx, "stale_dropped", {
                    "reason": "send_group_changed_after_meme_selection",
                    "send_index": send_index,
                    "send_count": send_count,
                })
                await _emit_state({
                    "session_id": ctx.snapshot.session_id,
                    "job_id": ctx.job_id,
                    "snapshot_id": ctx.snapshot.snapshot_id,
                    "send_index": send_index,
                    "send_count": send_count,
                    "result": "stale_dropped",
                })
                return

        final_guard_reason = _final_visible_item_guard_reason(item)
        if final_guard_reason:
            _record_internal_output_guard_filter(ctx, decision, [{
                "item_index": original_index,
                "item_type": item.type.value,
                "reason": final_guard_reason,
                "content_preview": item.content[:200],
                "send_index": send_index,
                "split_guard_fallback": unit.get("split_guard_fallback"),
            }])
            continue

        content = item.content
        item_type = item.type.value
        per_send_result = {**result, "text": content, "texts": [content], "item": item}

        if not ctx.bypass_user_composing_gate and _requires_user_composing_gate([item]):
            commit_sent_progress()
            if not await _wait_until_user_not_composing(ctx, send_group_version):
                await _emit_llm_finished(llm_target, "stale_dropped")
                return

        if not await claim_visible_send(f"send_index_{send_index}"):
            return

        gate.record_send_meta(ctx, send_index, send_count, item_type)
        if not ctx.bypass_send_group_gate and not await gate.is_send_group_current(ctx, send_group_version):
            await _emit_llm_finished(llm_target, "stale_dropped")
            commit_sent_progress()
            gate.mark_job_stale_dropped(ctx.job_id)
            await gate.dispatch_latest_after_stale(ctx.job_id)
            _record_job_state(ctx, "stale_dropped", {
                "reason": "send_group_changed_after_meta",
                "send_index": send_index,
                "send_count": send_count,
            })
            await _emit_state({
                "session_id": ctx.snapshot.session_id,
                "job_id": ctx.job_id,
                "snapshot_id": ctx.snapshot.snapshot_id,
                "send_index": send_index,
                "send_count": send_count,
                "result": "stale_dropped",
            })
            return

        await _emit_message({
            "type": "assistant_message",
            "session_id": ctx.snapshot.session_id,
            "action": decision.action.value,
            "text": content,
            "content": content,
            "item_type": item_type,
            "visible": True,
            "is_meme": item.type in (SendItemType.MEME, SendItemType.EMOJI),
            "meme_path": content[5:] if item.type == SendItemType.MEME else None,
            "job_id": ctx.job_id,
            "snapshot_id": ctx.snapshot.snapshot_id,
            "send_index": send_index,
            "send_count": send_count,
            "send_key": send_key,
            **target,
            **_reply_target_from_context(ctx, send_index),
        })
        gate.mark_send_key_sent(send_key)
        _record_assistant_send(
            ctx=ctx,
            decision=decision,
            item=item,
            routed=per_send_result,
            send_index=send_index,
            send_count=send_count,
            send_key=send_key,
        )
        await _emit_conversation_changed("assistant_send", session_id=ctx.snapshot.session_id)
        if unit["is_last_part"] and original_index not in sent_original_indices:
            sent_items.append(commit_item)
            sent_original_indices.add(original_index)

    commit_sent_progress()
    if sent_items:
        _apply_active_message_setting_side_effect_after_send(ctx, decision, sent_items)
        gate.mark_job_sent(ctx.job_id)
        await _emit_llm_finished(llm_target, "sent")
    else:
        _record_active_message_setting_side_effect(ctx, decision, "skipped", {
            "active_schedule_marker_detected": True,
            "active_message_setting": decision.active_message_setting(),
            "visibility": "internal_debug_only",
            "reason": "no_visible_send",
            "sent_item_count": 0,
        })
        gate.mark_job_dropped(ctx.job_id)
        await _emit_llm_finished(llm_target, "dropped")


def _apply_active_message_setting_side_effect_after_send(
    ctx: ProcessContext,
    decision: ActionDecision,
    sent_items: list[SendItem],
):
    setting = decision.active_message_setting()
    if not setting:
        return

    base_payload = {
        "active_schedule_marker_detected": True,
        "active_message_setting": setting,
        "sent_item_count": len(sent_items),
        "visibility": "internal_debug_only",
        "applied_after_visible_send": bool(sent_items),
    }
    if not sent_items:
        _record_active_message_setting_side_effect(
            ctx,
            decision,
            "skipped",
            {**base_payload, "reason": "no_visible_send"},
        )
        return

    allowed, reason = _active_message_setting_side_effect_allowed(ctx)
    if not allowed:
        _record_active_message_setting_side_effect(
            ctx,
            decision,
            "skipped",
            {**base_payload, "reason": reason},
        )
        return

    try:
        success, response_text = _apply_active_message_setting(ctx.snapshot.session_id, setting)
    except Exception as exc:  # noqa: BLE001
        _record_active_message_setting_side_effect(
            ctx,
            decision,
            "error",
            {
                **base_payload,
                "active_schedule_applied": False,
                "reason": "apply_exception",
                "error": str(exc)[:500],
            },
        )
        return

    _record_active_message_setting_side_effect(
        ctx,
        decision,
        "applied" if success else "error",
        {
            **base_payload,
            "active_schedule_applied": success,
            "response_text": response_text,
            "reason": "applied" if success else "apply_failed",
        },
    )


def _active_message_setting_side_effect_allowed(ctx: ProcessContext) -> tuple[bool, str]:
    user_text = _active_message_setting_snapshot_user_text(ctx)
    if not user_text:
        return False, "no_user_text_in_snapshot"
    if looks_like_active_message_setting_request(user_text):
        return True, "current_user_requested_active_message_setting"
    return False, "current_user_text_did_not_request_active_message_setting"


def _active_message_setting_snapshot_user_text(ctx: ProcessContext) -> str:
    parts: list[str] = []
    for evt in getattr(ctx.snapshot, "events", []) or []:
        if _event_role(evt) != "user":
            continue
        text = _event_text(evt)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _event_role(evt) -> str:
    if isinstance(evt, dict):
        return str(evt.get("role") or "user").strip().lower()
    return str(getattr(evt, "role", "user") or "user").strip().lower()


def _event_text(evt) -> str:
    if isinstance(evt, dict):
        return str(evt.get("text") or "").strip()
    return str(getattr(evt, "text", "") or "").strip()


def _record_active_message_setting_side_effect(
    ctx: ProcessContext,
    decision: ActionDecision,
    status: str,
    payload: dict,
):
    setting = decision.active_message_setting()
    if not setting:
        return
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=ctx.snapshot.session_id,
            event_type="active_message.setting_side_effect",
            raw_payload=_json_dumps(payload),
            parsed_payload=_json_dumps(decision.to_harness_payload(exclude_none=True)),
            action=decision.action.value,
            snapshot_id=ctx.snapshot.snapshot_id,
            buffer_version=ctx.snapshot.buffer_version,
            job_id=ctx.job_id,
            status=status,
        )
        db.add(log)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


def _apply_recent_repetition_guard(
    ctx: ProcessContext,
    decision: ActionDecision,
) -> tuple[ActionDecision, list[RepetitionRemoval]]:
    if decision.action in (Action.WAIT, Action.END_CHAT):
        return decision, []

    recent_texts = _recent_assistant_visible_texts(ctx.snapshot.session_id, RECENT_REPETITION_WINDOW)
    result = filter_recent_repeated_reactions(decision, recent_texts)
    return result.decision, result.removed


def _recent_assistant_visible_texts(session_id: str, limit: int) -> list[str]:
    db = next(get_db())
    try:
        rows = (
            db.query(ConversationEvent)
            .filter(ConversationEvent.session_id == session_id)
            .filter(ConversationEvent.is_visible == True)  # noqa: E712
            .filter(ConversationEvent.event_type.in_(["assistant_text", "assistant_react"]))
            .order_by(desc(ConversationEvent.id))
            .limit(limit)
            .all()
        )
        return [row.text for row in rows if row.text]
    finally:
        db.close()


def _record_repetition_guard_filter(
    ctx: ProcessContext,
    decision: ActionDecision,
    removed: list[RepetitionRemoval],
):
    db = next(get_db())
    try:
        payload = {
            "window": RECENT_REPETITION_WINDOW,
            "removed": [
                {
                    "kind": item.kind,
                    "value": item.value,
                    "item_index": item.item_index,
                }
                for item in removed
            ],
            "visibility": "internal_debug_only",
            "history_mutation": "none",
        }
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=ctx.snapshot.session_id,
            event_type="repetition_guard",
            raw_payload=_json_dumps(payload),
            parsed_payload=_json_dumps(decision.to_harness_payload(exclude_none=True)),
            action=decision.action.value,
            snapshot_id=ctx.snapshot.snapshot_id,
            buffer_version=ctx.snapshot.buffer_version,
            job_id=ctx.job_id,
            status="filtered",
        )
        db.add(log)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


def _apply_internal_output_guard(decision: ActionDecision) -> tuple[ActionDecision, list[dict]]:
    if decision.action in (Action.WAIT, Action.END_CHAT):
        return decision, []

    kept: list[SendItem] = []
    removed: list[dict] = []
    for index, item in enumerate(decision.all_items()):
        reason = _internal_output_guard_reason(item)
        if reason:
            removed.append({
                "item_index": index,
                "item_type": item.type.value,
                "reason": reason,
                "content_preview": item.content[:200],
            })
            continue
        kept.append(item)

    if not removed:
        return decision, []
    if not kept:
        return ActionDecision(action=Action.WAIT, items=None), removed
    return decision.with_items(kept), removed


def _internal_output_guard_reason(item: SendItem) -> Optional[str]:
    if item.type != SendItemType.TEXT:
        return None
    content = item.content or ""
    safety = classify_visible_text(content, mode="bubble")
    if not safety.ok:
        return safety.reason or "unsafe_visible_text"
    return None


def _final_visible_item_guard_reason(item: SendItem) -> Optional[str]:
    if item.type != SendItemType.TEXT:
        return None
    safety = classify_visible_text(item.content or "", mode="bubble")
    if safety.ok:
        return None
    return safety.reason or "unsafe_visible_text"


def _record_internal_output_guard_filter(
    ctx: ProcessContext,
    decision: ActionDecision,
    removed: list[dict],
):
    db = next(get_db())
    try:
        payload = {
            "removed": removed,
            "visibility": "internal_debug_only",
            "history_mutation": "provider_transcript_reset",
            "reason": "blocked_user_visible_internal_protocol",
        }
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=ctx.snapshot.session_id,
            event_type="internal_output_guard",
            raw_payload=_json_dumps(payload),
            parsed_payload=_json_dumps(decision.to_harness_payload(exclude_none=True)),
            action=decision.action.value,
            snapshot_id=ctx.snapshot.snapshot_id,
            buffer_version=ctx.snapshot.buffer_version,
            job_id=ctx.job_id,
            status="filtered",
        )
        db.add(log)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


def _requires_user_composing_gate(items: list[SendItem]) -> bool:
    return any(item.type == SendItemType.TEXT for item in items)


def _build_display_send_units(items: list[SendItem]) -> list[dict]:
    allow_text_split = len(items) < DISPLAY_SPLIT_DISABLE_ITEM_COUNT
    units = _display_send_units_with_split(items, allow_text_split=allow_text_split)
    if allow_text_split and len(units) > DISPLAY_SPLIT_DISABLE_ITEM_COUNT:
        return _display_send_units_with_split(items, allow_text_split=False)
    return units


def _display_send_units_with_split(items: list[SendItem], allow_text_split: bool) -> list[dict]:
    units: list[dict] = []
    for original_index, item in enumerate(items):
        split_guard_fallback = None
        if item.type == SendItemType.TEXT and allow_text_split:
            display_parts, split_guard_fallback = _safe_split_text_for_display(item.content)
        else:
            display_parts = [item.content]
        for part_index, part in enumerate(display_parts):
            display_item = item
            if item.type == SendItemType.TEXT and part != item.content:
                display_item = SendItem(type=SendItemType.TEXT, content=part)
            units.append({
                "original_index": original_index,
                "original_item": item,
                "display_item": display_item,
                "is_last_part": part_index == len(display_parts) - 1,
                "split_guard_fallback": split_guard_fallback,
            })
    return units


def _safe_split_text_for_display(text: str) -> tuple[list[str], Optional[str]]:
    original_safety = classify_visible_text(text or "", mode="bubble")
    if not original_safety.ok:
        return [], original_safety.reason or "unsafe_original_text"

    parts = _split_text_for_display(text)
    for part in parts:
        safety = classify_visible_text(part or "", mode="bubble")
        if not safety.ok:
            return [text], safety.reason or "unsafe_split_part"
    return parts, None


def _split_text_for_display(text: str) -> list[str]:
    if not text:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    chinese_count_after_trigger = 0
    index = 0

    while index < len(text):
        trigger = _display_split_trigger_at(text, index)
        if trigger:
            current.append(trigger)
            if chinese_count_after_trigger >= DISPLAY_SPLIT_MIN_CHINESE_CHARS:
                parts.append("".join(current))
                current = []
            chinese_count_after_trigger = 0
            index += len(trigger)
            continue

        char = text[index]
        current.append(char)
        if _is_chinese_char(char):
            chinese_count_after_trigger += 1
        index += 1

    if current:
        parts.append("".join(current))
    return parts or [text]


def _display_split_trigger_at(text: str, index: int) -> Optional[str]:
    for trigger in DISPLAY_SPLIT_TRIGGERS:
        if text.startswith(trigger, index):
            return trigger
    return None


def _is_chinese_char(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _onebot_expected_token() -> str:
    return (
        os.getenv("ONEBOT_ACCESS_TOKEN")
        or str(load_settings().get("onebot_access_token") or "")
    ).strip()


def _message_target_from_snapshot(snapshot) -> dict:
    events = list(snapshot.events or [])
    if not events:
        return {}

    # Only the latest pending input owns the outgoing channel. This prevents a
    # WebUI turn from being sent to QQ just because an older QQ event is still
    # present in the same buffered snapshot.
    event = events[-1]
    if event.get("platform") != "qq":
        return {}

    user_id = event.get("user_id")
    raw = event.get("raw") or {}
    if isinstance(raw, dict):
        user_id = raw.get("qq_user_id") or user_id
    if user_id:
        return {
            "target_platform": "qq",
            "target_user_id": str(user_id),
        }
    return {}


def _message_target_from_context(ctx: ProcessContext) -> dict:
    if ctx.internal_source:
        return _message_target_from_session_id(ctx.snapshot.session_id)
    return _message_target_from_snapshot(ctx.snapshot)


def _reply_target_from_context(ctx: ProcessContext, send_index: int = 0) -> dict:
    if send_index != 0:
        return {}
    if ctx.internal_source != "media_followup":
        return {}
    payload = ctx.internal_payload or {}
    if not isinstance(payload, dict):
        return {}
    media_ref = payload.get("media_ref") or {}
    if not isinstance(media_ref, dict):
        media_ref = {}
    message_id = (
        media_ref.get("onebot_message_id")
        or payload.get("onebot_message_id")
        or payload.get("reply_to_message_id")
    )
    message_id = str(message_id or "").strip()
    if not message_id:
        return {}
    return {"reply_to_message_id": message_id}


def _message_target_from_event(event: ChatEvent) -> dict:
    if event.platform != "qq":
        return {}
    return {
        "target_platform": "qq",
        "target_user_id": event.user_id,
    }


def _message_target_from_session_id(session_id: str) -> dict:
    sid = normalize_session_id(session_id)
    identity = infer_identity(sid)
    if identity.platform == "qq":
        return {
            "session_id": sid,
            "target_platform": "qq",
            "target_user_id": identity.user_id,
        }
    return {"session_id": sid}


async def _emit_message(data: dict):
    for cb in message_callbacks:
        try:
            await cb(data)
        except Exception:
            pass
    if data.get("type") == "assistant_message" and data.get("target_platform") == "qq":
        await _send_onebot_assistant_message(data)


async def _emit_state(data: dict):
    await _emit_message({"type": "assistant_state", **data})


async def _emit_llm_started(target: Optional[dict] = None):
    """LLM 真正开始思考/生成时通知前端显示“对方正在输入”。"""
    payload = {"result": "llm_started"}
    if target:
        payload.update(target)
    await _emit_state(payload)
    await _start_onebot_input_status_refresh(target)


async def _emit_llm_finished(target: Optional[dict] = None, reason: str = "llm_finished"):
    await _stop_onebot_input_status_refresh(target)
    payload = {"result": "llm_finished", "reason": reason}
    if target:
        payload.update(target)
    await _emit_state(payload)


def _onebot_input_status_user_id(target: Optional[dict]) -> str:
    if not target or target.get("target_platform") != "qq":
        return ""
    user_id = str(target.get("target_user_id") or "")
    return user_id.strip()


async def _start_onebot_input_status_refresh(target: Optional[dict]):
    user_id = _onebot_input_status_user_id(target)
    if not user_id:
        return

    await _stop_onebot_input_status_refresh(target)
    task_target = dict(target or {})
    if not await _set_onebot_input_status(
        task_target,
        event_type=ONEBOT_LLM_INPUT_STATUS_EVENT_TYPE,
        reason="llm_started",
    ):
        return

    task = asyncio.create_task(_onebot_input_status_refresh_loop(task_target, user_id))
    _onebot_input_status_tasks[user_id] = task

    def cleanup(done_task: asyncio.Task):
        if _onebot_input_status_tasks.get(user_id) is done_task:
            _onebot_input_status_tasks.pop(user_id, None)

    task.add_done_callback(cleanup)


async def _stop_onebot_input_status_refresh(target: Optional[dict]):
    user_id = _onebot_input_status_user_id(target)
    if not user_id:
        return
    task = _onebot_input_status_tasks.pop(user_id, None)
    if not task or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _onebot_input_status_refresh_loop(target: dict, user_id: str):
    started_at = asyncio.get_running_loop().time()
    try:
        while True:
            await asyncio.sleep(ONEBOT_LLM_INPUT_STATUS_REFRESH_SECONDS)
            if asyncio.get_running_loop().time() - started_at > ONEBOT_LLM_INPUT_STATUS_MAX_SECONDS:
                return
            await _set_onebot_input_status(
                target,
                event_type=ONEBOT_LLM_INPUT_STATUS_EVENT_TYPE,
                reason="llm_refresh",
            )
    except asyncio.CancelledError:
        raise


async def _set_onebot_input_status(target: Optional[dict], event_type: int, reason: str) -> bool:
    user_id = _onebot_input_status_user_id(target)
    if not user_id:
        return False

    try:
        response = await onebot_manager.set_input_status(user_id, event_type)
        _record_onebot_input_status(user_id, event_type, response, "sent", reason, session_id=normalize_session_id(target.get("session_id") if target else None))
        return True
    except Exception as exc:
        _record_onebot_input_status(user_id, event_type, None, "error", reason, str(exc), session_id=normalize_session_id(target.get("session_id") if target else None))
        return False


async def _emit_conversation_changed(
    reason: str,
    event: Optional[ChatEvent] = None,
    session_id: Optional[str] = None,
):
    if event and event.event_type == EventType.USER_COMPOSING:
        return
    await _emit_message({
        "type": "conversation_changed",
        "session_id": event.session_id if event else normalize_session_id(session_id or DEFAULT_SESSION_ID),
        "reason": reason,
        "event_type": event.event_type.value if event else None,
        "platform": event.platform if event else None,
        "user_id": event.user_id if event else None,
    })


async def _send_onebot_assistant_message(data: dict) -> None:
    target_user_id = str(data.get("target_user_id") or "")
    if not target_user_id:
        return

    session_id = normalize_session_id(data.get("session_id") or DEFAULT_SESSION_ID)
    send_key = data.get("send_key") or f"onebot_command_{uuid.uuid4().hex[:8]}"
    item_type = str(data.get("item_type") or "text")
    content = str(data.get("content") or data.get("text") or "")
    reply_to_message_id = str(data.get("reply_to_message_id") or "").strip() or None
    try:
        if item_type == SendItemType.MEME.value or content.startswith("meme:"):
            stem = content[5:] if content.startswith("meme:") else content
            image_path = _meme_path_for_stem(stem)
            if not image_path:
                _record_onebot_send(
                    session_id,
                    send_key,
                    target_user_id,
                    item_type,
                    content,
                    None,
                    "skipped:meme_not_found",
                    reply_to_message_id=reply_to_message_id,
                )
                return
            response = await onebot_manager.send_private_image(
                target_user_id,
                Path(image_path).resolve().as_uri(),
                reply_to_message_id=reply_to_message_id,
            )
        else:
            response = await onebot_manager.send_private_text(
                target_user_id,
                content,
                reply_to_message_id=reply_to_message_id,
            )
        _record_onebot_send(
            session_id,
            send_key,
            target_user_id,
            item_type,
            content,
            response,
            "sent",
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as exc:
        _record_onebot_send(
            session_id,
            send_key,
            target_user_id,
            item_type,
            content,
            None,
            "error",
            str(exc),
            reply_to_message_id=reply_to_message_id,
        )


def _meme_path_for_stem(stem: str) -> str:
    normalized = (stem or "").strip()
    if not normalized:
        return ""
    for category_id, stems in meme_catalog.get_all_images().items():
        if normalized in stems:
            return meme_catalog.get_image_path(category_id, normalized)
    return ""


def _onebot_response_message_id(response: Optional[dict]) -> Optional[str]:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if isinstance(data, dict):
        message_id = data.get("message_id")
        if message_id is not None:
            return str(message_id)
    message_id = response.get("message_id")
    if message_id is not None:
        return str(message_id)
    return None


def _record_onebot_send(
    session_id: str,
    send_key: str,
    target_user_id: str,
    item_type: str,
    content: str,
    response: Optional[dict],
    status: str,
    error_message: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"{send_key}:onebot",
            session_id=session_id,
            event_type="onebot_send",
            platform="qq",
            user_id=target_user_id,
            raw_payload=_json_dumps({
                "target_user_id": target_user_id,
                "item_type": item_type,
                "reply_to_message_id": reply_to_message_id,
                "onebot_message_id": _onebot_response_message_id(response),
                "response": response,
            }),
            final_text=content,
            item_type=item_type,
            send_key=send_key,
            status=status,
            error_message=error_message,
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()


def _record_onebot_group_send(
    session_id: str,
    send_key: str,
    target_group_id: str,
    item_type: str,
    content: str,
    response: Optional[dict],
    status: str,
    error_message: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"{send_key}:onebot",
            session_id=session_id,
            event_type="onebot_group_send",
            platform="qq_group",
            user_id=target_group_id,
            raw_payload=_json_dumps({
                "target_group_id": target_group_id,
                "item_type": item_type,
                "reply_to_message_id": reply_to_message_id,
                "onebot_message_id": _onebot_response_message_id(response),
                "response": response,
            }),
            final_text=content,
            item_type=item_type,
            send_key=send_key,
            status=status,
            error_message=error_message,
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()


def _record_onebot_input_status(
    target_user_id: str,
    event_type: int,
    response: Optional[dict],
    status: str,
    reason: str,
    error_message: Optional[str] = None,
    session_id: Optional[str] = None,
):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"onebot_input_status_{uuid.uuid4().hex[:8]}",
            session_id=normalize_session_id(session_id or DEFAULT_SESSION_ID),
            event_type="onebot_input_status_send",
            platform="qq",
            user_id=target_user_id,
            raw_payload=_json_dumps({
                "target_user_id": target_user_id,
                "event_type": event_type,
                "reason": reason,
                "response": response,
            }),
            item_type="input_status",
            status=status,
            error_message=error_message,
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()


async def _wait_until_user_not_composing(
    ctx: ProcessContext,
    expected_buffer_version: Optional[int] = None,
) -> bool:
    gate = ctx.gate
    if not gate.is_user_composing():
        return True

    gate.mark_job_deferred(ctx.job_id)
    wait_started_at = datetime.now()
    wait_until = wait_started_at + timedelta(seconds=USER_COMPOSING_MAX_BLOCK_SECONDS)
    composing_meta = gate.get_user_composing_meta()
    await _emit_state({
        "session_id": ctx.snapshot.session_id,
        "job_id": ctx.job_id,
        "snapshot_id": ctx.snapshot.snapshot_id,
        "result": "deferred_user_composing",
        "max_wait_seconds": USER_COMPOSING_MAX_BLOCK_SECONDS,
        "wait_started_at": wait_started_at.isoformat(),
        "wait_until": wait_until.isoformat(),
        "composing": composing_meta,
    })

    while gate.is_user_composing():
        if gate.is_job_stale(ctx.job_id):
            gate.mark_job_stale_dropped(ctx.job_id)
            await gate.dispatch_latest_after_stale(ctx.job_id)
            _record_job_state(ctx, "stale_dropped", {"reason": "user_message_while_composing"})
            await _emit_state({
                "session_id": ctx.snapshot.session_id,
                "job_id": ctx.job_id,
                "snapshot_id": ctx.snapshot.snapshot_id,
                "result": "stale_dropped",
                "reason": "user_message_while_composing",
            })
            return False
        now = datetime.now()
        if now >= wait_until:
            gate.clear_user_composing(reason="send_gate_max_wait_elapsed")
            break
        await asyncio.sleep(min(0.2, max(0.01, (wait_until - now).total_seconds())))

    if expected_buffer_version is None:
        expected_buffer_version = ctx.snapshot.buffer_version
    still_current = await gate.is_send_group_current(ctx, expected_buffer_version)
    if gate.is_job_stale(ctx.job_id) or not still_current:
        gate.mark_job_stale_dropped(ctx.job_id)
        await gate.dispatch_latest_after_stale(ctx.job_id)
        _record_job_state(ctx, "stale_dropped", {
            "reason": "stale_after_user_composing",
            "expected_buffer_version": expected_buffer_version,
        })
        await _emit_state({
            "session_id": ctx.snapshot.session_id,
            "job_id": ctx.job_id,
            "snapshot_id": ctx.snapshot.snapshot_id,
            "result": "stale_dropped",
        })
        return False

    await _emit_state({
        "session_id": ctx.snapshot.session_id,
        "job_id": ctx.job_id,
        "snapshot_id": ctx.snapshot.snapshot_id,
        "result": "resumed_after_user_composing",
        "expected_buffer_version": expected_buffer_version,
    })
    return True

@router.post("/api/config")
async def update_config(config: ApiConfig):
    temperature = _normalize_temperature_for_provider(config.temperature, config.base_url, config.model)
    mimo_web_search_mode = _normalize_mimo_web_search_mode(config.mimo_web_search_mode)
    llm_client.update_config(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        thinking_enabled=config.thinking_enabled,
        temperature=temperature,
        mimo_web_search_mode=mimo_web_search_mode,
    )
    if runtime_manager:
        runtime_manager.reconfigure_llm_clients(reason="llm_config_changed")
    elif companion_graph:
        companion_graph.reset_provider_transcript(reason="llm_config_changed")
    save_settings({
        "api_key": config.api_key,
        "base_url": config.base_url,
        "model": config.model,
        "thinking_enabled": config.thinking_enabled,
        "temperature": temperature,
        "mimo_web_search_mode": mimo_web_search_mode,
    })
    return {
        "status": "ok",
        "temperature": temperature,
        "temperature_min": TEMPERATURE_MIN,
        "temperature_max": _temperature_max_for_provider(config.base_url, config.model),
        "mimo_web_search_mode": mimo_web_search_mode,
    }


@router.get("/api/config")
async def get_config():
    return {
        "api_key": llm_client.api_key,
        "base_url": llm_client.base_url,
        "model": llm_client.model,
        "thinking_enabled": llm_client.thinking_enabled,
        "temperature": llm_client.temperature,
        "temperature_min": TEMPERATURE_MIN,
        "temperature_max": _temperature_max_for_provider(llm_client.base_url, llm_client.model),
        "mimo_web_search_mode": llm_client.mimo_web_search_mode,
    }


@router.post("/api/chat")
async def send_message(msg: ChatMessage):
    session_id = webui_session_id(msg.session_id)
    runtime = _runtime_for_session(session_id)
    text = msg.text.strip()
    event_type = _event_type_for_text(text)

    event = ChatEvent(
        event_id=f"evt_{uuid.uuid4().hex[:8]}",
        session_id=session_id,
        event_type=event_type,
        text=text,
        timestamp=datetime.now(),
        raw={"source": "webui"},
    )

    if event.event_type == EventType.TEXT:
        active_setting_result = await _handle_active_message_setting_text_event(event)
        if active_setting_result:
            return active_setting_result

    _record_incoming_event(event)
    await _emit_conversation_changed("incoming_event", event)

    result = await runtime.gate.handle_event(event)

    if event_type == EventType.COMMAND_MEM:
        return await _handle_command_event(event, result)

    if event_type == EventType.COMMAND_FORGET:
        return await _handle_command_event(event, result)

    return result


@router.post("/api/input-status")
async def update_input_status(config: InputStatusConfig):
    session_id = webui_session_id(config.session_id)
    runtime = _runtime_for_session(session_id)
    event = ChatEvent(
        event_id=f"evt_{uuid.uuid4().hex[:8]}",
        session_id=session_id,
        event_type=EventType.USER_COMPOSING,
        timestamp=datetime.now(),
        raw={
            "composing": config.composing,
            "ttl_ms": config.ttl_ms,
            "source": "webui",
        },
    )
    result = await runtime.gate.handle_event(event)
    await _emit_state({
        "session_id": session_id,
        "result": "user_composing",
        "composing": runtime.gate.get_user_composing_meta(),
    })
    return result


@router.websocket("/onebot/ws")
async def onebot_ws(websocket: WebSocket):
    init_gate()
    await onebot_manager.handle_websocket(
        websocket=websocket,
        event_handler=handle_onebot_payload,
        expected_token=_onebot_expected_token(),
    )


@router.get("/api/onebot/status")
async def get_onebot_status():
    return onebot_manager.status()


@router.post("/api/onebot/debug/send-private-text")
async def debug_send_onebot_private_text(request: Request, payload: OneBotDebugSendRequest):
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="local debug endpoint only")

    user_id = payload.user_id.strip()
    text = payload.text.strip()
    if not user_id or not text:
        raise HTTPException(status_code=400, detail="user_id and text are required")

    try:
        response = await onebot_manager.send_private_text(user_id, text)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "sent", "response": response}


async def _enrich_onebot_reply_context(event: ChatEvent) -> ChatEvent:
    raw = event.raw or {}
    if event.platform != "qq" or not isinstance(raw, dict):
        return event

    reply_to_message_id = str(raw.get("reply_to_message_id") or "").strip()
    if not reply_to_message_id:
        return event
    if raw.get("reply_context"):
        return event

    enriched = dict(raw)
    context = _lookup_local_onebot_reply_context(event.session_id, reply_to_message_id)
    if context:
        enriched["reply_context"] = context
        return event.model_copy(update={"raw": enriched})

    try:
        response = await asyncio.wait_for(onebot_manager.get_msg(reply_to_message_id), timeout=3.0)
        context = _reply_context_from_get_msg_response(response, reply_to_message_id)
        if context:
            enriched["reply_context"] = context
        else:
            enriched["reply_context_error"] = "empty get_msg response"
    except Exception as exc:  # noqa: BLE001
        enriched["reply_context_error"] = str(exc)[:300]

    return event.model_copy(update={"raw": enriched})


def _lookup_local_onebot_reply_context(session_id: str, message_id: str) -> Optional[dict]:
    target_id = str(message_id or "").strip()
    if not target_id:
        return None

    db = next(get_db())
    try:
        rows = (
            db.query(RawChatLog)
            .filter(RawChatLog.session_id == normalize_session_id(session_id or DEFAULT_SESSION_ID))
            .order_by(desc(RawChatLog.id))
            .limit(300)
            .all()
        )
        for row in rows:
            payload = _safe_json_object(row.raw_payload)
            if row.event_type == "onebot_send":
                if str(payload.get("onebot_message_id") or "") == target_id:
                    return {
                        "message_id": target_id,
                        "role": "assistant",
                        "text": row.final_text or "",
                        "item_type": row.item_type or "text",
                        "source": "local_onebot_send_log",
                    }

            raw = payload.get("raw") if isinstance(payload, dict) else None
            if not isinstance(raw, dict):
                raw = payload
            if str(raw.get("onebot_message_id") or "") == target_id:
                return {
                    "message_id": target_id,
                    "role": "user",
                    "text": row.input_text or _segments_to_reply_summary(raw.get("message_segments") or []),
                    "item_type": row.event_type or "message",
                    "source": "local_incoming_log",
                }
    except Exception:
        return None
    finally:
        db.close()
    return None


def _reply_context_from_get_msg_response(response: dict, message_id: str) -> Optional[dict]:
    if not isinstance(response, dict):
        return None
    data = response.get("data")
    if not isinstance(data, dict):
        return None

    segments = data.get("message")
    text = (
        _segments_to_reply_summary(segments if isinstance(segments, list) else [])
        or str(data.get("raw_message") or "").strip()
        or str(data.get("message") or "").strip()
    )
    if not text:
        return None

    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    sender_id = str(sender.get("user_id") or data.get("user_id") or "").strip()
    self_id = str(onebot_manager.status().get("self_id") or "").strip()
    role = "assistant" if self_id and sender_id == self_id else "user"

    return {
        "message_id": str(data.get("message_id") or message_id),
        "role": role,
        "text": text,
        "item_type": str(data.get("message_type") or "message"),
        "source": "onebot_get_msg",
    }


def _segments_to_reply_summary(segments: list) -> str:
    parts: list[str] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "").strip().lower()
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if seg_type == "reply":
            continue
        if seg_type == "text":
            text = str(data.get("text") or "").strip()
            if text:
                parts.append(text)
            continue
        if seg_type == "face":
            face_id = str(data.get("id") or "").strip()
            parts.append(f"[QQ表情: face:{face_id}]" if face_id else "[QQ表情]")
            continue
        if seg_type == "mface":
            parts.append("[表情]")
            continue
        if seg_type == "image":
            summary = str(data.get("summary") or "").strip()
            parts.append(summary or "[图片]")
            continue
        if seg_type:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()


def _safe_json_object(text: Optional[str]) -> dict:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def _handle_active_message_setting_text_event(event: ChatEvent) -> Optional[dict]:
    setting = await _extract_active_message_setting(event.text or "", event.session_id)
    if setting["type"] == "none":
        return None

    runtime = _runtime_for_event(event)
    await runtime.gate.record_non_chat_user_activity(event)
    _record_active_message_setting_user_event(event)
    await _emit_conversation_changed("incoming_event", event)

    success, response_text = _apply_active_message_setting(event.session_id, setting)
    runtime.gate.record_command("active_message.setting", "success" if success else "error")
    await _record_command_response(event.session_id, "active_message.setting", response_text, success)
    await _emit_message({
        "type": "assistant_message",
        "session_id": event.session_id,
        "action": "ACTIVE_MESSAGE_SETTING",
        "text": response_text,
        "content": response_text,
        "item_type": "text",
        "visible": True,
        "is_meme": False,
        "meme_path": None,
        **_message_target_from_event(event),
    })
    await _emit_conversation_changed("assistant_command_response", session_id=event.session_id)
    return {
        "handled": True,
        "type": "active_message_setting",
        "active_message_setting_updated": success,
        "response": response_text,
    }


async def _extract_active_message_setting(text: str, session_id: str) -> dict:
    now = datetime.now()
    if not looks_like_active_message_setting_request(text):
        return dict(ACTIVE_SETTING_NONE)

    llm = _internal_llm_for_session(session_id)
    if getattr(llm, "api_key", ""):
        try:
            messages = build_active_message_setting_messages(
                content=text,
                current_time=now.strftime("%Y-%m-%d %H:%M"),
            )
            raw = await llm.chat_completion(messages=messages, temperature=0.0)
            setting = parse_active_message_setting_output(raw, now=now)
            if setting["type"] != "none":
                return setting
        except Exception as exc:
            print(f"[active-message-setting] LLM extraction failed, fallback to local parse: {exc}")

    return fallback_active_message_setting_from_text(text, now=now)


def _apply_active_message_setting(session_id: str, setting: dict) -> tuple[bool, str]:
    normalized = normalize_active_message_setting(setting)
    if normalized["type"] == "none":
        return False, "这条我先不改主动消息时间。"

    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    config = _normalize_active_message_config(load_settings().get("active_message"))
    sessions = dict(config.get("sessions") or {})
    session_cfg = dict(sessions.get(sid) or {})
    if normalized["type"] == "next":
        session_cfg["next_active_at"] = normalized["time"]
    elif normalized["type"] == "daily":
        session_cfg["daily_time"] = normalized["time"]
    sessions[sid] = session_cfg
    config["sessions"] = sessions

    if not save_settings({"active_message": config}):
        return False, "主动消息时间保存失败。"
    _refresh_active_message_jobs(config)
    return True, format_active_message_setting_response(normalized)


def _record_active_message_setting_user_event(event: ChatEvent):
    db = next(get_db())
    try:
        session_id = normalize_session_id(event.session_id or DEFAULT_SESSION_ID)
        conv = ConversationEvent(
            session_id=session_id,
            event_type="user_command",
            text=event.text,
            is_visible=True,
            action="active_message.setting",
        )
        db.add(conv)
        raw = RawChatLog(
            event_id=event.event_id,
            session_id=session_id,
            event_type="active_message.setting_input",
            platform=event.platform,
            user_id=event.user_id,
            input_text=event.text,
            raw_payload=_json_dumps(_event_payload(event)),
            action="active_message.setting",
            status="received",
        )
        db.add(raw)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


async def handle_onebot_payload(payload: dict) -> None:
    event = parse_onebot_event(payload)
    if not event:
        return

    if _is_group_chat_event(event):
        if event.event_type == EventType.TEXT:
            event_type = _event_type_for_text(event.text or "")
            if event_type != event.event_type:
                event = event.model_copy(update={"event_type": event_type})
        group_window = await group_chat_buffer.append(event)
        group_image_refs = group_image_cache.remember_event(event)
        group_meme_jobs = _enqueue_group_sticker_jobs(event, group_window)
        group_memory_observation = _observe_group_memory_event(event)
        group_activity_update = _maybe_update_group_activity(event.session_id)
        group_repetition = None
        group_memory_command = None
        if event.event_type in (EventType.COMMAND_MEM, EventType.COMMAND_FORGET):
            group_reply_trigger = {"status": "skipped", "reason": "group_memory_command"}
            group_memory_command = await _handle_group_memory_command_event(event)
        else:
            group_repetition = await _handle_group_repetition_event(event)
            if group_repetition and group_repetition.get("status") == "selected":
                group_reply_trigger = {
                    "status": "skipped",
                    "reason": "group_repetition_triggered",
                    "repetition": group_repetition,
                }
            else:
                trigger_policy = _group_policy_gate(
                    event.session_id,
                    trigger_reason=_group_trigger_reason_for_event(event),
                    item_type=SendItemType.TEXT.value,
                )
                if not trigger_policy.get("ok"):
                    group_reply_trigger = {
                        "status": "skipped",
                        "reason": trigger_policy.get("reason") or "group_trigger_policy_blocked",
                        "policy": trigger_policy,
                    }
                else:
                    group_reply_trigger = await _ensure_group_reply_scheduler().on_group_event(event, group_window)
        _record_incoming_event(event)
        await _emit_conversation_changed("incoming_group_event", event)
        await _emit_state({
            "session_id": event.session_id,
            "result": "group_buffered",
            "group_buffer": {
                "buffer_version": group_window["buffer_version"],
                "buffered_events": group_window["buffered_events"],
                "latest": group_window["latest"],
            },
            "group_image_refs": group_image_refs,
            "group_meme_jobs": group_meme_jobs,
            "group_reply_trigger": group_reply_trigger,
            "group_memory_observation": group_memory_observation,
            "group_memory_command": group_memory_command,
            "group_repetition": group_repetition,
            "group_activity_update": group_activity_update,
        })
        return

    init_gate()
    event = await _enrich_onebot_reply_context(event)
    if event.event_type == EventType.TEXT:
        event_type = _event_type_for_text(event.text or "")
        if event_type != event.event_type:
            event = event.model_copy(update={"event_type": event_type})
    runtime = _runtime_for_event(event)

    if event.event_type == EventType.TEXT:
        active_setting_result = await _handle_active_message_setting_text_event(event)
        if active_setting_result:
            return

    _record_incoming_event(event)
    await _emit_conversation_changed("incoming_event", event)
    result = await runtime.gate.handle_event(event)

    if event.event_type in (EventType.COMMAND_MEM, EventType.COMMAND_FORGET):
        await _handle_command_event(event, result)
        return

    if event.event_type == EventType.USER_COMPOSING:
        await _emit_state({
            "session_id": event.session_id,
            "result": "user_composing",
            "composing": runtime.gate.get_user_composing_meta(),
            "target_platform": "qq",
            "target_user_id": event.user_id,
        })


def _is_group_chat_event(event: ChatEvent) -> bool:
    raw = event.raw or {}
    return (
        _is_group_session_id(event.session_id)
        and isinstance(raw, dict)
        and raw.get("message_type") == "group"
    )


def _observe_group_memory_event(event: ChatEvent) -> Optional[dict]:
    if event.event_type != EventType.TEXT:
        return None
    raw = event.raw if isinstance(event.raw, dict) else {}
    sender_qid = str(raw.get("qq_user_id") or event.user_id or "").strip()
    if not sender_qid:
        return {"status": "skipped", "reason": "missing_sender_qid"}
    self_id = str(raw.get("qq_self_id") or "").strip()
    if self_id and self_id == sender_qid:
        return {"status": "skipped", "reason": "self_message"}
    result = group_memory_manager.observe_group_text(
        event.text or "",
        session_id=event.session_id,
        sender_qid=sender_qid,
        timestamp=event.timestamp,
    )
    if result.get("status") in {"identity_observed", "write_failed"}:
        _record_group_memory_observation(event, result)
    return result


def _record_group_memory_observation(event: ChatEvent, payload: dict):
    db = next(get_db())
    try:
        session_id = normalize_session_id(event.session_id)
        raw = event.raw if isinstance(event.raw, dict) else {}
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            event_type="group_memory_observation",
            platform="qq_group",
            user_id=str(raw.get("qq_group_id") or ""),
            input_text=event.text,
            raw_payload=_json_dumps({
                "sender_qid": raw.get("qq_user_id"),
                "group_id": raw.get("qq_group_id"),
                "payload": payload,
            }),
            action="group.memory.observe",
            status="ok" if payload.get("status") == "identity_observed" else "error",
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()


def _maybe_update_group_activity(session_id: str) -> dict:
    sid = normalize_session_id(session_id)
    entries = group_chat_buffer.get_window(sid)
    result = group_activity_tracker.maybe_update(sid, entries)
    if result.get("status") == "updated":
        _record_group_activity_update(sid, result)
    return result


async def run_group_activity_update() -> dict:
    sessions = _group_buffer_session_ids()
    results = []
    for sid in sessions:
        entries = group_chat_buffer.get_window(sid)
        result = group_activity_tracker.maybe_update(sid, entries, force=True)
        results.append(result)
        if result.get("status") == "updated":
            _record_group_activity_update(sid, result)
    return {
        "status": "completed",
        "updated": sum(1 for item in results if item.get("status") == "updated"),
        "sessions": results,
    }


def _group_buffer_session_ids() -> list[str]:
    panel = group_chat_buffer.status()
    sessions = panel.get("sessions") if isinstance(panel, dict) else {}
    if not isinstance(sessions, dict):
        return []
    return sorted(sid for sid in sessions.keys() if _is_group_session_id(sid))


def _record_group_activity_update(session_id: str, payload: dict):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=normalize_session_id(session_id),
            event_type="group_activity_update",
            platform="qq_group",
            raw_payload=_json_dumps(payload),
            action="group.activity.update",
            status=str(payload.get("status") or "debug"),
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()


async def _handle_group_repetition_event(event: ChatEvent) -> Optional[dict]:
    if event.event_type != EventType.TEXT:
        return None
    policy = _group_policy_gate(
        event.session_id,
        trigger_reason="group_repetition",
        item_type=SendItemType.TEXT.value,
    )
    if not policy.get("ok"):
        return {
            "status": "skipped",
            "reason": policy.get("reason") or "group_repetition_policy_blocked",
            "session_id": normalize_session_id(event.session_id),
            "policy": policy,
        }
    raw = event.raw if isinstance(event.raw, dict) else {}
    group_window = group_chat_buffer.get_window(event.session_id)
    evaluation = group_repetition_detector.evaluate(event, group_window)
    if evaluation.get("status") != "selected":
        return evaluation

    trigger = {
        "trigger_id": f"group_repeat_{uuid.uuid4().hex[:8]}",
        "reason": "group_repetition",
        "source_message_id": str(raw.get("onebot_message_id") or evaluation.get("latest_message_id") or ""),
        "reply_to_message_id": None,
        "repetition": evaluation,
    }
    decision = {
        "should_send_candidate": True,
        "final_text": evaluation["text"],
    }
    send_result = await _send_group_reply_candidate(event.session_id, trigger, decision)
    result = {**evaluation, "send_result": send_result}
    _record_group_repetition_event(event, result)
    if send_result.get("status") == "sent":
        await _emit_conversation_changed("assistant_group_repetition", session_id=event.session_id)
    return result


def _record_group_repetition_event(event: ChatEvent, payload: dict):
    raw = event.raw if isinstance(event.raw, dict) else {}
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=normalize_session_id(event.session_id),
            event_type="group_repetition",
            platform="qq_group",
            user_id=str(raw.get("qq_group_id") or ""),
            input_text=event.text,
            raw_payload=_json_dumps(payload),
            action="group.repetition",
            final_text=str(payload.get("text") or ""),
            item_type="text",
            status=str((payload.get("send_result") or {}).get("status") or payload.get("status") or "debug"),
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()


async def _handle_group_memory_command_event(event: ChatEvent) -> dict:
    raw = event.raw if isinstance(event.raw, dict) else {}
    sender_qid = str(raw.get("qq_user_id") or event.user_id or "").strip()
    if event.event_type == EventType.COMMAND_MEM:
        success, response_text, payload = group_memory_manager.apply_mem_command(
            event.text or "",
            session_id=event.session_id,
            sender_qid=sender_qid,
            timestamp=event.timestamp,
        )
        action = "group.command.mem"
    elif event.event_type == EventType.COMMAND_FORGET:
        success, response_text, payload = group_memory_manager.apply_forget_command(
            event.text or "",
            session_id=event.session_id,
            sender_qid=sender_qid,
        )
        action = "group.command.forget"
    else:
        return {"handled": False, "reason": "not_group_memory_command"}

    send_result = await _send_group_reply_candidate(
        event.session_id,
        {
            "trigger_id": f"group_memory_{uuid.uuid4().hex[:8]}",
            "reason": action,
            "source_message_id": str(raw.get("onebot_message_id") or ""),
            "reply_to_message_id": str(raw.get("onebot_message_id") or ""),
        },
        {
            "should_send_candidate": True,
            "final_text": response_text,
            "reply_to_message_id": str(raw.get("onebot_message_id") or ""),
        },
    )
    sent = send_result.get("status") == "sent"
    _record_group_memory_command_response(
        event,
        action,
        response_text,
        success,
        payload,
        visible=sent,
        send_result=send_result,
    )
    if sent:
        await _emit_message({
            "type": "assistant_message",
            "session_id": event.session_id,
            "action": action,
            "text": response_text,
            "content": response_text,
            "item_type": "text",
            "visible": True,
            "is_meme": False,
            "meme_path": None,
            "target_platform": "qq_group",
            "target_group_id": raw.get("qq_group_id"),
        })
        await _emit_conversation_changed("assistant_group_memory_command_response", session_id=event.session_id)
    return {
        "handled": True,
        "action": action,
        "success": success,
        "response": response_text,
        "payload": payload,
        "send_result": send_result,
    }


def _record_group_memory_command_response(
    event: ChatEvent,
    action: str,
    response_text: str,
    success: bool,
    payload: dict,
    visible: bool = True,
    send_result: Optional[dict] = None,
):
    db = next(get_db())
    try:
        session_id = normalize_session_id(event.session_id)
        raw = event.raw if isinstance(event.raw, dict) else {}
        if visible:
            conv = ConversationEvent(
                session_id=session_id,
                event_type="assistant_system",
                text=response_text,
                is_visible=True,
                action=action,
            )
            db.add(conv)
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            event_type="group_memory_command",
            platform="qq_group",
            user_id=str(raw.get("qq_group_id") or ""),
            raw_payload=_json_dumps({
                "action": action,
                "sender_qid": raw.get("qq_user_id"),
                "group_id": raw.get("qq_group_id"),
                "payload": payload,
                "send_result": send_result,
                "visible_recorded": visible,
            }),
            action=action,
            final_text=response_text,
            item_type="text",
            status="ok" if success else "error",
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()


def _enqueue_group_sticker_jobs(event: ChatEvent, group_window: dict) -> dict:
    raw = event.raw if isinstance(event.raw, dict) else {}
    media_refs = raw.get("media_refs") or []
    if not isinstance(media_refs, list):
        return {"queued": 0, "jobs": []}

    sticker_refs = [
        (index, media_ref)
        for index, media_ref in enumerate(media_refs)
        if isinstance(media_ref, dict) and media_ref.get("is_sticker")
    ]
    if not sticker_refs:
        return {"queued": 0, "jobs": []}

    queue = _ensure_media_job_queue()
    jobs = []
    event_payload = _event_payload(event)
    for index, media_ref in sticker_refs:
        media_key = _group_media_harness_key(event, media_ref, index)
        enqueued = queue.enqueue(
            MediaJob(
                media_key=media_key,
                session_id=event.session_id,
                snapshot_id=0,
                buffer_version=int(group_window.get("buffer_version") or 0),
                source_job_id=f"group_meme:{event.event_id}",
                event=copy.deepcopy(event_payload),
                media_ref=copy.deepcopy(media_ref),
                event_text=str(event.text or ""),
                context_text=_group_meme_context_text(event.session_id),
            )
        )
        jobs.append({
            "media_key": media_key,
            "message_id": str(media_ref.get("onebot_message_id") or raw.get("onebot_message_id") or ""),
            "segment_index": media_ref.get("segment_index", index),
            "status": "queued" if enqueued else "already_queued",
        })
    return {
        "queued": sum(1 for job in jobs if job["status"] == "queued"),
        "jobs": jobs,
    }


def _group_media_harness_key(event: ChatEvent, media_ref: dict, index: int) -> str:
    segment_index = media_ref.get("segment_index")
    if segment_index is None:
        segment_index = index
    identity = (
        media_ref.get("file_unique")
        or media_ref.get("file_id")
        or media_ref.get("file")
        or media_ref.get("url")
        or segment_index
    )
    return f"{event.session_id}:{event.event_id}:{segment_index}:{identity}"


def _group_meme_context_text(session_id: str) -> str:
    window = group_chat_buffer.get_model_window(session_id, limit=12)
    return json.dumps({"group_window": window}, ensure_ascii=False)


async def _prepare_group_image_understanding_for_window(
    session_id: str,
    *,
    trigger: dict,
    model_window: list[dict],
) -> tuple[list[dict], list[dict]]:
    sid = normalize_session_id(session_id)
    enhanced_window = copy.deepcopy(model_window or [])
    debug_items: list[dict] = []
    media_queue = _ensure_media_job_queue()

    for entry in enhanced_window:
        if not isinstance(entry, dict):
            continue
        media_items = entry.get("media")
        if not isinstance(media_items, list):
            continue
        for media in media_items:
            if not isinstance(media, dict) or media.get("kind") != "image":
                continue
            media_key = group_image_media_key(sid, media.get("message_id"), media.get("segment_index"))
            if not media_key:
                continue

            prompt_payload = group_image_cache.get_payload(sid, media_key)
            cache_hit = prompt_payload is not None
            cache_write = False
            raw_payloads: list[dict] = []
            if not prompt_payload:
                raw_ref = group_image_cache.get_ref(sid, media_key)
                if raw_ref:
                    job = MediaJob(
                        media_key=media_key,
                        session_id=sid,
                        snapshot_id=0,
                        buffer_version=_safe_int(trigger.get("request_buffer_version"))
                        or _safe_int(trigger.get("source_buffer_version"))
                        or 0,
                        source_job_id=f"group_image_understanding:{trigger.get('trigger_id') or uuid.uuid4().hex[:8]}",
                        event={"raw": {"onebot_message_id": media.get("message_id")}},
                        media_ref=copy.deepcopy(raw_ref),
                        event_text=str(entry.get("text") or "[图片]"),
                        context_text=_group_image_context_text(enhanced_window),
                    )
                    prompt_payload, raw_payloads = await media_queue.process_inline_image_for_prompt(job)
                    _record_media_job_payloads(raw_payloads, job)
                    if prompt_payload:
                        group_image_cache.set_payload(sid, media_key, prompt_payload)
                        cache_write = True
                else:
                    prompt_payload = _missing_group_image_understanding_payload(
                        session_id=sid,
                        media_key=media_key,
                        media=media,
                    )

            compact = _compact_group_image_understanding_for_prompt(prompt_payload)
            if compact:
                media["image"] = compact
            item_debug = {
                "media_key": media_key,
                "message_id": str(media.get("message_id") or ""),
                "segment_index": media.get("segment_index"),
                "status": compact.get("status") if isinstance(compact, dict) else "missing",
                "media_cache_hit": cache_hit,
                "media_cache_write": cache_write,
                "reply_target_candidate": bool(compact.get("reply_target_candidate")) if isinstance(compact, dict) else False,
            }
            debug_items.append(item_debug)
            _record_group_image_understanding_log(sid, item_debug)
    return enhanced_window, debug_items


def _group_image_context_text(model_window: list[dict]) -> str:
    compact_window = copy.deepcopy(model_window or [])
    for entry in compact_window:
        if not isinstance(entry, dict):
            continue
        for media in entry.get("media") or []:
            if isinstance(media, dict):
                media.pop("image", None)
    return json.dumps({"group_window": compact_window}, ensure_ascii=False)


def _compact_group_image_understanding_for_prompt(payload: Optional[dict]) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status") or "unknown")
    if status != "completed":
        return {
            "status": status,
            "instruction": payload.get("instruction") or "图片没有成功看清；不要编造图片内容。",
            "reply_target_candidate": False,
        }

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    compact = {
        "status": "completed",
        "summary": result.get("visible_summary"),
        "relation": result.get("relation_to_context"),
        "intent": result.get("user_intent"),
        "desired_response": result.get("desired_response"),
        "reply_style": result.get("reply_style"),
        "confidence": result.get("confidence"),
        "reply_target_candidate": True,
    }
    return {key: value for key, value in compact.items() if value is not None}


def _missing_group_image_understanding_payload(session_id: str, media_key: str, media: dict) -> dict:
    return {
        "internal_event_harness": "image_understanding_result",
        "media_key": media_key,
        "session_id": session_id,
        "status": "failed",
        "media_ref": {
            "onebot_message_id": str(media.get("message_id") or ""),
            "segment_index": media.get("segment_index"),
            "is_sticker": False,
        },
        "error": "group image media ref is no longer available",
        "visibility": "internal_only_not_visible_to_user",
        "instruction": "图片没有成功看清；不要编造图片内容，可以基于群聊文字轻问或 WAIT。",
    }


def _select_group_reply_target(trigger: dict, model_window: list[dict], should_send: bool) -> Optional[str]:
    if not should_send:
        return None
    explicit = str(trigger.get("reply_to_message_id") or "").strip()
    if explicit:
        return explicit
    latest = model_window[-1] if model_window else {}
    if not isinstance(latest, dict):
        return None
    for media in latest.get("media") or []:
        if not isinstance(media, dict) or media.get("kind") != "image":
            continue
        image = media.get("image") if isinstance(media.get("image"), dict) else {}
        if image.get("status") == "completed" and image.get("reply_target_candidate"):
            return str(media.get("message_id") or "").strip() or None
    return None


def _group_window_with_confirmed_nicknames(model_window: list[dict], qid_to_nickname: dict[str, str]) -> list[dict]:
    known = {
        str(qid): str(name).strip()
        for qid, name in (qid_to_nickname or {}).items()
        if str(qid).strip() and str(name).strip()
    }
    window = copy.deepcopy(model_window or [])
    for item in window:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("qid") or "")
        if qid in known:
            item["nickname"] = known[qid]
        else:
            item.pop("nickname", None)
    return window


def _record_group_image_understanding_log(session_id: str, payload: dict):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            event_type="group_image_understanding",
            raw_payload=_json_dumps(payload),
            job_id=str(payload.get("media_key") or ""),
            status=str(payload.get("status") or "debug"),
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()


async def _send_group_reply_candidate(session_id: str, trigger: dict, decision: dict) -> dict:
    sid = normalize_session_id(session_id)
    group_id = group_id_from_session_id(sid)
    send_key = f"{str(trigger.get('trigger_id') or 'group_reply')}:group_send"
    item_type, content = _group_candidate_send_item(decision)
    reply_to_message_id = str(decision.get("reply_to_message_id") or "").strip() or None
    if not content:
        result = {
            "status": "skipped",
            "reason": "empty_group_candidate",
            "session_id": sid,
            "send_key": send_key,
        }
        _record_onebot_group_send(sid, send_key, group_id, item_type, content, None, result["status"], result["reason"], reply_to_message_id)
        return result

    known_window = group_chat_buffer.get_model_window(sid, limit=50)
    known_qids = known_qids_from_group_window(known_window)
    known_message_ids = known_message_ids_from_group_window(
        known_window,
        extra_message_ids=[trigger.get("source_message_id"), trigger.get("reply_to_message_id"), reply_to_message_id],
    )
    if item_type == SendItemType.TEXT.value:
        safety = classify_group_visible_text(
            content,
            known_qids=known_qids,
            known_message_ids=known_message_ids,
        )
        if not safety.ok:
            result = {
                "status": "skipped",
                "reason": safety.reason or "unsafe_group_visible_text",
                "session_id": sid,
                "group_id": group_id,
                "send_key": send_key,
                "safety": safety.__dict__,
            }
            _record_onebot_group_send(sid, send_key, group_id, item_type, content, None, result["status"], result["reason"], reply_to_message_id)
            await _emit_state({"result": "group_reply_send", **result})
            return result

    limiter = _refresh_group_send_limiter()
    gate = limiter.evaluate(
        sid,
        item_type=item_type,
        content=content,
        reply_to_message_id=reply_to_message_id,
        trigger_reason=str(trigger.get("reason") or ""),
    )
    group_id = str(gate.get("group_id") or group_id)
    if not gate.get("ok"):
        result = {
            "status": "skipped",
            "reason": gate.get("reason") or "group_send_blocked",
            "session_id": sid,
            "group_id": group_id,
            "send_key": send_key,
            "gate": gate,
        }
        _record_onebot_group_send(sid, send_key, group_id, item_type, content, None, result["status"], result["reason"], reply_to_message_id)
        await _emit_state({"result": "group_reply_send", **result})
        return result

    try:
        if item_type == SendItemType.MEME.value or content.startswith("meme:") or content.startswith(":meme:"):
            stem = _meme_stem_from_group_content(content)
            image_path = _meme_path_for_stem(stem)
            if not image_path:
                result = {
                    "status": "skipped",
                    "reason": "meme_not_found",
                    "session_id": sid,
                    "group_id": group_id,
                    "send_key": send_key,
                }
                _record_onebot_group_send(sid, send_key, group_id, item_type, content, None, result["status"], result["reason"], reply_to_message_id)
                await _emit_state({"result": "group_reply_send", **result})
                return result
            response = await onebot_manager.send_group_image(
                group_id,
                Path(image_path).resolve().as_uri(),
                reply_to_message_id=reply_to_message_id,
            )
        elif item_type == "image":
            response = await onebot_manager.send_group_image(
                group_id,
                _sendable_file_uri(content),
                reply_to_message_id=reply_to_message_id,
            )
        else:
            response = await onebot_manager.send_group_text(
                group_id,
                content,
                reply_to_message_id=reply_to_message_id,
            )
        limiter.record_sent(
            sid,
            item_type=item_type,
            content=content,
            reply_to_message_id=reply_to_message_id,
        )
        result = {
            "status": "sent",
            "reason": None,
            "session_id": sid,
            "group_id": group_id,
            "send_key": send_key,
            "onebot_message_id": _onebot_response_message_id(response),
            "gate": gate,
        }
        _record_onebot_group_send(sid, send_key, group_id, item_type, content, response, result["status"], None, reply_to_message_id)
        await _emit_state({"result": "group_reply_send", **result})
        return result
    except Exception as exc:
        result = {
            "status": "error",
            "reason": "onebot_group_send_error",
            "session_id": sid,
            "group_id": group_id,
            "send_key": send_key,
            "error_message": str(exc),
            "gate": gate,
        }
        _record_onebot_group_send(sid, send_key, group_id, item_type, content, None, result["status"], str(exc), reply_to_message_id)
        await _emit_state({"result": "group_reply_send", **result})
        return result


def _group_candidate_send_item(decision: dict) -> tuple[str, str]:
    item_type = str(decision.get("item_type") or SendItemType.TEXT.value).strip().lower()
    content = str(decision.get("content") or decision.get("final_text") or "").strip()
    items = decision.get("items")
    if isinstance(items, list) and items:
        first = items[0] if isinstance(items[0], dict) else {}
        item_type = str(first.get("type") or item_type).strip().lower()
        content = str(first.get("content") or first.get("text") or content).strip()
    if content.startswith("meme:") or content.startswith(":meme:"):
        item_type = SendItemType.MEME.value
    return item_type or SendItemType.TEXT.value, content


def _meme_stem_from_group_content(content: str) -> str:
    text = str(content or "").strip()
    if text.startswith(":meme:"):
        return text[len(":meme:"):].strip()
    if text.startswith("meme:"):
        return text[len("meme:"):].strip()
    return text


def _sendable_file_uri(content: str) -> str:
    value = str(content or "").strip()
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        return value
    path = Path(value)
    if path.exists():
        return path.resolve().as_uri()
    return value


async def _run_group_reply_decision(session_id: str, trigger: dict, model_window: list[dict]) -> dict:
    sid = normalize_session_id(session_id)
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    llm = _internal_llm_for_session(sid)
    send_config = _current_group_send_config()
    group_id = group_id_from_session_id(sid)
    send_policy = evaluate_group_send_policy(
        send_config,
        sid,
        trigger_reason=str(trigger.get("reason") or ""),
        item_type=SendItemType.TEXT.value,
    )
    if not send_policy.get("ok"):
        result = {
            "status": "policy_skipped",
            "action": None,
            "should_send_candidate": False,
            "send_layer_enabled": True,
            "send_config_enabled": send_config["enabled"],
            "group_whitelisted": group_id in set(send_config["allowed_group_ids"]),
            "send_policy": send_policy,
            "final_text": None,
            "reply_to_message_id": None,
            "image_understanding": [],
            "parse_status": "policy_skipped",
            "safety_reason": send_policy.get("reason"),
            "errors": [send_policy.get("reason") or "group_send_policy_blocked"],
            "llm_debug": llm.get_cache_debug(),
        }
        _record_group_reply_decision_log(
            sid,
            trigger=trigger,
            raw_output="",
            result=result,
            buffer_version=trigger.get("request_buffer_version"),
        )
        return result
    window_qids = known_qids_from_group_window(model_window)
    group_memory_context = group_memory_manager.prompt_context(sid, qids=window_qids)
    qid_to_nickname = group_memory_context.get("qid_to_nickname") or {}
    named_window = _group_window_with_confirmed_nicknames(model_window, qid_to_nickname)
    enhanced_window, image_debug = await _prepare_group_image_understanding_for_window(
        sid,
        trigger=trigger,
        model_window=named_window,
    )
    known_qids = known_qids_from_group_window(enhanced_window, qid_to_nickname=qid_to_nickname)
    messages = build_group_chat_messages(
        group_window=enhanced_window,
        trigger={
            **trigger,
            "image_understanding": image_debug,
        },
        qid_to_nickname=qid_to_nickname,
        group_memory=group_memory_context,
        current_time=current_time,
        send_enabled=bool(send_policy.get("ok")),
    )
    known_message_ids = known_message_ids_from_group_window(
        enhanced_window,
        extra_message_ids=[trigger.get("source_message_id"), trigger.get("reply_to_message_id")],
    )
    raw_output = ""
    await _emit_llm_started({"session_id": sid})
    try:
        raw_output = await llm.chat_completion(messages=messages, temperature=0.8, max_tokens=160)
        parsed = parse_group_chat_output(
            raw_output,
            known_qids=known_qids,
            known_message_ids=known_message_ids,
        )
        final_text = parsed.decision.text_bubbles()[0] if parsed.should_send else None
        reply_to_message_id = _select_group_reply_target(
            trigger=trigger,
            model_window=enhanced_window,
            should_send=parsed.should_send,
        )
        status = "candidate_ready_not_sent" if parsed.should_send else ("guarded" if parsed.errors else "wait")
        result = {
            "status": status,
            "action": parsed.decision.action.value,
            "should_send_candidate": parsed.should_send,
            "send_layer_enabled": True,
            "send_config_enabled": send_config["enabled"],
            "group_whitelisted": group_id in set(send_config["allowed_group_ids"]),
            "send_policy": send_policy,
            "final_text": final_text,
            "reply_to_message_id": reply_to_message_id,
            "image_understanding": image_debug,
            "parse_status": parsed.status,
            "safety_reason": parsed.safety.reason,
            "errors": parsed.errors,
            "llm_debug": llm.get_cache_debug(),
        }
        _record_group_reply_decision_log(
            sid,
            trigger=trigger,
            raw_output=raw_output,
            result=result,
            buffer_version=trigger.get("request_buffer_version"),
        )
        return result
    except Exception as exc:
        result = {
            "status": "error",
            "action": None,
            "should_send_candidate": False,
            "send_layer_enabled": True,
            "send_config_enabled": send_config["enabled"],
            "group_whitelisted": group_id in set(send_config["allowed_group_ids"]),
            "send_policy": send_policy,
            "final_text": None,
            "reply_to_message_id": None,
            "image_understanding": image_debug,
            "parse_status": "error",
            "safety_reason": None,
            "errors": [str(exc)],
            "llm_debug": llm.get_cache_debug(),
        }
        _record_group_reply_decision_log(
            sid,
            trigger=trigger,
            raw_output=raw_output,
            result=result,
            buffer_version=trigger.get("request_buffer_version"),
            error_message=str(exc),
        )
        return result
    finally:
        await _emit_llm_finished({"session_id": sid}, reason="group_reply_decision_finished")


def _record_group_reply_decision_log(
    session_id: str,
    *,
    trigger: dict,
    raw_output: str,
    result: dict,
    buffer_version=None,
    error_message: Optional[str] = None,
):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            event_type="group_reply_decision",
            raw_payload=_json_dumps({
                "trigger": trigger,
                "send_layer_enabled": result.get("send_layer_enabled"),
                "send_config_enabled": result.get("send_config_enabled"),
                "group_whitelisted": result.get("group_whitelisted"),
            }),
            llm_raw_output=raw_output,
            parsed_payload=_json_dumps(result),
            action=result.get("action"),
            final_text=result.get("final_text"),
            item_type="text" if result.get("final_text") else None,
            buffer_version=_safe_int(buffer_version),
            job_id=str(trigger.get("trigger_id") or ""),
            status=str(result.get("status") or "debug"),
            error_message=error_message,
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()


async def _record_group_reply_scheduler_event(payload: dict):
    session_id = normalize_session_id(payload.get("session_id") or DEFAULT_SESSION_ID)
    status = str(payload.get("status") or payload.get("event_type") or "debug")
    db = next(get_db())
    try:
        trigger = payload.get("trigger") if isinstance(payload.get("trigger"), dict) else {}
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            event_type=str(payload.get("event_type") or "group_reply_scheduler"),
            raw_payload=_json_dumps(payload),
            buffer_version=_safe_int(trigger.get("source_buffer_version")),
            job_id=str(trigger.get("trigger_id") or payload.get("pending_trigger_id") or ""),
            status=status,
            error_message=str(payload.get("error_message") or "")[:2000] or None,
        )
        db.add(log)
        db.commit()
    except Exception as exc:
        print(f"DB error: {exc}")
    finally:
        db.close()
    await _emit_state({"result": "group_reply_scheduler", **payload})


def _safe_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _handle_command_event(event: ChatEvent, result: Optional[dict] = None) -> dict:
    text = event.text or ""
    target = _message_target_from_event(event)
    if event.event_type == EventType.COMMAND_MEM:
        action = "COMMAND_MEM"
        action_key = "command.mem"
        command = "/mem"
        try:
            active_setting = await _extract_active_message_setting(text, event.session_id)
            if active_setting["type"] != "none":
                success, response_text = _apply_active_message_setting(event.session_id, active_setting)
                action = "ACTIVE_MESSAGE_SETTING"
                action_key = "active_message.setting"
                command = "active_message.setting"
            else:
                success, response_text = await _run_mem_command(text, event.session_id, target)
        except Exception:
            await _emit_llm_finished({**target, "session_id": event.session_id}, "command_error")
            raise
    elif event.event_type == EventType.COMMAND_FORGET:
        action = "COMMAND_FORGET"
        action_key = "command.forget"
        command = "/forget"
        try:
            success, response_text = await _run_forget_command(text, event.session_id, target)
        except Exception:
            await _emit_llm_finished({**target, "session_id": event.session_id}, "command_error")
            raise
    else:
        return result or {"handled": False, "type": "not_command"}

    runtime = _runtime_for_event(event)
    runtime.gate.record_command(command, "success" if success else "error")
    if success and action_key == "command.mem" and hasattr(runtime.graph, "reset_provider_transcript"):
        runtime.graph.reset_provider_transcript(reason=f"{action_key}_memory_updated")
    await _record_command_response(event.session_id, action_key, response_text, success)
    await _emit_llm_finished({**target, "session_id": event.session_id}, "command_response")
    await _emit_message({
        "type": "assistant_message",
        "session_id": event.session_id,
        "action": action,
        "text": response_text,
        "content": response_text,
        "item_type": "text",
        "visible": True,
        "is_meme": False,
        "meme_path": None,
        **_message_target_from_event(event),
    })
    await _emit_conversation_changed("assistant_command_response", session_id=event.session_id)
    return {
        **(result or {}),
        "memory_updated": success and action_key == "command.mem",
        "active_message_setting_updated": success and action_key == "active_message.setting",
        "response": response_text,
    }


async def _run_mem_command(text: str, session_id: str, target: Optional[dict] = None) -> tuple[bool, str]:
    await _emit_llm_started({**(target or {}), "session_id": session_id})
    runtime = _runtime_for_session(session_id)
    return await runtime.memory.apply_mem_via_llm(text, _internal_llm_for_session(session_id))


async def _run_forget_command(text: str, session_id: str, target: Optional[dict] = None) -> tuple[bool, str]:
    await _emit_llm_started({**(target or {}), "session_id": session_id})
    runtime = _runtime_for_session(session_id)
    return await runtime.memory.apply_forget_via_llm(text, _internal_llm_for_session(session_id))


def _event_type_for_text(text: str) -> EventType:
    lowered = text.lower()
    if lowered.startswith("/mem"):
        return EventType.COMMAND_MEM
    if lowered.startswith("/forget"):
        return EventType.COMMAND_FORGET
    return EventType.TEXT


def _record_incoming_event(event: ChatEvent):
    db = next(get_db())
    try:
        session_id = normalize_session_id(event.session_id or DEFAULT_SESSION_ID)
        event_type = (
            "user_command"
            if event.event_type in (EventType.COMMAND_MEM, EventType.COMMAND_FORGET)
            else "user_text"
        )
        if event.event_type == EventType.NUDGE:
            event_type = "nudge"
        elif event.event_type == EventType.RECALL:
            event_type = "recall"
        elif event.event_type == EventType.IMAGE:
            event_type = "user_image"
        elif event.event_type == EventType.STICKER:
            event_type = "user_sticker"
        elif event.event_type == EventType.AUDIO:
            event_type = "user_audio"
        elif event.event_type == EventType.VIDEO:
            event_type = "user_video"
        elif event.event_type == EventType.USER_COMPOSING:
            event_type = "user_composing"

        conv = ConversationEvent(
            session_id=session_id,
            event_type=event_type,
            text=event.text,
            is_visible=event.event_type not in (EventType.USER_COMPOSING,),
            action=event.event_type.value if event.event_type != EventType.TEXT else None,
        )
        db.add(conv)
        raw = RawChatLog(
            event_id=event.event_id,
            session_id=session_id,
            event_type=event.event_type.value,
            platform=event.platform,
            user_id=event.user_id,
            input_text=event.text,
            raw_payload=_json_dumps(_event_payload(event)),
            status="received",
        )
        db.add(raw)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


async def _record_command_response(session_id: str, action: str, response_text: str, success: bool):
    db = next(get_db())
    try:
        conv = ConversationEvent(
            session_id=session_id,
            event_type="assistant_system",
            text=response_text,
            is_visible=True,
            action=action,
        )
        db.add(conv)
        raw = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            event_type=action,
            action=action,
            final_text=response_text,
            item_type="text",
            status="sent" if success else "error",
        )
        db.add(raw)
        db.commit()
    finally:
        db.close()


def _record_llm_error(ctx: ProcessContext, error_message: str):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=ctx.snapshot.session_id,
            event_type="llm_decision",
            snapshot_id=ctx.snapshot.snapshot_id,
            buffer_version=ctx.snapshot.buffer_version,
            job_id=ctx.job_id,
            status="error",
            error_message=error_message[:2000],
        )
        db.add(log)
        db.commit()
    finally:
        db.close()


def _record_decision_drop(ctx: ProcessContext, decision):
    db = next(get_db())
    try:
        graph = _graph_for_context(ctx)
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=ctx.snapshot.session_id,
            event_type="llm_decision",
            llm_raw_output=graph.get_last_llm_raw_output() if graph else None,
            parsed_payload=_json_dumps(decision.to_harness_payload(exclude_none=True)),
            action=decision.action.value,
            final_text=None,
            snapshot_id=ctx.snapshot.snapshot_id,
            buffer_version=ctx.snapshot.buffer_version,
            job_id=ctx.job_id,
            status="dropped",
        )
        db.add(log)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


def _record_job_state(ctx: ProcessContext, status: str, payload: Optional[dict] = None):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=ctx.snapshot.session_id,
            event_type="job_state",
            raw_payload=_json_dumps(payload or {}),
            snapshot_id=ctx.snapshot.snapshot_id,
            buffer_version=ctx.snapshot.buffer_version,
            job_id=ctx.job_id,
            status=status,
        )
        db.add(log)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


def _record_prompt_cache_debug(ctx: ProcessContext, payload: Optional[dict] = None):
    if not payload:
        return
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=ctx.snapshot.session_id,
            event_type="prompt_cache_debug",
            raw_payload=_json_dumps(payload),
            snapshot_id=ctx.snapshot.snapshot_id,
            buffer_version=ctx.snapshot.buffer_version,
            job_id=ctx.job_id,
            status="debug",
        )
        db.add(log)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


def _record_media_harness_debug(ctx: ProcessContext, payloads: Optional[list[dict]] = None):
    if not payloads:
        return
    db = next(get_db())
    try:
        for payload in payloads:
            log = RawChatLog(
                event_id=f"evt_{uuid.uuid4().hex[:8]}",
                session_id=ctx.snapshot.session_id,
                event_type=str(payload.get("internal_event_harness") or "media_harness"),
                raw_payload=_json_dumps(payload),
                snapshot_id=ctx.snapshot.snapshot_id,
                buffer_version=ctx.snapshot.buffer_version,
                job_id=ctx.job_id,
                status=str(payload.get("status") or "debug"),
            )
            db.add(log)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


async def _record_background_media_payloads(payloads: list[dict], job: MediaJob):
    _record_media_job_payloads(payloads, job)
    group_meme_update = await _update_group_meme_from_payloads(payloads, job)
    await _emit_state({
        "session_id": job.session_id,
        "result": "media_job_finished",
        "media_job_id": job.job_id,
        "media_key": job.media_key,
        "payloads": payloads,
        "group_meme_update": group_meme_update,
    })
    for payload in _media_followup_payloads(payloads):
        asyncio.create_task(_dispatch_media_followup_when_idle(job, payload))


async def _update_group_meme_from_payloads(payloads: list[dict], job: MediaJob) -> Optional[dict]:
    if not _is_group_session_id(job.session_id):
        return None
    updates = []
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        if payload.get("internal_event_harness") != "meme_intake_result":
            continue
        media_ref = payload.get("media_ref") if isinstance(payload.get("media_ref"), dict) else {}
        message_id = str(
            media_ref.get("onebot_message_id")
            or job.media_ref.get("onebot_message_id")
            or ""
        )
        if not message_id:
            continue
        segment_index = media_ref.get("segment_index", job.media_ref.get("segment_index"))
        meme = _meme_name_from_intake_payload(payload)
        update = await group_chat_buffer.update_meme_result(
            session_id=job.session_id,
            message_id=message_id,
            segment_index=segment_index,
            meme=meme,
            intake_status=str(payload.get("status") or ""),
            media_key=str(payload.get("media_key") or job.media_key or ""),
        )
        updates.append({
            "message_id": message_id,
            "segment_index": segment_index,
            "meme": update.get("meme"),
            "updated": update.get("updated"),
            "buffer_version": update.get("buffer_version"),
        })
    if not updates:
        return None
    return {"updated": any(item.get("updated") for item in updates), "items": updates}


def _meme_name_from_intake_payload(payload: dict) -> str:
    save_result = payload.get("save_result")
    if isinstance(save_result, dict):
        status = str(save_result.get("status") or payload.get("status") or "")
        file_stem = str(save_result.get("file_stem") or "").strip()
        if status in {"saved", "duplicate"} and file_stem:
            return file_stem
    return "unknown"


def _media_followup_payloads(payloads: Optional[list[dict]]) -> list[dict]:
    return []


async def _dispatch_media_followup_when_idle(job: MediaJob, payload: dict):
    media_key = str(payload.get("media_key") or job.media_key or "").strip()
    if not media_key or media_key in _media_followup_keys:
        return
    _media_followup_keys.add(media_key)

    for attempt in range(20):
        runtime = _runtime_for_session(job.session_id)
        dispatched = await runtime.gate.dispatch_internal_followup(
            "media_followup",
            {
                **payload,
                "session_id": job.session_id,
                "source_job_id": job.source_job_id,
            },
        )
        if dispatched.get("dispatched"):
            await _emit_state({
                "session_id": job.session_id,
                "result": "media_followup_dispatched",
                "media_job_id": job.job_id,
                "media_key": media_key,
                "job_id": dispatched.get("job_id"),
                "snapshot_id": dispatched.get("snapshot_id"),
            })
            return
        if dispatched.get("reason") != "pending_job":
            break
        await asyncio.sleep(0.5)

    await _emit_state({
        "session_id": job.session_id,
        "result": "media_followup_skipped",
        "media_job_id": job.job_id,
        "media_key": media_key,
        "reason": "gate_busy",
    })


def _record_media_job_payloads(payloads: Optional[list[dict]], job: MediaJob):
    if not payloads:
        return
    db = next(get_db())
    try:
        for payload in payloads:
            log = RawChatLog(
                event_id=f"{job.job_id}:{uuid.uuid4().hex[:8]}",
                session_id=job.session_id,
                event_type=str(payload.get("internal_event_harness") or "media_job"),
                raw_payload=_json_dumps(payload),
                snapshot_id=job.snapshot_id,
                buffer_version=job.buffer_version,
                job_id=job.source_job_id,
                status=str(payload.get("status") or "debug"),
            )
            db.add(log)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


def _record_assistant_send(
    ctx: ProcessContext,
    decision,
    item: SendItem,
    routed: dict,
    send_index: int,
    send_count: int,
    send_key: str,
):
    db = next(get_db())
    try:
        graph = _graph_for_context(ctx)
        log = RawChatLog(
            event_id=send_key,
            session_id=ctx.snapshot.session_id,
            event_type="llm_decision",
            llm_raw_output=graph.get_last_llm_raw_output() if graph else None,
            parsed_payload=_json_dumps(decision.to_harness_payload(exclude_none=True)),
            action=decision.action.value,
            final_text=item.content,
            item_type=item.type.value,
            send_index=send_index,
            send_count=send_count,
            send_key=send_key,
            snapshot_id=ctx.snapshot.snapshot_id,
            buffer_version=ctx.snapshot.buffer_version,
            job_id=ctx.job_id,
            status=f"sent:{send_index + 1}/{send_count}",
        )
        db.add(log)

        event_type = "assistant_react" if item.type in (SendItemType.EMOJI, SendItemType.MEME) else "assistant_text"
        conv = ConversationEvent(
            session_id=ctx.snapshot.session_id,
            event_type=event_type,
            text=item.content,
            is_visible=True,
            action=routed.get("action") or decision.action.value,
        )
        db.add(conv)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()


async def _active_message_next_retry_sleep():
    if ACTIVE_MESSAGE_NEXT_RETRY_DELAY_SECONDS > 0:
        await asyncio.sleep(ACTIVE_MESSAGE_NEXT_RETRY_DELAY_SECONDS)


async def _send_active_message_fixed_text(
    *,
    sid: str,
    target: dict,
    gate,
    graph,
    job_id: str,
    text: str,
    raw_output: str,
    due_status: dict,
    clear_next: bool = False,
    extra_result: Optional[dict] = None,
) -> dict:
    from ..core.router import ActionRouter

    decision = ActionDecision(
        action=Action.REPLY,
        items=[SendItem(type=SendItemType.TEXT, content=text)],
    )
    routed = ActionRouter().route(decision)
    active_items = list(routed.get("items") or [])
    active_item = active_items[0] if active_items else None
    _record_active_message(sid, job_id, raw_output, decision, routed)
    gate.record_decision(decision)
    await gate.finish_active_message_job(job_id, "sent")
    if graph and routed["text"]:
        graph.commit_external_assistant_text(routed["text"])
    await _emit_message({
        "type": "assistant_message",
        "session_id": sid,
        "action": decision.action.value,
        "text": routed["text"],
        "content": routed["text"],
        "item_type": active_item.type.value if active_item else "text",
        "visible": True,
        "is_meme": False,
        "meme_path": None,
        "source": "active_message",
        **target,
    })
    next_cleared = True
    if clear_next:
        next_cleared = _clear_active_message_next_time(sid)
    result = {
        "status": "sent",
        "job_id": job_id,
        "action": decision.action.value,
        "schedule": due_status,
    }
    if extra_result:
        result.update(extra_result)
    if not next_cleared:
        result["next_clear_error"] = "failed_to_clear_next_active_time"
    return result


async def run_active_message_once(
    manual: bool = False,
    session_id: Optional[str] = None,
    scheduled_source: Optional[str] = None,
    scheduled_time: Optional[str] = None,
) -> dict:
    """执行一次主动消息检查。

    MVP 只做一次轻量开场：有 pending 候选、处于 COLD、无未处理输入、未超每日上限时才发送。
    """
    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    if _is_group_session_id(sid):
        return {"status": "skipped", "reason": "group_readonly_session"}
    runtime = _runtime_for_session(sid)
    gate = runtime.gate
    graph = runtime.graph
    active_llm = _internal_llm_for_session(sid)
    memory = runtime.memory
    target = _message_target_from_session_id(sid)
    config = _normalize_active_message_config(load_settings().get("active_message"))
    now = datetime.now()

    if not config["enabled"]:
        return {"status": "skipped", "reason": "disabled"}
    due_status = {"due": True, "source": "manual", "time": now.strftime("%H:%M")}
    if not manual:
        due_status = _active_message_scheduled_status(
            config=config,
            session_id=sid,
            source=scheduled_source or "global",
            scheduled_time=scheduled_time,
        )
        if not due_status["due"]:
            return {"status": "skipped", "reason": due_status["reason"], "schedule": due_status}
    if _active_messages_sent_today(sid) >= config["daily_limit"]:
        return {"status": "skipped", "reason": "daily_limit"}
    if _has_unanswered_active_message(sid):
        return {"status": "skipped", "reason": "unanswered_backoff"}

    job_id = await gate.reserve_active_message_job()
    if not job_id:
        return {"status": "skipped", "reason": "gate_busy"}

    llm_started = False
    try:
        topics = memory.read_tomorrow_topics()
        topics, expired_count = expire_stale_candidates(topics)
        if expired_count and not memory.write_tomorrow_topics(topics):
            await gate.finish_active_message_job(job_id, "dropped")
            return {"status": "error", "reason": "failed_to_expire_candidates"}

        candidate = select_pending_candidate(topics)
        if not candidate:
            if due_status.get("source") == "next":
                return await _send_active_message_fixed_text(
                    sid=sid,
                    target=target,
                    gate=gate,
                    graph=graph,
                    job_id=job_id,
                    text=ACTIVE_MESSAGE_NEXT_FALLBACK_TEXT,
                    raw_output="system:next_active_at_without_candidate",
                    due_status=due_status,
                    clear_next=True,
                    extra_result={"fallback_reason": "no_candidate"},
                )
            await gate.finish_active_message_job(job_id, "dropped")
            return {"status": "skipped", "reason": "no_candidate"}

        if not active_llm.api_key:
            await gate.finish_active_message_job(job_id, "dropped")
            return {"status": "skipped", "reason": "llm_not_configured"}

        await _emit_llm_started(target)
        llm_started = True
        messages = build_active_message_messages(
            candidate_section=candidate.section,
            candidate_text=candidate.text,
            soul_md=memory.read_soul(),
            memory_core_md=memory.read_memory_core(),
            current_time=now.strftime("%H:%M"),
        )
        is_next_schedule = due_status.get("source") == "next"
        max_llm_attempts = 1 + (ACTIVE_MESSAGE_NEXT_LLM_RETRY_COUNT if is_next_schedule else 0)
        last_llm_error = None
        raw_output = None
        for attempt_index in range(max_llm_attempts):
            if attempt_index > 0:
                await _active_message_next_retry_sleep()
                if not await gate.is_active_message_job_current(job_id):
                    gate.mark_job_stale_dropped(job_id)
                    await gate.dispatch_latest_after_stale(job_id)
                    return {
                        "status": "skipped",
                        "reason": "stale_dropped",
                        "schedule": due_status,
                        "llm_attempts": attempt_index,
                    }
            try:
                raw_output = await active_llm.chat_completion(messages=messages, temperature=0.3)
                break
            except Exception as exc:
                last_llm_error = exc
                if not is_next_schedule:
                    raise

        if raw_output is None:
            if is_next_schedule:
                if not await gate.is_active_message_job_current(job_id):
                    gate.mark_job_stale_dropped(job_id)
                    await gate.dispatch_latest_after_stale(job_id)
                    return {
                        "status": "skipped",
                        "reason": "stale_dropped",
                        "schedule": due_status,
                        "llm_attempts": max_llm_attempts,
                    }
                error_summary = str(last_llm_error) if last_llm_error else "unknown error"
                return await _send_active_message_fixed_text(
                    sid=sid,
                    target=target,
                    gate=gate,
                    graph=graph,
                    job_id=job_id,
                    text=ACTIVE_MESSAGE_NEXT_FALLBACK_TEXT,
                    raw_output=f"system:next_active_at_llm_retry_exhausted:{error_summary}",
                    due_status=due_status,
                    clear_next=True,
                    extra_result={
                        "fallback_reason": "llm_retry_exhausted",
                        "llm_attempts": max_llm_attempts,
                    },
                )
            raise last_llm_error or RuntimeError("active message LLM call failed")

        decision = parse_active_decision(raw_output)

        if not await gate.is_active_message_job_current(job_id):
            gate.mark_job_stale_dropped(job_id)
            await gate.dispatch_latest_after_stale(job_id)
            return {"status": "skipped", "reason": "stale_dropped"}

        if decision.action == Action.WAIT:
            if not memory.write_tomorrow_topics(
                mark_candidate_status(memory.read_tomorrow_topics(), candidate, "blocked")
            ):
                await gate.finish_active_message_job(job_id, "dropped")
                return {"status": "error", "reason": "failed_to_block_candidate"}
            await gate.finish_active_message_job(job_id, "dropped")
            await _emit_state({"session_id": sid, "job_id": job_id, "result": "active_wait"})
            return {"status": "skipped", "reason": "llm_wait"}

        from ..core.router import ActionRouter
        routed = ActionRouter().route(decision)
        if not routed["visible"] or not routed["text"]:
            await gate.finish_active_message_job(job_id, "dropped")
            await _emit_state({"session_id": sid, "job_id": job_id, "result": "active_dropped"})
            return {"status": "skipped", "reason": "not_visible"}
        active_items = list(routed.get("items") or [])
        active_item = active_items[0] if active_items else None

        if not memory.write_tomorrow_topics(
            mark_candidate_status(memory.read_tomorrow_topics(), candidate, "used")
        ):
            await gate.finish_active_message_job(job_id, "dropped")
            return {"status": "error", "reason": "failed_to_mark_used"}

        _record_active_message(sid, job_id, raw_output, decision, routed)
        gate.record_decision(decision)
        await gate.finish_active_message_job(job_id, "sent")

        if graph and routed["text"]:
            graph.commit_external_assistant_text(routed["text"])

        await _emit_message({
            "type": "assistant_message",
            "session_id": sid,
            "action": decision.action.value,
            "text": routed["text"],
            "content": routed["text"],
            "item_type": active_item.type.value if active_item else "text",
            "visible": True,
            "is_meme": routed.get("is_meme", False),
            "meme_path": active_item.content[5:] if active_item and active_item.type == SendItemType.MEME else None,
            "source": "active_message",
            **target,
        })
        result = {
            "status": "sent",
            "job_id": job_id,
            "action": decision.action.value,
            "candidate_section": candidate.section,
            "schedule": due_status,
        }
        if due_status.get("source") == "next" and not _clear_active_message_next_time(sid):
            result["next_clear_error"] = "failed_to_clear_next_active_time"
        return result
    except Exception as e:
        await gate.finish_active_message_job(job_id, "dropped")
        return {"status": "error", "reason": str(e)}
    finally:
        if llm_started:
            await _emit_llm_finished(target, reason="active_message_finished")


async def run_scheduled_active_messages(
    session_id: Optional[str],
    source: str,
    scheduled_time: Optional[str],
) -> dict:
    if source == "global":
        return await run_active_messages_for_global_default(scheduled_time)
    if not session_id:
        return {"status": "error", "reason": "missing_session_id", "source": source}
    return await run_active_message_once(
        manual=False,
        session_id=session_id,
        scheduled_source=source,
        scheduled_time=scheduled_time,
    )


async def run_active_messages_for_global_default(scheduled_time: Optional[str] = None) -> dict:
    config = _normalize_active_message_config(load_settings().get("active_message"))
    results = {}
    for sid in _scheduler_session_ids():
        if _active_message_has_user_override(config, sid):
            results[sid] = {
                "status": "skipped",
                "reason": "user_override",
                "schedule": _active_message_scheduled_status(
                    config=config,
                    session_id=sid,
                    source="global",
                    scheduled_time=scheduled_time,
                ),
            }
            continue
        results[sid] = await run_active_message_once(
            manual=False,
            session_id=sid,
            scheduled_source="global",
            scheduled_time=scheduled_time,
        )
    return {"status": "completed", "source": "global", "results": results}


async def run_active_messages_for_all_sessions() -> dict:
    return await run_active_messages_for_global_default()


def _record_active_message(session_id: str, job_id: str, raw_output: str, decision, routed: dict):
    db = next(get_db())
    try:
        first_item = (routed.get("items") or [None])[0]
        raw = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            event_type="active_message",
            llm_raw_output=raw_output,
            parsed_payload=_json_dumps(decision.to_harness_payload(exclude_none=True)),
            action=decision.action.value,
            final_text=routed.get("text"),
            item_type=first_item.type.value if first_item else None,
            send_index=0 if first_item else None,
            send_count=1 if first_item else None,
            job_id=job_id,
            status="sent",
        )
        db.add(raw)

        event_type = (
            "assistant_react"
            if first_item and first_item.type in (SendItemType.EMOJI, SendItemType.MEME)
            else "assistant_text"
        )
        conv = ConversationEvent(
            session_id=session_id,
            event_type=event_type,
            text=routed.get("text"),
            is_visible=True,
            action="active_message",
        )
        db.add(conv)
        db.commit()
    finally:
        db.close()


def _normalize_active_message_config(config: Optional[dict]) -> dict:
    merged = dict(ACTIVE_MESSAGE_DEFAULTS)
    if isinstance(config, dict):
        merged.update(config)

    return {
        "enabled": bool(merged.get("enabled")),
        "hour": _clamp_int(merged.get("hour"), 0, 23, ACTIVE_MESSAGE_DEFAULTS["hour"]),
        "minute": _clamp_int(merged.get("minute"), 0, 59, ACTIVE_MESSAGE_DEFAULTS["minute"]),
        "daily_limit": _clamp_int(merged.get("daily_limit"), 1, 3, ACTIVE_MESSAGE_DEFAULTS["daily_limit"]),
        "sessions": _normalize_active_message_sessions(merged.get("sessions")),
    }


def _normalize_active_message_sessions(value) -> dict:
    if not isinstance(value, dict):
        return {}
    sessions = {}
    for raw_sid, raw_cfg in value.items():
        sid = normalize_session_id(str(raw_sid or ""))
        if not sid or not isinstance(raw_cfg, dict):
            continue
        if _is_group_session_id(sid):
            continue
        cfg = {}
        daily_time = _normalize_hhmm(raw_cfg.get("daily_time"))
        next_active_at = _normalize_active_datetime(raw_cfg.get("next_active_at"))
        if daily_time:
            cfg["daily_time"] = daily_time
        if next_active_at:
            cfg["next_active_at"] = next_active_at
        if cfg:
            sessions[sid] = cfg
    return sessions


def _clamp_int(value, min_value: int, max_value: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _normalize_hhmm(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"


def _normalize_active_datetime(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M")


def _normalize_topic_template_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in (value or "").strip().splitlines()).strip()


def _is_non_initial_tomorrow_topics(markdown: str) -> bool:
    normalized = _normalize_topic_template_text(markdown)
    if not normalized:
        return False
    return normalized != _normalize_topic_template_text(TOMORROW_TOPICS_TEMPLATE)


def _read_session_tomorrow_topics(session_id: str) -> str:
    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    path = Path(session_registry.session_dir(sid)) / "TOMORROW_TOPICS.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[active-message-monitor] failed to read topics for {sid}: {exc}")
        return ""


def _active_message_next_activation_time(config: dict, session_id: str, now: datetime) -> str:
    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    session_cfg = _active_message_session_config(config, sid)
    now_minute = now.replace(second=0, microsecond=0)

    next_active_at = _normalize_active_datetime(session_cfg.get("next_active_at"))
    if next_active_at:
        next_dt = datetime.strptime(next_active_at, "%Y-%m-%d %H:%M")
        if next_dt >= now_minute:
            return next_dt.strftime("%Y-%m-%d %H:%M")

    daily_time = _normalize_hhmm(session_cfg.get("daily_time")) or f"{config['hour']:02d}:{config['minute']:02d}"
    hour, minute = [int(part) for part in daily_time.split(":", 1)]
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target < now_minute:
        target += timedelta(days=1)
    return target.strftime("%Y-%m-%d %H:%M")


def _active_message_monitor_rows(config: dict, now: datetime) -> list[dict]:
    if not config.get("enabled"):
        return []

    rows = []
    for item in _list_sessions():
        sid = normalize_session_id(item.get("session_id") or DEFAULT_SESSION_ID)
        if _is_group_session_id(sid):
            continue
        topics = _read_session_tomorrow_topics(sid)
        if not _is_non_initial_tomorrow_topics(topics):
            continue
        rows.append({
            "session_id": sid,
            "next_time_to_activate": _active_message_next_activation_time(config, sid, now),
        })
    return sorted(rows, key=lambda row: row["session_id"])


def _active_message_due_status(config: dict, session_id: str, now: datetime) -> dict:
    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    session_cfg = (config.get("sessions") or {}).get(sid) or {}
    next_active_at = _normalize_active_datetime(session_cfg.get("next_active_at"))
    if next_active_at:
        due_at = datetime.strptime(next_active_at, "%Y-%m-%d %H:%M")
        if now.replace(second=0, microsecond=0) >= due_at:
            return {"due": True, "source": "next", "time": next_active_at}
        return {"due": False, "reason": "not_due", "source": "next", "time": next_active_at}

    daily_time = _normalize_hhmm(session_cfg.get("daily_time"))
    source = "daily"
    if not daily_time:
        daily_time = f"{config['hour']:02d}:{config['minute']:02d}"
        source = "global"
    if now.strftime("%H:%M") == daily_time:
        return {"due": True, "source": source, "time": daily_time}
    return {"due": False, "reason": "not_due", "source": source, "time": daily_time}


def _active_message_session_config(config: dict, session_id: str) -> dict:
    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    return dict((config.get("sessions") or {}).get(sid) or {})


def _active_message_pending_next_time(session_cfg: dict, now: Optional[datetime] = None) -> Optional[str]:
    next_active_at = _normalize_active_datetime(session_cfg.get("next_active_at"))
    if not next_active_at:
        return None
    now = now or datetime.now()
    due_at = datetime.strptime(next_active_at, "%Y-%m-%d %H:%M")
    if now.replace(second=0, microsecond=0) <= due_at:
        return next_active_at
    return None


def _active_message_has_user_override(config: dict, session_id: str) -> bool:
    session_cfg = _active_message_session_config(config, session_id)
    return bool(
        _normalize_hhmm(session_cfg.get("daily_time"))
        or _active_message_pending_next_time(session_cfg)
    )


def _active_message_scheduled_status(
    *,
    config: dict,
    session_id: str,
    source: str,
    scheduled_time: Optional[str],
) -> dict:
    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    session_cfg = _active_message_session_config(config, sid)
    source = source if source in {"global", "daily", "next"} else "global"

    if source == "next":
        expected_time = _normalize_active_datetime(scheduled_time)
        configured_time = _normalize_active_datetime(session_cfg.get("next_active_at"))
        if not expected_time or configured_time != expected_time:
            return {
                "due": False,
                "reason": "stale_next_job",
                "source": "next",
                "time": expected_time,
                "configured_time": configured_time,
            }
        return {"due": True, "source": "next", "time": expected_time}

    pending_next_active_at = _active_message_pending_next_time(session_cfg)
    if source == "daily":
        expected_time = _normalize_hhmm(scheduled_time)
        configured_time = _normalize_hhmm(session_cfg.get("daily_time"))
        if pending_next_active_at:
            return {
                "due": False,
                "reason": "next_active_time_pending",
                "source": "daily",
                "time": expected_time,
                "next_active_at": pending_next_active_at,
            }
        if not expected_time or configured_time != expected_time:
            return {
                "due": False,
                "reason": "stale_daily_job",
                "source": "daily",
                "time": expected_time,
                "configured_time": configured_time,
            }
        return {"due": True, "source": "daily", "time": expected_time}

    configured_daily = _normalize_hhmm(session_cfg.get("daily_time"))
    if pending_next_active_at:
        return {
            "due": False,
            "reason": "user_next_override",
            "source": "global",
            "time": scheduled_time,
            "next_active_at": pending_next_active_at,
        }
    if configured_daily:
        return {
            "due": False,
            "reason": "user_daily_override",
            "source": "global",
            "time": scheduled_time,
            "daily_time": configured_daily,
        }

    expected_time = _normalize_hhmm(scheduled_time) or f"{config['hour']:02d}:{config['minute']:02d}"
    configured_time = f"{config['hour']:02d}:{config['minute']:02d}"
    if expected_time != configured_time:
        return {
            "due": False,
            "reason": "stale_global_job",
            "source": "global",
            "time": expected_time,
            "configured_time": configured_time,
        }
    return {"due": True, "source": "global", "time": expected_time}


def _clear_active_message_next_time(session_id: str) -> bool:
    sid = normalize_session_id(session_id or DEFAULT_SESSION_ID)
    config = _normalize_active_message_config(load_settings().get("active_message"))
    sessions = dict(config.get("sessions") or {})
    session_cfg = dict(sessions.get(sid) or {})
    if "next_active_at" not in session_cfg:
        return True
    session_cfg.pop("next_active_at", None)
    if session_cfg:
        sessions[sid] = session_cfg
    else:
        sessions.pop(sid, None)
    config["sessions"] = sessions
    if not save_settings({"active_message": config}):
        return False
    _refresh_active_message_jobs(config)
    return True


def _active_messages_sent_today(session_id: str = DEFAULT_SESSION_ID) -> int:
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    db = next(get_db())
    try:
        return (
            db.query(ConversationEvent)
            .filter(ConversationEvent.session_id == session_id)
            .filter(ConversationEvent.created_at >= start)
            .filter(ConversationEvent.action == "active_message")
            .count()
        )
    finally:
        db.close()


def _has_unanswered_active_message(session_id: str = DEFAULT_SESSION_ID) -> bool:
    db = next(get_db())
    try:
        last_active = (
            db.query(ConversationEvent)
            .filter(ConversationEvent.session_id == session_id)
            .filter(ConversationEvent.action == "active_message")
            .order_by(desc(ConversationEvent.created_at))
            .first()
        )
        if not last_active:
            return False

        last_user = (
            db.query(ConversationEvent)
            .filter(ConversationEvent.session_id == session_id)
            .filter(ConversationEvent.event_type.in_(["user_text", "user_command"]))
            .order_by(desc(ConversationEvent.created_at))
            .first()
        )
        return not last_user or last_user.created_at <= last_active.created_at
    finally:
        db.close()


@router.post("/api/nudge")
async def send_nudge(payload: Optional[NudgeRequest] = None):
    session_id = webui_session_id(payload.session_id if payload else None)
    runtime = _runtime_for_session(session_id)
    event = ChatEvent(
        event_id=f"evt_{uuid.uuid4().hex[:8]}",
        session_id=session_id,
        event_type=EventType.NUDGE,
        text="拍了拍你",
        timestamp=datetime.now(),
        raw={"source": "webui"},
    )
    _record_incoming_event(event)
    await _emit_conversation_changed("incoming_event", event)
    result = await runtime.gate.handle_event(event)
    return result


@router.get("/api/soul")
async def get_soul():
    return {"content": memory_manager.read_soul()}


@router.post("/api/soul")
async def update_soul(config: SoulConfig):
    success = memory_manager.write_soul(config.content)
    return {"status": "ok" if success else "error"}


@router.get("/api/memory/schedule")
async def get_memory_schedule():
    return scheduler_manager.get_schedules()


@router.post("/api/memory/schedule")
async def update_memory_schedule(config: MemoryScheduleConfig):
    scheduler_manager.update_schedule(
        "memory_analysis_day",
        config.memory_analysis_day_hour,
        config.memory_analysis_day_minute,
    )
    scheduler_manager.update_schedule(
        "memory_analysis_night",
        config.memory_analysis_night_hour,
        config.memory_analysis_night_minute,
    )
    scheduler_manager.update_schedule(
        "midnight_cleanup",
        config.midnight_cleanup_hour,
        config.midnight_cleanup_minute,
    )
    save_settings({
        "memory_analysis_day": {"hour": config.memory_analysis_day_hour, "minute": config.memory_analysis_day_minute},
        "memory_analysis_night": {"hour": config.memory_analysis_night_hour, "minute": config.memory_analysis_night_minute},
        "midnight_cleanup": {"hour": config.midnight_cleanup_hour, "minute": config.midnight_cleanup_minute},
    })
    return {"status": "ok"}


@router.get("/api/active-message/config")
async def get_active_message_config():
    return _normalize_active_message_config(load_settings().get("active_message"))


@router.get("/api/active-message/monitor")
async def get_active_message_monitor():
    now = datetime.now()
    config = _normalize_active_message_config(load_settings().get("active_message"))
    return {
        "enabled": config["enabled"],
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "rows": _active_message_monitor_rows(config, now),
    }


@router.post("/api/active-message/config")
async def update_active_message_config(config: ActiveMessageConfig):
    init_gate()
    current = _normalize_active_message_config(load_settings().get("active_message"))
    payload = config.model_dump(exclude_none=True)
    if "sessions" not in payload:
        payload["sessions"] = current.get("sessions", {})
    normalized = _normalize_active_message_config(payload)
    if not save_settings({"active_message": normalized}):
        return {"status": "error", "reason": "failed_to_save_settings", "config": normalized}
    refreshed = _refresh_active_message_jobs(normalized)
    return {"status": "ok", "config": refreshed}


@router.get("/api/active-message/status")
async def get_active_message_status(session_id: Optional[str] = Query(None)):
    sid = _session_id_from_query(session_id)
    runtime = _runtime_for_session(sid)
    topics = runtime.memory.read_tomorrow_topics()
    config = _normalize_active_message_config(load_settings().get("active_message"))
    return {
        "session_id": sid,
        "config": config,
        "schedule": _active_message_due_status(config, sid, datetime.now()),
        "sent_today": _active_messages_sent_today(sid),
        "has_unanswered_active_message": _has_unanswered_active_message(sid),
        "candidates": count_candidates_by_status(topics),
        "gate_ready": await runtime.gate.can_accept_active_message() if hasattr(runtime.gate, "can_accept_active_message") else None,
    }


@router.post("/api/active-message/run")
async def run_active_message_now(session_id: Optional[str] = Query(None)):
    return await run_active_message_once(manual=True, session_id=_session_id_from_query(session_id))


@router.get("/api/memes/stats")
async def get_meme_stats():
    return meme_catalog.get_stats()


@router.get("/api/memes/categories")
async def get_meme_categories():
    cats = meme_catalog.get_categories()
    order = meme_catalog.get_category_display_order()
    result = []
    for cat_id in order:
        info = cats.get(cat_id, {})
        count = len(meme_catalog.get_images_in_category(cat_id))
        result.append({
            "id": cat_id,
            "name": info.get("name", cat_id),
            "description": info.get("description", ""),
            "count": count,
        })
    return result


@router.get("/api/memes/category/{category_id}")
async def get_meme_category_images(category_id: str):
    images = meme_catalog.get_images_in_category(category_id)
    return {
        "category": category_id,
        "images": images,
    }


@router.get("/api/memes/image/{category_id}/{file_stem}")
async def get_meme_image(category_id: str, file_stem: str):
    path = meme_catalog.get_image_path(category_id, file_stem)
    if path and os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="Image not found")


@router.delete("/api/memes/image/{category_id}/{file_stem}")
async def delete_meme_image(category_id: str, file_stem: str):
    success = meme_catalog.delete_image(category_id, file_stem)
    if success:
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Image not found")


@router.get("/api/memes/render")
async def render_meme(stem: str):
    """通过 file_stem 查找并返回表情包图片"""
    from ..memes.renderer import MemeRenderer
    renderer = MemeRenderer(meme_catalog)
    path = renderer.render_meme(stem)
    if path and os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="Meme not found")


@router.get("/api/memes/filtered")
async def get_filtered_memes():
    import json
    path = meme_catalog.filtered_path
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"count": len(data), "items": data}
    return {"count": 0, "items": []}


@router.post("/api/memes/steal/analyze")
async def analyze_meme_for_steal(req: MemeStealAnalyzeRequest):
    """分析图片是否适合偷表情并生成 category/save_name；不保存文件。"""
    if not llm_client.api_key:
        raise HTTPException(status_code=400, detail="LLM API key is not configured")
    try:
        result = await meme_steal_analyzer.analyze(
            image_ref=req.image_ref,
            llm_client=llm_client,
            context_text=req.context_text,
        )
        return result.model_dump(mode="json")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"meme steal analysis failed: {e}")


@router.get("/api/config/hot-duration")
async def get_hot_duration():
    init_gate()
    return {
        "hot_duration_minutes": runtime_manager.hot_duration_minutes if runtime_manager else event_gate.hot_duration_minutes,
    }


@router.post("/api/config/hot-duration")
async def update_hot_duration(config: HotDurationConfig):
    init_gate()
    if runtime_manager:
        runtime_manager.update_hot_duration(config.hot_duration_minutes)
    elif event_gate:
        event_gate.update_hot_duration(config.hot_duration_minutes)
    save_settings({"hot_duration_minutes": config.hot_duration_minutes})
    return {
        "status": "ok",
        "hot_duration_minutes": runtime_manager.hot_duration_minutes if runtime_manager else event_gate.hot_duration_minutes,
    }


@router.get("/api/context-checkpoint/config")
async def get_context_checkpoint_config():
    return normalize_context_checkpoint_config(load_settings().get("context_checkpoint"))


@router.post("/api/context-checkpoint/config")
async def update_context_checkpoint_config(config: ContextCheckpointConfig):
    normalized = normalize_context_checkpoint_config(config.model_dump())
    save_settings({"context_checkpoint": normalized})
    return {"status": "ok", "config": normalized}


@router.get("/api/group-chat/config")
async def get_group_chat_config(
    group_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
):
    config = _current_group_send_config()
    resolved_group_id = _resolve_group_id_for_config(group_id=group_id, session_id=session_id)
    sid = f"qq_group_{resolved_group_id}" if resolved_group_id else ""
    return {
        "status": "ok",
        "config": config,
        "group_id": resolved_group_id,
        "session_id": sid or None,
        "known_group_ids": _known_group_config_ids(config),
        "group_policy": group_send_policy_for_session(config, sid) if sid else None,
    }


@router.post("/api/group-chat/config")
async def update_group_chat_config(config_update: GroupChatOpsConfig):
    current = _current_group_send_config()
    payload = config_update.model_dump(exclude_unset=True)
    if "enabled" in payload and payload["enabled"] is not None:
        current["enabled"] = bool(payload["enabled"])

    group_id = _normalize_group_id_value(payload.get("group_id"))
    policy_keys = {
        "observe_only",
        "allow_mention_reply",
        "allow_roll_reply",
        "allow_repetition",
        "allow_meme_send",
        "allow_command_reply",
    }
    policy_updates = {
        key: bool(payload[key])
        for key in policy_keys
        if key in payload and payload[key] is not None
    }
    if group_id and policy_updates:
        existing_policy = current.get("groups", {}).get(group_id) or current.get("default_group_policy") or {}
        current.setdefault("groups", {})[group_id] = normalize_group_policy({
            **existing_policy,
            **policy_updates,
        })
        allowed = set(current.get("allowed_group_ids") or [])
        allowed.add(group_id)
        current["allowed_group_ids"] = sorted(allowed)

    normalized = _save_group_send_config(current)
    sid = f"qq_group_{group_id}" if group_id else ""
    return {
        "status": "ok",
        "config": normalized,
        "group_id": group_id or None,
        "session_id": sid or None,
        "known_group_ids": _known_group_config_ids(normalized),
        "group_policy": group_send_policy_for_session(normalized, sid) if sid else None,
    }


def _resolve_group_id_for_config(
    *,
    group_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    direct = _normalize_group_id_value(group_id)
    if direct:
        return direct
    sid = normalize_session_id(session_id or "")
    from_session = group_id_from_session_id(sid)
    if from_session:
        return from_session
    known = _known_group_config_ids(_current_group_send_config())
    return known[0] if known else ""


def _normalize_group_id_value(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("qq_group_"):
        return group_id_from_session_id(text)
    return normalize_session_id(text, "unknown")


def _known_group_config_ids(config: Optional[dict] = None) -> list[str]:
    cfg = normalize_group_send_config(config)
    ids = set(cfg.get("allowed_group_ids") or [])
    ids.update((cfg.get("groups") or {}).keys())
    for sid in _group_buffer_session_ids():
        group_id = group_id_from_session_id(sid)
        if group_id:
            ids.add(group_id)
    return sorted(item for item in ids if item)


@router.get("/api/sessions")
async def get_sessions():
    return {"current": DEFAULT_SESSION_ID, "sessions": _list_sessions()}


@router.post("/api/sessions/select")
async def select_session(payload: SessionSelectRequest):
    sid = webui_session_id(payload.session_id)
    _runtime_for_session(sid)
    save_settings({"webui_session_id": sid})
    return {"status": "ok", "session_id": sid}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    sid = normalize_session_id(session_id)
    if sid == DEFAULT_SESSION_ID:
        raise HTTPException(status_code=400, detail="webui_default cannot be fully deleted; use clear instead")

    if runtime_manager:
        await runtime_manager.clear_session(sid)
        runtime_manager.drop_session_runtime(sid)
    if media_job_queue:
        media_job_queue.clear(sid)
    _clear_media_followup_keys(sid)
    await group_chat_buffer.clear(sid)
    group_image_cache.clear(sid)
    group_send_limiter.clear(sid)
    group_repetition_detector.clear(sid)
    group_activity_tracker.clear(sid)
    if group_reply_scheduler:
        await group_reply_scheduler.clear(sid)

    db = next(get_db())
    try:
        db.query(ConversationEvent).filter(ConversationEvent.session_id == sid).delete()
        db.query(RawChatLog).filter(RawChatLog.session_id == sid).delete()
        db.query(ContextCheckpoint).filter(ContextCheckpoint.session_id == sid).delete()
        db.commit()
    finally:
        db.close()

    session_registry.delete_private_files(sid)
    await _emit_conversation_changed("session_deleted", session_id=sid)
    return {"status": "ok", "session_id": sid}


@router.get("/api/status")
async def get_status(session_id: Optional[str] = Query(None)):
    sid = _session_id_from_query(session_id)
    if _is_group_session_id(sid):
        group_panel = group_chat_buffer.status(sid)
        qid_to_nickname = group_memory_manager.qid_to_nickname(sid)
        model_window = group_chat_buffer.get_model_window(sid, qid_to_nickname=qid_to_nickname, limit=50)
        latest = group_panel.get("latest") or {}
        buffered_events = group_panel.get("buffered_events", 0)
        group_runtime = group_chat_runtime_status()
        group_scheduler = _ensure_group_reply_scheduler().status(sid)
        group_image_status = group_image_cache.status(sid)
        group_send_status = _refresh_group_send_limiter().status(sid)
        group_memory_status = group_memory_manager.status(sid)
        group_repetition_status = group_repetition_detector.status(sid)
        group_activity_status = group_activity_tracker.status(sid)
        return {
            "session_id": sid,
            "status": "GROUP_CHAT",
            "buffer_version": group_panel.get("buffer_version", 0),
            "msg_index_today": buffered_events,
            "hot_until": None,
            "hot_duration_minutes": None,
            "last_action": None,
            "last_text": latest.get("text"),
            "last_snapshot_result": "group_chat_buffered",
            "gate": {
                "pending_job_id": None,
                "buffered_events": buffered_events,
                "stale_jobs_count": 0,
                "sent_jobs_count": 0,
                "user_composing": {"active": False},
            },
            "snapshot": {
                "session_id": sid,
                "snapshot_id": None,
                "buffer_version": group_panel.get("buffer_version", 0),
                "status": "GROUP_CHAT",
                "buffered_events": buffered_events,
                "memory_sources": ["group_buffer"],
                "context_note": "群聊窗口，不使用私聊 HOT/COLD",
            },
            "llm": {
                "last_llm_raw": None,
                "parse_status": None,
                "decision_result": None,
                "last_send": None,
                "group_runtime": group_runtime,
            },
            "media": [],
            "media_jobs": media_job_queue.status(sid) if media_job_queue else {"enabled": False, "session_id": sid},
            "meme": None,
            "onebot": onebot_manager.status(),
            "group_runtime": group_runtime,
            "group_scheduler": group_scheduler,
            "group_send": group_send_status,
            "group_memory": group_memory_status,
            "group_repetition": group_repetition_status,
            "group_activity": group_activity_status,
            "group_image_cache": group_image_status,
            "group_buffer": {**group_panel, "model_window": model_window},
        }
    runtime = _runtime_for_session(sid)
    gate = runtime.gate
    graph = runtime.graph
    # 检查是否需要从 HOT 退回 COLD
    await gate.maybe_exit_hot()
    last_decision = gate.get_last_decision()
    last_result = gate.get_last_snapshot_result()
    last_snapshot = gate.snapshot_manager.get_last_snapshot()
    last_ctx = gate._last_process_context

    # 计算热聊剩余时间（分钟）
    hot_remaining = None
    if gate.state.hot_until:
        hot_remaining = max(0, int((gate.state.hot_until - datetime.now()).total_seconds() / 60))

    # 从 LLM 主图获取最近一次解析状态
    parsed = graph.get_last_parsed_decision() if graph else {}
    media_jobs_panel = media_job_queue.status(sid) if media_job_queue else {"enabled": False, "session_id": sid}

    # 组装 Snapshot 面板数据
    snapshot_panel = {
        "session_id": sid,
        "snapshot_id": gate.state.current_snapshot_id,
        "buffer_version": gate.state.buffer_version,
        "status": gate.state.status.value,
        "buffered_events": len(gate.buffer._events),
        "memory_sources": ["SOUL", "MEMORY_CORE", "dm", "TOMORROW_TOPICS"],
    }
    if gate.state.status.value == "COLD":
        if last_snapshot and last_snapshot.cold_start_meta:
            snapshot_panel["cold_start_meta"] = {
                "timestamp": last_snapshot.cold_start_meta.timestamp,
                "last_user_message_age": last_snapshot.cold_start_meta.last_user_message_age,
                "msg_index": last_snapshot.cold_start_meta.msg_index,
                "status": last_snapshot.cold_start_meta.status.value,
            }
        else:
            snapshot_panel["context_note"] = "等待第一条消息，尚未生成 cold_start_meta"
    else:
        snapshot_panel["hot_until"] = gate.state.hot_until.isoformat() if gate.state.hot_until else None
        snapshot_panel["hot_remaining"] = hot_remaining
        snapshot_panel["context_note"] = "热聊状态，不传递 cold_start_meta"

    # LLM 决策结果
    llm_panel = {
        "last_llm_raw": graph.get_llm_raw_output_history() if graph else None,
        "parse_status": parsed.get("parse_status"),
        "decision_result": last_result,
        "last_send": gate.get_last_send_meta(),
    }
    media_panel = graph.get_last_media_debug() if graph else []

    # 事件门实时状态
    gate_panel = {
        "pending_job_id": gate._pending_job_id,
        "buffered_events": len(gate.buffer._events),
        "stale_jobs_count": len(gate._stale_job_ids),
        "sent_jobs_count": len(gate._sent_job_ids),
        "user_composing": gate.get_user_composing_meta(),
    }

    # Meme / 命令链路
    meme_panel = None
    if last_ctx and graph and (last_ctx.meme_search_used or graph.get_last_meme_search()):
        meme_panel = {
            "search_meme": graph.get_last_meme_search(),
            "candidates": last_ctx.meme_candidates,
            "selected_meme": ", ".join(last_ctx.selected_memes) if last_ctx.selected_memes else last_ctx.selected_meme,
            "render_status": graph.get_last_render_status(),
            "last_command": gate.get_last_command(),
            "command_result": gate.get_last_command_result(),
        }
    elif gate.get_last_command():
        meme_panel = {
            "search_meme": None,
            "candidates": [],
            "selected_meme": None,
            "render_status": None,
            "last_command": gate.get_last_command(),
            "command_result": gate.get_last_command_result(),
        }

    return {
        # 兼容旧字段
        "session_id": sid,
        "status": gate.state.status.value,
        "buffer_version": gate.state.buffer_version,
        "msg_index_today": gate.state.msg_index_today,
        "hot_until": gate.state.hot_until.isoformat() if gate.state.hot_until else None,
        "hot_duration_minutes": gate.hot_duration_minutes,
        "last_action": last_decision.action.value if last_decision else None,
        "last_text": last_decision.text if last_decision else None,
        "last_snapshot_result": last_result,
        "gate": gate_panel,
        # 新 MVP 调试面板字段
        "snapshot": snapshot_panel,
        "llm": llm_panel,
        "media": media_panel,
        "media_jobs": media_jobs_panel,
        "meme": meme_panel,
        "onebot": onebot_manager.status(),
    }


@router.get("/api/conversation")
async def get_conversation(session_id: Optional[str] = Query(None)):
    sid = _session_id_from_query(session_id)
    db = next(get_db())
    try:
        events = (
            db.query(ConversationEvent)
            .filter(ConversationEvent.session_id == sid)
            .filter(ConversationEvent.is_visible == True)  # noqa: E712
            .order_by(ConversationEvent.created_at)
            .all()
        )
        return [
            {
                "id": e.id,
                "session_id": e.session_id,
                "event_type": e.event_type,
                "text": e.text,
                "action": e.action,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ]
    finally:
        db.close()


@router.post("/api/conversation/clear")
async def clear_conversation(
    session_id: Optional[str] = Query(None),
    all_sessions: bool = Query(False),
):
    """清空对话记录和状态"""
    sid = _session_id_from_query(session_id)
    if all_sessions:
        if runtime_manager:
            for active_sid in runtime_manager.active_session_ids():
                await runtime_manager.clear_session(active_sid)
        if media_job_queue:
            media_job_queue.clear()
        _clear_media_followup_keys()
        await group_chat_buffer.clear()
        group_image_cache.clear()
        group_send_limiter.clear()
        group_repetition_detector.clear()
        group_activity_tracker.clear()
        if group_reply_scheduler:
            await group_reply_scheduler.clear()
    else:
        if _is_group_session_id(sid):
            await group_chat_buffer.clear(sid)
            group_image_cache.clear(sid)
            group_send_limiter.clear(sid)
            group_repetition_detector.clear(sid)
            group_activity_tracker.clear(sid)
            if group_reply_scheduler:
                await group_reply_scheduler.clear(sid)
        else:
            _runtime_for_session(sid)
            if runtime_manager:
                await runtime_manager.clear_session(sid)
        _clear_media_followup_keys(sid)

    db = next(get_db())
    try:
        if all_sessions:
            db.query(ConversationEvent).delete()
            db.query(RawChatLog).delete()
            db.query(ContextCheckpoint).delete()
        else:
            db.query(ConversationEvent).filter(ConversationEvent.session_id == sid).delete()
            db.query(RawChatLog).filter(RawChatLog.session_id == sid).delete()
            db.query(ContextCheckpoint).filter(ContextCheckpoint.session_id == sid).delete()
        db.commit()
    finally:
        db.close()

    await _emit_conversation_changed("conversation_cleared", session_id=sid)
    return {"status": "ok", "session_id": None if all_sessions else sid, "all_sessions": all_sessions}


@router.get("/api/raw-logs")
async def get_raw_logs(
    session_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    sid = _session_id_from_query(session_id)
    db = next(get_db())
    try:
        rows = (
            db.query(RawChatLog)
            .filter(RawChatLog.session_id == sid)
            .order_by(desc(RawChatLog.id))
            .limit(limit)
            .all()
        )
        return [
            {
                "id": row.id,
                "event_id": row.event_id,
                "session_id": row.session_id,
                "event_type": row.event_type,
                "platform": row.platform,
                "user_id": row.user_id,
                "input_text": row.input_text,
                "raw_payload": row.raw_payload,
                "llm_raw_output": row.llm_raw_output,
                "parsed_payload": row.parsed_payload,
                "action": row.action,
                "final_text": row.final_text,
                "item_type": row.item_type,
                "snapshot_id": row.snapshot_id,
                "buffer_version": row.buffer_version,
                "job_id": row.job_id,
                "status": row.status,
                "error_message": row.error_message,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    finally:
        db.close()
