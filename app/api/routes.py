import os
import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import desc

from ..adapters.onebot11 import OneBotConnectionManager, OneBotMediaDownloader, parse_onebot_event
from ..active.messages import (
    count_candidates_by_status,
    expire_stale_candidates,
    mark_candidate_status,
    parse_active_decision,
    select_pending_candidate,
)
from ..core.event_gate import EventGate, ProcessContext
from ..core.events import ChatEvent, EventType
from ..core.decisions import Action, ActionDecision, SendItem, SendItemType
from ..core.graph import CompanionGraph
from ..core.media_jobs import MediaJob, MediaJobQueue
from ..core.repetition_guard import (
    RECENT_REPETITION_WINDOW,
    RepetitionRemoval,
    build_repetition_guard_system_reminder,
    filter_recent_repeated_reactions,
)
from ..core.state import ChatStatus
from ..core.settings import load_settings, save_settings
from ..llm.client import LLMClient
from ..llm.prompts import build_active_message_messages
from ..memory.files import MemoryFileManager
from ..memes.catalog import MemeCatalog
from ..memes.steal import MemeStealAnalyzer, MemeStealSaver
from ..scheduler.jobs import SchedulerManager
from ..storage.db import get_db, engine
from ..storage.models import Base, RawChatLog, ConversationEvent, ensure_storage_schema

Base.metadata.create_all(bind=engine)
ensure_storage_schema(engine)

router = APIRouter()

# 全局实例（MVP 阶段简化）
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_SESSION_ID = "default"


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
    }
    path = os.path.join(ROOT_DIR, "api_key.txt")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            api_key_match = re.search(r"api_key\s*=\s*[\"']?([A-Za-z0-9_\-]+)", content)
            base_url_match = re.search(r"base_url\s*=\s*[\"']([^\"']+)", content)
            model_match = re.search(r"model\s*=\s*[\"']([^\"']+)", content)
            if api_key_match:
                config["api_key"] = api_key_match.group(1)
            if base_url_match:
                config["base_url"] = base_url_match.group(1)
            if model_match:
                config["model"] = model_match.group(1)
        except Exception:
            pass

    # 持久化配置优先级最高：前端手动保存过的值覆盖上面
    if _persisted.get("api_key"):
        config["api_key"] = _persisted["api_key"]
    if _persisted.get("base_url"):
        config["base_url"] = _persisted["base_url"]
    if _persisted.get("model"):
        config["model"] = _persisted["model"]
    config["thinking_enabled"] = _persisted.get("thinking_enabled", False)
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

# Store for WebUI callbacks
message_callbacks = []

ACTIVE_MESSAGE_DEFAULTS = {
    "enabled": False,
    "hour": 10,
    "minute": 0,
    "daily_limit": 1,
    "quiet_start_hour": 0,
    "quiet_end_hour": 9,
}


class ApiConfig(BaseModel):
    api_key: str
    base_url: str
    model: str
    thinking_enabled: bool = False


class ChatMessage(BaseModel):
    text: str


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
    quiet_start_hour: int = 0
    quiet_end_hour: int = 9


class InputStatusConfig(BaseModel):
    composing: bool = True
    ttl_ms: int = 3000


class MemeStealAnalyzeRequest(BaseModel):
    image_ref: str
    context_text: str = ""


def init_gate():
    global event_gate, companion_graph, media_job_queue
    if event_gate is None:
        media_job_queue = MediaJobQueue(
            llm_client=llm_client,
            media_downloader=onebot_media_downloader,
            meme_steal_analyzer=meme_steal_analyzer,
            meme_steal_saver=meme_steal_saver,
            on_payloads=_record_background_media_payloads,
            max_workers=1,
        )
        media_job_queue.start()
        event_gate = EventGate(
            on_decision=on_decision,
            hot_duration_minutes=_persisted.get("hot_duration_minutes", 30),
        )
        companion_graph = CompanionGraph(
            llm_client=llm_client,
            memory_manager=memory_manager,
            meme_catalog=meme_catalog,
            media_job_queue=media_job_queue,
        )
        # 启动时把持久化的调度时间应用到调度器
        for job_id in ("memory_analysis_day", "memory_analysis_night", "midnight_cleanup"):
            cfg = _persisted.get(job_id)
            if isinstance(cfg, dict):
                scheduler_manager.update_schedule(job_id, cfg.get("hour"), cfg.get("minute"))
        active_cfg = _normalize_active_message_config(_persisted.get("active_message"))
        scheduler_manager.update_schedule("active_message", active_cfg["hour"], active_cfg["minute"])
        scheduler_manager.set_active_message_callback(run_active_message_once)
        scheduler_manager.start()
    elif media_job_queue:
        media_job_queue.start()


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


