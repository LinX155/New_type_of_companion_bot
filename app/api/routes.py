import os
import json
import re
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..core.event_gate import EventGate, ProcessContext
from ..core.events import ChatEvent, EventType
from ..core.decisions import Action
from ..core.graph import CompanionGraph
from ..core.state import ChatStatus
from ..core.settings import load_settings, save_settings
from ..llm.client import LLMClient
from ..memory.files import MemoryFileManager
from ..memes.catalog import MemeCatalog
from ..scheduler.jobs import SchedulerManager
from ..storage.db import get_db, engine
from ..storage.models import Base, RawChatLog, ConversationEvent

Base.metadata.create_all(bind=engine)

router = APIRouter()

# 全局实例（MVP 阶段简化）
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

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
scheduler_manager = SchedulerManager(memory_manager, llm_client)

companion_graph: Optional[CompanionGraph] = None
event_gate: Optional[EventGate] = None

# Store for WebUI callbacks
message_callbacks = []


class ApiConfig(BaseModel):
    api_key: str
    base_url: str
    model: str
    thinking_enabled: bool = False


class ChatMessage(BaseModel):
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


async def on_decision(ctx: ProcessContext):
    """LLM 决策回调"""
    global companion_graph, event_gate

    if event_gate.is_job_stale(ctx.job_id):
        event_gate.mark_job_stale_dropped(ctx.job_id)
        await event_gate.dispatch_latest_after_stale(ctx.job_id)
        await _emit_state({"job_id": ctx.job_id, "result": "stale_dropped"})
        return

    try:
        await _emit_llm_started()
        decision = await companion_graph.run(ctx)
    except Exception as e:
        event_gate.mark_job_dropped(ctx.job_id)
        _record_llm_error(ctx, str(e))
        await _emit_state({
            "job_id": ctx.job_id,
            "snapshot_id": ctx.snapshot.snapshot_id,
            "result": "error",
        })
        return

    if not await event_gate.is_snapshot_current(ctx):
        event_gate.mark_job_stale_dropped(ctx.job_id)
        await event_gate.dispatch_latest_after_stale(ctx.job_id)
        await _emit_state({"job_id": ctx.job_id, "result": "stale_dropped"})
        return

    event_gate.record_decision(decision)

    # 路由决策
    from ..core.router import ActionRouter
    router_inst = ActionRouter()
    result = router_inst.route(decision)

    if result["visible"]:
        cleared = await event_gate.clear_buffer_after_visible_send(ctx.snapshot.buffer_version)
        if not cleared:
            event_gate.mark_job_stale_dropped(ctx.job_id)
            await event_gate.dispatch_latest_after_stale(ctx.job_id)
            await _emit_state({"job_id": ctx.job_id, "result": "stale_dropped"})
            return

    if decision.action == Action.ENTER_CHAT:
        await event_gate._enter_hot()
    elif decision.action == Action.END_CHAT:
        await event_gate._exit_hot()
    elif result["visible"] and ctx.snapshot.status == ChatStatus.COLD:
        await event_gate._enter_hot()

    # 记录到数据库
    db = next(get_db())
    try:
        log = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id="default",
            event_type="llm_decision",
            llm_raw_output=json.dumps({"action": decision.action.value, "text": decision.text}),
            action=decision.action.value,
            final_text=decision.text,
            snapshot_id=ctx.snapshot.snapshot_id,
            buffer_version=ctx.snapshot.buffer_version,
            job_id=ctx.job_id,
            status="sent" if result["visible"] else "dropped",
        )
        db.add(log)

        if result["visible"] and result["text"]:
            event_type = "assistant_react" if decision.action == Action.REACT else "assistant_text"
            conv = ConversationEvent(
                session_id="default",
                event_type=event_type,
                text=result["text"],
                is_visible=True,
                action=decision.action.value,
            )
            db.add(conv)

        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()

    if result["visible"]:
        companion_graph.commit_turn(ctx, decision, visible=True)
        event_gate.mark_job_sent(ctx.job_id)
    else:
        event_gate.mark_job_dropped(ctx.job_id)

    # 触发回调
    if result["visible"]:
        await _emit_message({
                "type": "assistant_message",
                "action": decision.action.value,
                "text": result["text"],
                "visible": result["visible"],
                "is_meme": result.get("is_meme", False),
                "meme_path": ctx.selected_meme,
                "snapshot_id": ctx.snapshot.snapshot_id,
        })
    else:
        await _emit_state({
            "job_id": ctx.job_id,
            "snapshot_id": ctx.snapshot.snapshot_id,
            "action": decision.action.value,
            "result": "dropped",
        })


async def _emit_message(data: dict):
    for cb in message_callbacks:
        try:
            await cb(data)
        except Exception:
            pass


async def _emit_state(data: dict):
    await _emit_message({"type": "assistant_state", **data})


async def _emit_llm_started():
    """LLM 真正开始思考/生成时通知前端显示“对方正在输入”。"""
    await _emit_state({"result": "llm_started"})