async def on_decision(ctx: ProcessContext):
    """LLM 决策回调"""
    global companion_graph, event_gate
    target = _message_target_from_snapshot(ctx.snapshot)

    if event_gate.is_job_stale(ctx.job_id):
        event_gate.mark_job_stale_dropped(ctx.job_id)
        await event_gate.dispatch_latest_after_stale(ctx.job_id)
        _record_job_state(ctx, "stale_dropped", {"reason": "job_already_stale"})
        await _emit_state({"session_id": ctx.snapshot.session_id, "job_id": ctx.job_id, "result": "stale_dropped"})
        return

    try:
        await _emit_llm_started(target)
        decision = await companion_graph.run(ctx)
        _record_prompt_cache_debug(ctx, companion_graph.get_prompt_observability())
        media_debug = (
            companion_graph.get_last_media_debug()
            if hasattr(companion_graph, "get_last_media_debug")
            else None
        )
        _record_media_harness_debug(ctx, media_debug)
    except Exception as e:
        await _emit_llm_finished(target, "error")
        event_gate.mark_job_dropped(ctx.job_id)
        _record_llm_error(ctx, str(e))
        await _emit_state({
            "job_id": ctx.job_id,
            "snapshot_id": ctx.snapshot.snapshot_id,
            "result": "error",
        })
        return

    if not await event_gate.is_snapshot_current(ctx):
        await _emit_llm_finished(target, "stale_dropped")
        event_gate.mark_job_stale_dropped(ctx.job_id)
        await event_gate.dispatch_latest_after_stale(ctx.job_id)
        _record_job_state(ctx, "stale_dropped", {"reason": "snapshot_not_current"})
        await _emit_state({"session_id": ctx.snapshot.session_id, "job_id": ctx.job_id, "result": "stale_dropped"})
        return

    decision, repetition_removed = _apply_recent_repetition_guard(ctx, decision)
    if repetition_removed:
        _record_repetition_guard_filter(ctx, decision, repetition_removed)
        if companion_graph:
            companion_graph.append_internal_system_reminder(
                build_repetition_guard_system_reminder(repetition_removed)
            )

    event_gate.record_decision(decision)

    # 路由决策
    from ..core.router import ActionRouter
    router_inst = ActionRouter()
    result = router_inst.route(decision)
    items = list(result.get("items") or [])

    if result["visible"] and items:
        cleared, send_group_version = await event_gate.clear_buffer_after_visible_send(ctx.snapshot.buffer_version)
        if not cleared:
            await _emit_llm_finished(target, "stale_dropped")
            event_gate.mark_job_stale_dropped(ctx.job_id)
            await event_gate.dispatch_latest_after_stale(ctx.job_id)
            _record_job_state(ctx, "stale_dropped", {"reason": "buffer_version_changed_before_send"})
            await _emit_state({"session_id": ctx.snapshot.session_id, "job_id": ctx.job_id, "result": "stale_dropped"})
            return
    else:
        send_group_version = ctx.snapshot.buffer_version

    if decision.action == Action.ENTER_CHAT:
        await event_gate._enter_hot()
    elif decision.action == Action.END_CHAT:
        await event_gate._exit_hot()

    if not result["visible"] or not items:
        await _emit_llm_finished(target, "dropped")
        _record_decision_drop(ctx, decision)
        event_gate.mark_job_dropped(ctx.job_id)
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
        if not companion_graph or committed_sent_count >= len(sent_items):
            return
        pending_items = sent_items[committed_sent_count:]
        if committed_sent_count == 0:
            companion_graph.commit_sent_items(ctx, list(sent_items))
        else:
            companion_graph.commit_assistant_items(pending_items)
        committed_sent_count = len(sent_items)

    send_count = len(display_units)
    for send_index, unit in enumerate(display_units):
        original_index = unit["original_index"]
        original_item = unit["original_item"]
        item = unit["display_item"]
        if not await event_gate.is_send_group_current(ctx, send_group_version):
            await _emit_llm_finished(target, "stale_dropped")
            commit_sent_progress()
            event_gate.mark_job_stale_dropped(ctx.job_id)
            await event_gate.dispatch_latest_after_stale(ctx.job_id)
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

        send_key = event_gate.build_send_key(ctx, send_index)
        if event_gate.is_send_key_sent(send_key):
            continue

        content = item.content
        item_type = item.type.value
        per_send_result = {**result, "text": content, "texts": [content], "item": item}

        if _requires_user_composing_gate([item]):
            commit_sent_progress()
            if not await _wait_until_user_not_composing(ctx):
                await _emit_llm_finished(target, "stale_dropped")
                return

        event_gate.record_send_meta(ctx, send_index, send_count, item_type)
        if not await event_gate.is_send_group_current(ctx, send_group_version):
            await _emit_llm_finished(target, "stale_dropped")
            commit_sent_progress()
            event_gate.mark_job_stale_dropped(ctx.job_id)
            await event_gate.dispatch_latest_after_stale(ctx.job_id)
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
            **_message_target_from_snapshot(ctx.snapshot),
        })
        event_gate.mark_send_key_sent(send_key)
        _record_assistant_send(
            ctx=ctx,
            decision=decision,
            item=item,
            routed=per_send_result,
            send_index=send_index,
            send_count=send_count,
            send_key=send_key,
        )
        await _emit_conversation_changed("assistant_send")
        if unit["is_last_part"] and original_index not in sent_original_indices:
            sent_items.append(original_item)
            sent_original_indices.add(original_index)

    commit_sent_progress()
    event_gate.mark_job_sent(ctx.job_id)
    await _emit_llm_finished(target, "sent")


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


def _requires_user_composing_gate(items: list[SendItem]) -> bool:
    return any(item.type == SendItemType.TEXT for item in items)


def _build_display_send_units(items: list[SendItem]) -> list[dict]:
    units: list[dict] = []
    for original_index, item in enumerate(items):
        display_parts = (
            _split_text_for_display(item.content)
            if item.type == SendItemType.TEXT
            else [item.content]
        )
        for part_index, part in enumerate(display_parts):
            display_item = item
            if item.type == SendItemType.TEXT and part != item.content:
                display_item = SendItem(type=SendItemType.TEXT, content=part)
            units.append({
                "original_index": original_index,
                "original_item": item,
                "display_item": display_item,
                "is_last_part": part_index == len(display_parts) - 1,
            })
    return units


def _split_text_for_display(text: str) -> list[str]:
    if not text:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    chinese_count_after_comma = 0

    for char in text:
        current.append(char)
        if _is_chinese_char(char):
            chinese_count_after_comma += 1

        if char in ("，", ","):
            if chinese_count_after_comma > 5:
                parts.append("".join(current))
                current = []
            chinese_count_after_comma = 0

    if current:
        parts.append("".join(current))
    return parts or [text]


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


def _message_target_from_event(event: ChatEvent) -> dict:
    if event.platform != "qq":
        return {}
    return {
        "target_platform": "qq",
        "target_user_id": event.user_id,
    }


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
        _record_onebot_input_status(user_id, event_type, response, "sent", reason)
        return True
    except Exception as exc:
        _record_onebot_input_status(user_id, event_type, None, "error", reason, str(exc))
        return False


async def _emit_conversation_changed(reason: str, event: Optional[ChatEvent] = None):
    if event and event.event_type == EventType.USER_COMPOSING:
        return
    await _emit_message({
        "type": "conversation_changed",
        "session_id": DEFAULT_SESSION_ID,
        "reason": reason,
        "event_type": event.event_type.value if event else None,
        "platform": event.platform if event else None,
        "user_id": event.user_id if event else None,
    })