def init_gate():
    global event_gate, companion_graph
    if event_gate is None:
        event_gate = EventGate(
            on_decision=on_decision,
            hot_duration_minutes=_persisted.get("hot_duration_minutes", 30),
        )
        companion_graph = CompanionGraph(
            llm_client=llm_client,
            memory_manager=memory_manager,
            meme_catalog=meme_catalog,
        )
        # 启动时把持久化的调度时间应用到调度器
        for job_id in ("memory_analysis_day", "memory_analysis_night", "midnight_cleanup"):
            cfg = _persisted.get(job_id)
            if isinstance(cfg, dict):
                scheduler_manager.update_schedule(job_id, cfg.get("hour"), cfg.get("minute"))
        scheduler_manager.start()


@router.post("/api/config")
async def update_config(config: ApiConfig):
    llm_client.update_config(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        thinking_enabled=config.thinking_enabled,
    )
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
    )

    # 记录用户消息
    db = next(get_db())
    try:
        conv = ConversationEvent(
            session_id="default",
            event_type="user_command" if event_type in (EventType.COMMAND_MEM, EventType.COMMAND_FORGET) else "user_text",
            text=text,
            is_visible=True,
            action=event_type.value if event_type in (EventType.COMMAND_MEM, EventType.COMMAND_FORGET) else None,
        )
        db.add(conv)
        raw = RawChatLog(
            event_id=event.event_id,
            session_id="default",
            event_type=event_type.value,
            platform=event.platform,
            user_id=event.user_id,
            input_text=text,
            status="received",
        )
        db.add(raw)
        db.commit()
    except Exception as e:
        print(f"DB error: {e}")
    finally:
        db.close()

    result = await event_gate.handle_event(event)

    if event_type == EventType.COMMAND_MEM:
        await _emit_llm_started()
        success, response_text = await memory_manager.apply_mem_via_llm(text, llm_client)
        event_gate.record_command("/mem", "success" if success else "error")
        await _record_command_response("command.mem", response_text, success)
        await _emit_message({
            "type": "assistant_message",
            "action": "COMMAND_MEM",
            "text": response_text,
            "visible": True,
            "is_meme": False,
            "meme_path": None,
        })
        return {**result, "memory_updated": success, "response": response_text}

    if event_type == EventType.COMMAND_FORGET:
        await _emit_llm_started()
        success, response_text = await memory_manager.apply_forget_via_llm(text, llm_client)
        event_gate.record_command("/forget", "success" if success else "error")
        await _record_command_response("command.forget", response_text, success)
        await _emit_message({
            "type": "assistant_message",
            "action": "COMMAND_FORGET",
            "text": response_text,
            "visible": True,
            "is_meme": False,
            "meme_path": None,
        })
        return {**result, "memory_updated": success, "response": response_text}

    return result


def _event_type_for_text(text: str) -> EventType:
    lowered = text.lower()
    if lowered.startswith("/mem"):
        return EventType.COMMAND_MEM
    if lowered.startswith("/forget"):
        return EventType.COMMAND_FORGET
    return EventType.TEXT


async def _record_command_response(action: str, response_text: str, success: bool):
    db = next(get_db())
    try:
        conv = ConversationEvent(
            session_id="default",
            event_type="assistant_system",
            text=response_text,
            is_visible=True,
            action=action,
        )
        db.add(conv)
        raw = RawChatLog(
            event_id=f"evt_{uuid.uuid4().hex[:8]}",
            session_id="default",
            event_type=action,
            action=action,
            final_text=response_text,
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
            session_id="default",
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


@router.post("/api/nudge")
async def send_nudge():
    init_gate()
    event = ChatEvent(
        event_id=f"evt_{uuid.uuid4().hex[:8]}",
        event_type=EventType.NUDGE,
        timestamp=datetime.now(),
    )
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

    # 组装 Snapshot 面板数据
    snapshot_panel = {
        "snapshot_id": event_gate.state.current_snapshot_id,
        "buffer_version": event_gate.state.buffer_version,
        "status": event_gate.state.status.value,
        "buffered_events": len(event_gate.buffer._events),
        "memory_sources": ["SOUL", "MEMORY_CORE", "dm"],
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
        "last_llm_raw": companion_graph.get_last_llm_raw_output() if companion_graph else None,
        "parse_status": parsed.get("parse_status"),
        "decision_result": last_result,
    }

    # 事件门实时状态
    gate_panel = {
        "pending_job_id": event_gate._pending_job_id,
        "buffered_events": len(event_gate.buffer._events),
        "stale_jobs_count": len(event_gate._stale_job_ids),
        "sent_jobs_count": len(event_gate._sent_job_ids),
    }

    # Meme / 命令链路
    meme_panel = None
    if last_ctx and (last_ctx.meme_search_used or companion_graph.get_last_meme_search()):
        meme_panel = {
            "search_meme": companion_graph.get_last_meme_search(),
            "candidates": last_ctx.meme_candidates,
            "selected_meme": last_ctx.selected_meme,
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
        "meme": meme_panel,
    }


@router.get("/api/conversation")
async def get_conversation():
    db = next(get_db())
    try:
        events = db.query(ConversationEvent).order_by(ConversationEvent.created_at).all()
        return [
            {
                "id": e.id,
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
    global event_gate, companion_graph
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
    if companion_graph:
        companion_graph._conversation_history.clear()

    db = next(get_db())
    try:
        db.query(ConversationEvent).delete()
        db.query(RawChatLog).delete()
        db.commit()
    finally:
        db.close()

    return {"status": "ok"}