async def _send_onebot_assistant_message(data: dict) -> None:
    target_user_id = str(data.get("target_user_id") or "")
    if not target_user_id:
        return

    send_key = data.get("send_key") or f"onebot_command_{uuid.uuid4().hex[:8]}"
    item_type = str(data.get("item_type") or "text")
    content = str(data.get("content") or data.get("text") or "")
    try:
        if item_type == SendItemType.MEME.value or content.startswith("meme:"):
            stem = content[5:] if content.startswith("meme:") else content
            image_path = _meme_path_for_stem(stem)
            if not image_path:
                _record_onebot_send(send_key, target_user_id, item_type, content, None, "skipped:meme_not_found")
                return
            response = await onebot_manager.send_private_image(target_user_id, Path(image_path).resolve().as_uri())
        else:
            response = await onebot_manager.send_private_text(target_user_id, content)
        _record_onebot_send(send_key, target_user_id, item_type, content, response, "sent")
    except Exception as exc:
        _record_onebot_send(send_key, target_user_id, item_type, content, None, "error", str(exc))


def _meme_path_for_stem(stem: str) -> str:
    normalized = (stem or "").strip()
    if not normalized:
        return ""
    for category_id, stems in meme_catalog.get_all_images().items():
        if normalized in stems:
            return meme_catalog.get_image_path(category_id, normalized)
    return ""


def _record_onebot_send(
    send_key: str,
    target_user_id: str,
    item_type: str,
    content: str,
    response: Optional[dict],
    status: str,
    error_message: Optional[str] = None,
):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"{send_key}:onebot",
            session_id=DEFAULT_SESSION_ID,
            event_type="onebot_send",
            platform="qq",
            user_id=target_user_id,
            raw_payload=_json_dumps({
                "target_user_id": target_user_id,
                "item_type": item_type,
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
):
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"onebot_input_status_{uuid.uuid4().hex[:8]}",
            session_id=DEFAULT_SESSION_ID,
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


async def _wait_until_user_not_composing(ctx: ProcessContext) -> bool:
    gate = ctx.gate
    if not gate.is_user_composing():
        return True

    gate.mark_job_deferred(ctx.job_id)
    composing_meta = gate.get_user_composing_meta()
    await _emit_state({
        "session_id": ctx.snapshot.session_id,
        "job_id": ctx.job_id,
        "snapshot_id": ctx.snapshot.snapshot_id,
        "result": "deferred_user_composing",
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
        await asyncio.sleep(0.2)

    if gate.is_job_stale(ctx.job_id) or not await gate.is_snapshot_current(ctx):
        gate.mark_job_stale_dropped(ctx.job_id)
        await gate.dispatch_latest_after_stale(ctx.job_id)
        _record_job_state(ctx, "stale_dropped", {"reason": "stale_after_user_composing"})
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
    })
    return True

@router.post("/api/config")
async def update_config(config: ApiConfig):
    llm_client.update_config(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        thinking_enabled=config.thinking_enabled,
    )
    if companion_graph:
        companion_graph.reset_provider_transcript(reason="llm_config_changed")
    save_settings({
        "api_key": config.api_key,
        "base_url": config.base_url,
        "model": config.model,
        "thinking_enabled": config.thinking_enabled,
    })
    return {"status": "ok"}


@router.get("/api/config")
async def get_config():
    return {
        "api_key": llm_client.api_key,
        "base_url": llm_client.base_url,
        "model": llm_client.model,
        "thinking_enabled": llm_client.thinking_enabled,
    }


@router.post("/api/chat")
async def send_message(msg: ChatMessage):
    init_gate()
    text = msg.text.strip()
    event_type = _event_type_for_text(text)

    event = ChatEvent(
        event_id=f"evt_{uuid.uuid4().hex[:8]}",
        event_type=event_type,
        text=text,
        timestamp=datetime.now(),
        raw={"source": "webui"},
    )

    _record_incoming_event(event)
    await _emit_conversation_changed("incoming_event", event)

    result = await event_gate.handle_event(event)

    if event_type == EventType.COMMAND_MEM:
        return await _handle_command_event(event, result)

    if event_type == EventType.COMMAND_FORGET:
        return await _handle_command_event(event, result)

    return result


@router.post("/api/input-status")
async def update_input_status(config: InputStatusConfig):
    init_gate()
    event = ChatEvent(
        event_id=f"evt_{uuid.uuid4().hex[:8]}",
        event_type=EventType.USER_COMPOSING,
        timestamp=datetime.now(),
        raw={
            "composing": config.composing,
            "ttl_ms": config.ttl_ms,
            "source": "webui",
        },
    )
    result = await event_gate.handle_event(event)
    await _emit_state({
        "session_id": DEFAULT_SESSION_ID,
        "result": "user_composing",
        "composing": event_gate.get_user_composing_meta(),
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


async def handle_onebot_payload(payload: dict) -> None:
    event = parse_onebot_event(payload)
    if not event:
        return

    init_gate()
    if event.event_type == EventType.TEXT:
        event_type = _event_type_for_text(event.text or "")
        if event_type != event.event_type:
            event = event.model_copy(update={"event_type": event_type})

    _record_incoming_event(event)
    await _emit_conversation_changed("incoming_event", event)
    result = await event_gate.handle_event(event)

    if event.event_type in (EventType.COMMAND_MEM, EventType.COMMAND_FORGET):
        await _handle_command_event(event, result)
        return

    if event.event_type == EventType.USER_COMPOSING:
        await _emit_state({
            "session_id": DEFAULT_SESSION_ID,
            "result": "user_composing",
            "composing": event_gate.get_user_composing_meta(),
            "target_platform": "qq",
            "target_user_id": event.user_id,
        })


async def _handle_command_event(event: ChatEvent, result: Optional[dict] = None) -> dict:
    text = event.text or ""
    target = _message_target_from_event(event)
    if event.event_type == EventType.COMMAND_MEM:
        action = "COMMAND_MEM"
        action_key = "command.mem"
        command = "/mem"
        try:
            success, response_text = await _run_mem_command(text, target)
        except Exception:
            await _emit_llm_finished(target, "command_error")
            raise
    elif event.event_type == EventType.COMMAND_FORGET:
        action = "COMMAND_FORGET"
        action_key = "command.forget"
        command = "/forget"
        try:
            success, response_text = await _run_forget_command(text, target)
        except Exception:
            await _emit_llm_finished(target, "command_error")
            raise
    else:
        return result or {"handled": False, "type": "not_command"}

    event_gate.record_command(command, "success" if success else "error")
    await _record_command_response(DEFAULT_SESSION_ID, action_key, response_text, success)
    await _emit_llm_finished(target, "command_response")
    await _emit_message({
        "type": "assistant_message",
        "session_id": DEFAULT_SESSION_ID,
        "action": action,
        "text": response_text,
        "content": response_text,
        "item_type": "text",
        "visible": True,
        "is_meme": False,
        "meme_path": None,
        **_message_target_from_event(event),
    })
    await _emit_conversation_changed("assistant_command_response")
    return {**(result or {}), "memory_updated": success, "response": response_text}


async def _run_mem_command(text: str, target: Optional[dict] = None) -> tuple[bool, str]:
    await _emit_llm_started(target)
    return await memory_manager.apply_mem_via_llm(text, llm_client)


async def _run_forget_command(text: str, target: Optional[dict] = None) -> tuple[bool, str]:
    await _emit_llm_started(target)
    return await memory_manager.apply_forget_via_llm(text, llm_client)


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
        session_id = DEFAULT_SESSION_ID
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
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id=ctx.snapshot.session_id,
            event_type="llm_decision",
            llm_raw_output=companion_graph.get_last_llm_raw_output() if companion_graph else None,
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
    await _emit_state({
        "session_id": job.session_id,
        "result": "media_job_finished",
        "media_job_id": job.job_id,
        "media_key": job.media_key,
        "payloads": payloads,
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
        log = RawChatLog(
            event_id=send_key,
            session_id=ctx.snapshot.session_id,
            event_type="llm_decision",
            llm_raw_output=companion_graph.get_last_llm_raw_output() if companion_graph else None,
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


async def run_active_message_once(manual: bool = False) -> dict:
    """执行一次主动消息检查。

    MVP 只做一次轻量开场：有 pending 候选、处于 COLD、无未处理输入、未超每日上限时才发送。
    """
    global event_gate, companion_graph
    session_id = DEFAULT_SESSION_ID
    init_gate()
    config = _normalize_active_message_config(load_settings().get("active_message"))
    now = datetime.now()

    if not config["enabled"]:
        return {"status": "skipped", "reason": "disabled"}
    if not manual and _is_quiet_time(now, config):
        return {"status": "skipped", "reason": "quiet_hours"}
    if _active_messages_sent_today(session_id) >= config["daily_limit"]:
        return {"status": "skipped", "reason": "daily_limit"}
    if _has_unanswered_active_message(session_id):
        return {"status": "skipped", "reason": "unanswered_backoff"}

    job_id = await event_gate.reserve_active_message_job()
    if not job_id:
        return {"status": "skipped", "reason": "gate_busy"}

    llm_started = False
    try:
        topics = memory_manager.read_tomorrow_topics()
        topics, expired_count = expire_stale_candidates(topics)
        if expired_count and not memory_manager.write_tomorrow_topics(topics):
            await event_gate.finish_active_message_job(job_id, "dropped")
            return {"status": "error", "reason": "failed_to_expire_candidates"}

        candidate = select_pending_candidate(topics)
        if not candidate:
            await event_gate.finish_active_message_job(job_id, "dropped")
            return {"status": "skipped", "reason": "no_candidate"}

        if not llm_client.api_key:
            await event_gate.finish_active_message_job(job_id, "dropped")
            return {"status": "skipped", "reason": "llm_not_configured"}

        await _emit_llm_started()
        llm_started = True
        messages = build_active_message_messages(
            candidate_section=candidate.section,
            candidate_text=candidate.text,
            soul_md=memory_manager.read_soul(),
            memory_core_md=memory_manager.read_memory_core(),
            current_time=now.strftime("%H:%M"),
        )
        raw_output = await llm_client.chat_completion(messages=messages, temperature=0.3)
        decision = parse_active_decision(raw_output)

        if not await event_gate.is_active_message_job_current(job_id):
            event_gate.mark_job_stale_dropped(job_id)
            await event_gate.dispatch_latest_after_stale(job_id)
            return {"status": "skipped", "reason": "stale_dropped"}

        if decision.action == Action.WAIT:
            if not memory_manager.write_tomorrow_topics(
                mark_candidate_status(memory_manager.read_tomorrow_topics(), candidate, "blocked")
            ):
                await event_gate.finish_active_message_job(job_id, "dropped")
                return {"status": "error", "reason": "failed_to_block_candidate"}
            await event_gate.finish_active_message_job(job_id, "dropped")
            await _emit_state({"session_id": session_id, "job_id": job_id, "result": "active_wait"})
            return {"status": "skipped", "reason": "llm_wait"}

        from ..core.router import ActionRouter
        routed = ActionRouter().route(decision)
        if not routed["visible"] or not routed["text"]:
            await event_gate.finish_active_message_job(job_id, "dropped")
            await _emit_state({"session_id": session_id, "job_id": job_id, "result": "active_dropped"})
            return {"status": "skipped", "reason": "not_visible"}
        active_items = list(routed.get("items") or [])
        active_item = active_items[0] if active_items else None

        if not memory_manager.write_tomorrow_topics(
            mark_candidate_status(memory_manager.read_tomorrow_topics(), candidate, "used")
        ):
            await event_gate.finish_active_message_job(job_id, "dropped")
            return {"status": "error", "reason": "failed_to_mark_used"}

        _record_active_message(session_id, job_id, raw_output, decision, routed)
        event_gate.record_decision(decision)
        await event_gate.finish_active_message_job(job_id, "sent")

        if companion_graph and routed["text"]:
            companion_graph.commit_external_assistant_text(routed["text"])

        await _emit_message({
            "type": "assistant_message",
            "session_id": session_id,
            "action": decision.action.value,
            "text": routed["text"],
            "content": routed["text"],
            "item_type": active_item.type.value if active_item else "text",
            "visible": True,
            "is_meme": routed.get("is_meme", False),
            "meme_path": active_item.content[5:] if active_item and active_item.type == SendItemType.MEME else None,
            "source": "active_message",
        })
        return {
            "status": "sent",
            "job_id": job_id,
            "action": decision.action.value,
            "candidate_section": candidate.section,
        }
    except Exception as e:
        await event_gate.finish_active_message_job(job_id, "dropped")
        return {"status": "error", "reason": str(e)}
    finally:
        if llm_started:
            await _emit_llm_finished(reason="active_message_finished")


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
        "quiet_start_hour": _clamp_int(
            merged.get("quiet_start_hour"), 0, 23, ACTIVE_MESSAGE_DEFAULTS["quiet_start_hour"]
        ),
        "quiet_end_hour": _clamp_int(
            merged.get("quiet_end_hour"), 0, 23, ACTIVE_MESSAGE_DEFAULTS["quiet_end_hour"]
        ),
    }


def _clamp_int(value, min_value: int, max_value: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _is_quiet_time(now: datetime, config: dict) -> bool:
    start = config["quiet_start_hour"]
    end = config["quiet_end_hour"]
    hour = now.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


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
async def send_nudge():
    init_gate()
    event = ChatEvent(
        event_id=f"evt_{uuid.uuid4().hex[:8]}",
        event_type=EventType.NUDGE,
        text="拍了拍你",
        timestamp=datetime.now(),
        raw={"source": "webui"},
    )
    _record_incoming_event(event)
    await _emit_conversation_changed("incoming_event", event)
    result = await event_gate.handle_event(event)
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


@router.post("/api/active-message/config")
async def update_active_message_config(config: ActiveMessageConfig):
    init_gate()
    normalized = _normalize_active_message_config(config.model_dump())
    scheduler_manager.update_schedule("active_message", normalized["hour"], normalized["minute"])
    save_settings({"active_message": normalized})
    return {"status": "ok", "config": normalized}


@router.get("/api/active-message/status")
async def get_active_message_status():
    init_gate()
    topics = memory_manager.read_tomorrow_topics()
    return {
        "config": _normalize_active_message_config(load_settings().get("active_message")),
        "sent_today": _active_messages_sent_today(DEFAULT_SESSION_ID),
        "has_unanswered_active_message": _has_unanswered_active_message(DEFAULT_SESSION_ID),
        "candidates": count_candidates_by_status(topics),
        "gate_ready": await event_gate.can_accept_active_message() if hasattr(event_gate, "can_accept_active_message") else None,
    }


@router.post("/api/active-message/run")
async def run_active_message_now():
    init_gate()
    return await run_active_message_once(manual=True)


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
        "hot_duration_minutes": event_gate.hot_duration_minutes,
    }


@router.post("/api/config/hot-duration")
async def update_hot_duration(config: HotDurationConfig):
    init_gate()
    event_gate.update_hot_duration(config.hot_duration_minutes)
    save_settings({"hot_duration_minutes": config.hot_duration_minutes})
    return {"status": "ok", "hot_duration_minutes": event_gate.hot_duration_minutes}


@router.get("/api/status")
async def get_status():
    init_gate()
    session_id = DEFAULT_SESSION_ID
    # 检查是否需要从 HOT 退回 COLD
    await event_gate.maybe_exit_hot()
    last_decision = event_gate.get_last_decision()
    last_result = event_gate.get_last_snapshot_result()
    last_snapshot = event_gate.snapshot_manager.get_last_snapshot()
    last_ctx = event_gate._last_process_context

    # 计算热聊剩余时间（分钟）
    hot_remaining = None
    if event_gate.state.hot_until:
        hot_remaining = max(0, int((event_gate.state.hot_until - datetime.now()).total_seconds() / 60))

    # 从 LLM 主图获取最近一次解析状态
    parsed = companion_graph.get_last_parsed_decision() if companion_graph else {}
    media_jobs_panel = media_job_queue.status() if media_job_queue else {"enabled": False}

    # 组装 Snapshot 面板数据
    snapshot_panel = {
        "session_id": session_id,
        "snapshot_id": event_gate.state.current_snapshot_id,
        "buffer_version": event_gate.state.buffer_version,
        "status": event_gate.state.status.value,
        "buffered_events": len(event_gate.buffer._events),
        "memory_sources": ["SOUL", "MEMORY_CORE", "dm", "TOMORROW_TOPICS"],
    }
    if event_gate.state.status.value == "COLD":
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
        snapshot_panel["hot_until"] = event_gate.state.hot_until.isoformat() if event_gate.state.hot_until else None
        snapshot_panel["hot_remaining"] = hot_remaining
        snapshot_panel["context_note"] = "热聊状态，不传递 cold_start_meta"

    # LLM 决策结果
    llm_panel = {
        "last_llm_raw": companion_graph.get_llm_raw_output_history() if companion_graph else None,
        "parse_status": parsed.get("parse_status"),
        "decision_result": last_result,
        "last_send": event_gate.get_last_send_meta(),
    }
    media_panel = companion_graph.get_last_media_debug() if companion_graph else []

    # 事件门实时状态
    gate_panel = {
        "pending_job_id": event_gate._pending_job_id,
        "buffered_events": len(event_gate.buffer._events),
        "stale_jobs_count": len(event_gate._stale_job_ids),
        "sent_jobs_count": len(event_gate._sent_job_ids),
        "user_composing": event_gate.get_user_composing_meta(),
    }

    # Meme / 命令链路
    meme_panel = None
    if last_ctx and (last_ctx.meme_search_used or companion_graph.get_last_meme_search()):
        meme_panel = {
            "search_meme": companion_graph.get_last_meme_search(),
            "candidates": last_ctx.meme_candidates,
            "selected_meme": ", ".join(last_ctx.selected_memes) if last_ctx.selected_memes else last_ctx.selected_meme,
            "render_status": companion_graph.get_last_render_status(),
            "last_command": event_gate.get_last_command(),
            "command_result": event_gate.get_last_command_result(),
        }
    elif event_gate.get_last_command():
        meme_panel = {
            "search_meme": None,
            "candidates": [],
            "selected_meme": None,
            "render_status": None,
            "last_command": event_gate.get_last_command(),
            "command_result": event_gate.get_last_command_result(),
        }

    return {
        # 兼容旧字段
        "session_id": session_id,
        "status": event_gate.state.status.value,
        "buffer_version": event_gate.state.buffer_version,
        "msg_index_today": event_gate.state.msg_index_today,
        "hot_until": event_gate.state.hot_until.isoformat() if event_gate.state.hot_until else None,
        "hot_duration_minutes": event_gate.hot_duration_minutes,
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
async def get_conversation():
    db = next(get_db())
    try:
        events = (
            db.query(ConversationEvent)
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
async def clear_conversation():
    """清空对话记录和状态"""
    global event_gate, companion_graph, media_job_queue
    if event_gate:
        new_version = await event_gate.buffer.clear()
        event_gate.state.status = ChatStatus.COLD
        event_gate.state.buffer_version = new_version
        event_gate.state.msg_index_today = 0
        event_gate.state.hot_until = None
        event_gate.state.pending_job_id = None
        event_gate._pending_job_id = None
        event_gate._stale_job_ids.clear()
        event_gate._sent_job_ids.clear()
        event_gate._sent_send_keys.clear()
        event_gate._last_send_meta = None
        event_gate.clear_user_composing()
    if companion_graph:
        companion_graph.clear_prompt_state()
    if media_job_queue:
        media_job_queue.clear()

    db = next(get_db())
    try:
        db.query(ConversationEvent).delete()
        db.query(RawChatLog).delete()
        db.commit()
    finally:
        db.close()

    await _emit_conversation_changed("conversation_cleared")
    return {"status": "ok"}
