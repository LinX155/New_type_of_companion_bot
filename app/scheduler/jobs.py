import json
import re
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from ..active.settings import ACTIVE_SETTING_NONE, normalize_active_message_setting
from ..llm.client import LLMClient
from ..core.settings import load_settings
from ..llm.prompts import (
    build_context_checkpoint_messages,
    build_group_context_checkpoint_messages,
    build_group_memory_analysis_messages,
    build_group_midnight_cleanup_messages,
    build_memory_analysis_messages,
    build_midnight_cleanup_messages,
)
from ..core.protocol import (
    contains_internal_visible_protocol,
    contains_malformed_visible_meme_marker,
    contains_visible_meme_marker,
)
from ..storage.context_checkpoints import (
    latest_context_checkpoint_for_session,
    latest_group_visible_event_id,
    latest_prompt_debug_for_session,
    latest_visible_event_id,
    load_conversation_context,
    load_group_visible_events_for_checkpoint,
    load_visible_events_for_checkpoint,
    normalize_context_checkpoint_config,
)
from ..storage.models import ContextCheckpoint, ConversationEvent, RawChatLog, ScheduledJobLog
from ..storage.db import SessionLocal
from ..memory.files import MemoryFileManager

TOMORROW_TOPIC_SECTIONS = ("未闭合话题", "昨日记忆", "生活感消息备选")
MEMORY_ANALYSIS_TOMORROW_SECTIONS = {"未闭合话题"}
MEMORY_ANALYSIS_EVENT_ROLES = {
    "user_text": "user",
    "user_image": "user",
    "user_sticker": "user",
    "nudge": "user",
    "assistant_text": "assistant",
    "assistant_react": "assistant",
}
GROUP_MEMORY_EVENT_ROLES = {
    "message.text": "user",
    "message.image": "user",
    "message.sticker": "user",
    "command.mem": "user",
    "command.forget": "user",
    "onebot_group_send": "assistant",
    "group_repetition": "assistant",
}
GROUP_CHECKPOINT_FORBIDDEN_OUTPUT_TOKENS = (
    "sender_card",
    "sender_nickname",
    "平台群名片",
    "平台昵称",
    "群名片",
    "QQ昵称",
    "qq昵称",
    "message_id",
    "onebot_message_id",
    "reply_to_message_id",
    "media_key",
    "media_job_id",
    "file_unique",
    "file_id",
    "local_path",
    "file_path",
    "http://",
    "https://",
)
ACTIVE_DAILY_JOB_PREFIX = "active_daily:"
ACTIVE_NEXT_JOB_PREFIX = "active_next:"
ACTIVE_MESSAGE_TOPIC_TIME_RE = re.compile(
    r"([01]?\d|2[0-3])\s*[:：]\s*[0-5]?\d|"
    r"([01]?\d|2[0-3])\s*点|"
    r"[零〇一二两三四五六七八九十]{1,3}\s*点|"
    r"(早上|上午|中午|下午|晚上|今晚|明早|明晚|凌晨|明天|今天)"
)


class SchedulerManager:
    def __init__(self, memory_manager: MemoryFileManager = None, llm_client: Optional[LLMClient] = None):
        self.scheduler = AsyncIOScheduler()
        self.memory = memory_manager or MemoryFileManager()
        self.llm = llm_client
        self.llm_client_factory: Optional[Callable[[str], LLMClient]] = None
        self.active_message_callback: Optional[Callable[[Optional[str], str, Optional[str]], Awaitable[dict]]] = None
        self.active_message_setting_callback: Optional[Callable[[str, dict], tuple[bool, str]]] = None
        self.group_activity_callback: Optional[Callable[[], Awaitable[dict]]] = None
        self.session_ids_provider: Optional[Callable[[], list[str]]] = None
        self.group_session_ids_provider: Optional[Callable[[], list[str]]] = None
        self.group_memory = None
        self.context_checkpoint_callback: Optional[Callable[[str, str, list[dict]], None]] = None
        self._job_configs = {
            "memory_analysis_day": {
                "hour": 13,
                "minute": 0,
                "func": self._run_memory_analysis,
            },
            "memory_analysis_night": {
                "hour": 19,
                "minute": 0,
                "func": self._run_memory_analysis,
            },
            "midnight_cleanup": {
                "hour": 3,
                "minute": 30,
                "func": self._run_midnight_cleanup,
            },
            "active_message": {
                "hour": 10,
                "minute": 0,
                "func": self._run_active_message,
            },
            "group_activity_update": {
                "hour": 6,
                "minute": 0,
                "func": self._run_group_activity_update,
            },
        }

    def start(self):
        if self.scheduler.running:
            return
        for job_id, config in self._job_configs.items():
            self.scheduler.add_job(
                config["func"],
                trigger=self._trigger_for_job(job_id, config),
                id=job_id,
                replace_existing=True,
            )
        self.scheduler.start()

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def update_schedule(self, job_id: str, hour: int, minute: int):
        if job_id in self._job_configs:
            self._job_configs[job_id]["hour"] = hour
            self._job_configs[job_id]["minute"] = minute
            if self.scheduler.running:
                self.scheduler.reschedule_job(
                    job_id,
                    trigger=self._trigger_for_job(job_id, self._job_configs[job_id]),
                )
            return True
        return False

    def _trigger_for_job(self, job_id: str, config: dict):
        return CronTrigger(hour=config["hour"], minute=config["minute"])

    def get_schedules(self) -> dict:
        return {
            k: {"hour": v["hour"], "minute": v["minute"]}
            for k, v in self._job_configs.items()
        }

    def set_active_message_callback(self, callback: Callable[[Optional[str], str, Optional[str]], Awaitable[dict]]):
        self.active_message_callback = callback

    def set_active_message_setting_callback(self, callback: Callable[[str, dict], tuple[bool, str]]):
        self.active_message_setting_callback = callback

    def set_group_activity_callback(self, callback: Callable[[], Awaitable[dict]]):
        self.group_activity_callback = callback

    def refresh_active_message_jobs(self, config: dict) -> tuple[dict, bool]:
        """Rebuild per-session active message jobs from persisted config."""
        normalized = dict(config or {})
        sessions = dict(normalized.get("sessions") or {})
        cleaned_sessions = {}
        changed = False

        self._remove_active_message_session_jobs()

        now_minute = datetime.now().replace(second=0, microsecond=0)
        for session_id, raw_session_cfg in sessions.items():
            session_cfg = dict(raw_session_cfg or {})
            cleaned_cfg = {}

            daily_time = self._normalize_hhmm(session_cfg.get("daily_time"))
            next_active_at = self._normalize_active_datetime(session_cfg.get("next_active_at"))
            next_run_at = None
            if next_active_at:
                parsed_next_run_at = datetime.strptime(next_active_at, "%Y-%m-%d %H:%M")
                if parsed_next_run_at >= now_minute:
                    next_run_at = parsed_next_run_at
                    cleaned_cfg["next_active_at"] = next_active_at
                else:
                    changed = True

            if daily_time:
                cleaned_cfg["daily_time"] = daily_time
                if normalized.get("enabled") and next_run_at is None:
                    self._schedule_active_daily_job(session_id, daily_time)

            if normalized.get("enabled") and next_run_at is not None:
                self._schedule_active_next_job(session_id, next_active_at, next_run_at)

            if cleaned_cfg:
                cleaned_sessions[session_id] = cleaned_cfg
            elif session_cfg:
                changed = True

        normalized["sessions"] = cleaned_sessions
        if cleaned_sessions != sessions:
            changed = True
        return normalized, changed

    def _remove_active_message_session_jobs(self):
        for job in list(self.scheduler.get_jobs()):
            if job.id.startswith(ACTIVE_DAILY_JOB_PREFIX) or job.id.startswith(ACTIVE_NEXT_JOB_PREFIX):
                try:
                    self.scheduler.remove_job(job.id)
                except JobLookupError:
                    pass

    def _schedule_active_daily_job(self, session_id: str, daily_time: str):
        hour, minute = self._parse_hhmm(daily_time)
        self.scheduler.add_job(
            self._run_active_message_for_session,
            trigger=CronTrigger(hour=hour, minute=minute),
            id=f"{ACTIVE_DAILY_JOB_PREFIX}{session_id}",
            args=[session_id, "daily", daily_time],
            replace_existing=True,
        )

    def _schedule_active_next_job(self, session_id: str, next_active_at: str, run_at: datetime):
        self.scheduler.add_job(
            self._run_active_message_for_session,
            trigger=DateTrigger(run_date=run_at),
            id=f"{ACTIVE_NEXT_JOB_PREFIX}{session_id}",
            args=[session_id, "next", next_active_at],
            replace_existing=True,
        )

    def _parse_hhmm(self, value: str) -> tuple[int, int]:
        hour, minute = value.split(":", 1)
        return int(hour), int(minute)

    def _normalize_hhmm(self, value) -> Optional[str]:
        if not isinstance(value, str):
            return None
        try:
            hour, minute = self._parse_hhmm(value.strip())
        except (ValueError, AttributeError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return f"{hour:02d}:{minute:02d}"

    def _normalize_active_datetime(self, value) -> Optional[str]:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        return parsed.strftime("%Y-%m-%d %H:%M")

    def set_session_ids_provider(self, callback: Callable[[], list[str]]):
        self.session_ids_provider = callback

    def set_group_session_ids_provider(self, callback: Callable[[], list[str]]):
        self.group_session_ids_provider = callback

    def set_group_memory_manager(self, manager):
        self.group_memory = manager

    def set_llm_client_factory(self, callback: Callable[[str], LLMClient]):
        self.llm_client_factory = callback

    def set_context_checkpoint_callback(self, callback: Callable[[str, str, list[dict]], None]):
        self.context_checkpoint_callback = callback

    def _session_ids(self) -> list[str]:
        if not self.session_ids_provider:
            return ["default"]
        session_ids = self.session_ids_provider() or []
        return sorted({sid for sid in session_ids if sid}) or ["default"]

    def _group_session_ids(self) -> list[str]:
        if not self.group_session_ids_provider:
            return []
        session_ids = self.group_session_ids_provider() or []
        return sorted({sid for sid in session_ids if sid and str(sid).startswith("qq_group_")})

    def _llm_for_session(self, session_id: str) -> Optional[LLMClient]:
        if self.llm_client_factory:
            return self.llm_client_factory(session_id)
        return self.llm

    def _llm_call_debug(self, llm: Optional[LLMClient]) -> Optional[dict]:
        if llm is None or not hasattr(llm, "get_last_call_debug"):
            return None
        debug = llm.get_last_call_debug()
        return debug if debug else None

    def _job_note(
        self,
        *,
        error: Optional[str] = None,
        llm: Optional[LLMClient] = None,
        llm_debug: Optional[dict] = None,
    ) -> Optional[str]:
        debug = llm_debug or self._llm_call_debug(llm)
        if not debug:
            return error
        payload = {"llm_call_debug": debug}
        if error:
            payload["error"] = error
        return json.dumps(payload, ensure_ascii=False)[:4000]

    async def _run_memory_analysis(self):
        base_job_id = f"memory_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        failures = []
        for session_id in self._session_ids():
            ok, error = await self._run_memory_analysis_for_session(base_job_id, session_id)
            if not ok:
                failures.append({"session_id": session_id, "error": error})
        for session_id in self._group_session_ids():
            ok, error = await self._run_group_memory_analysis_for_session(base_job_id, session_id)
            if not ok:
                failures.append({"session_id": session_id, "job_type": "group_memory_analysis", "error": error})
        return {"status": "completed" if not failures else "partial_failed", "failures": failures}

    async def _run_memory_analysis_for_session(self, base_job_id: str, session_id: str):
        job_id = f"{base_job_id}_{session_id}"
        log_id = self._start_job(job_id, "memory_analysis", session_id=session_id)
        llm = None
        try:
            llm = self._llm_for_session(session_id)
            if llm is None:
                raise RuntimeError("LLM client is not configured for memory analysis")

            memory = self.memory.for_session(session_id)
            date_str = datetime.now().strftime("%Y-%m-%d")
            transcript = self._load_transcript_for_date(datetime.now(), session_id)
            if not transcript:
                memory.read_today_memory()
                self._finish_job(log_id, "completed")
                return True, None

            messages = build_memory_analysis_messages(
                date_str=date_str,
                transcript=transcript,
                today_memory_md=memory.read_today_memory(),
                tomorrow_topics_md=memory.read_tomorrow_topics(),
            )
            raw_output = await llm.chat_completion(messages=messages, temperature=0.2)
            data = self._parse_json_object(raw_output)

            today_memory = data.get("today_memory_md")
            tomorrow_topics = data.get("tomorrow_topics_md")
            if not today_memory or not tomorrow_topics:
                raise RuntimeError("memory analysis response missing required markdown fields")
            active_message_setting = self._normalize_memory_active_message_setting(
                data.get("active_message_setting")
            )
            if active_message_setting["type"] != "none":
                today_memory = self._strip_active_message_setting_lines(today_memory)

            if not memory.write_today_memory(today_memory):
                raise RuntimeError("failed to write today's dm file")
            scoped_tomorrow_topics = self._merge_tomorrow_topics_sections(
                current=memory.read_tomorrow_topics(),
                proposed=tomorrow_topics,
                owned_sections=MEMORY_ANALYSIS_TOMORROW_SECTIONS,
            )
            if active_message_setting["type"] != "none":
                scoped_tomorrow_topics = self._strip_active_message_setting_lines(scoped_tomorrow_topics)
            if not memory.write_tomorrow_topics(scoped_tomorrow_topics):
                raise RuntimeError("failed to write TOMORROW_TOPICS.md")
            if active_message_setting["type"] != "none":
                self._apply_memory_active_message_setting(session_id, active_message_setting)

            self._finish_job(log_id, "completed", self._job_note(llm=llm))
            return True, None
        except Exception as e:
            self._finish_job(log_id, "failed", self._job_note(error=str(e), llm=llm))
            return False, str(e)

    async def _run_midnight_cleanup(self):
        base_job_id = f"midnight_cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        checkpoint_job_id = f"context_checkpoint_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        failures = []
        for session_id in self._session_ids():
            ok, error = await self._run_midnight_cleanup_for_session(base_job_id, session_id)
            if not ok:
                failures.append({"session_id": session_id, "error": error})
            checkpoint_ok, checkpoint_error = await self._run_context_checkpoint_for_session(checkpoint_job_id, session_id)
            if not checkpoint_ok:
                failures.append({"session_id": session_id, "job_type": "context_checkpoint", "error": checkpoint_error})
        for session_id in self._group_session_ids():
            ok, error = await self._run_group_midnight_cleanup_for_session(base_job_id, session_id)
            if not ok:
                failures.append({"session_id": session_id, "job_type": "group_midnight_cleanup", "error": error})
            checkpoint_ok, checkpoint_error = await self._run_group_context_checkpoint_for_session(checkpoint_job_id, session_id)
            if not checkpoint_ok:
                failures.append({"session_id": session_id, "job_type": "group_context_checkpoint", "error": checkpoint_error})
        return {"status": "completed" if not failures else "partial_failed", "failures": failures}

    async def _run_midnight_cleanup_for_session(self, base_job_id: str, session_id: str):
        job_id = f"{base_job_id}_{session_id}"
        log_id = self._start_job(job_id, "midnight_cleanup", session_id=session_id)
        llm = None
        try:
            llm = self._llm_for_session(session_id)
            if llm is None:
                raise RuntimeError("LLM client is not configured for midnight cleanup")

            memory = self.memory.for_session(session_id)
            target_day = datetime.now() - timedelta(days=1)
            date_str = target_day.strftime("%Y-%m-%d")
            day_memory = memory.read_dm_file(date_str)
            if day_memory:
                messages = build_midnight_cleanup_messages(
                    date_str=date_str,
                    memory_core_md=memory.read_memory_core(),
                    day_memory_md=day_memory,
                    tomorrow_topics_md=memory.read_tomorrow_topics(),
                )
                raw_output = await llm.chat_completion(messages=messages, temperature=0.2)
                data = self._parse_json_object(raw_output)

                memory_core = data.get("memory_core_md")
                tomorrow_topics = data.get("tomorrow_topics_md")
                if not memory_core or not tomorrow_topics:
                    raise RuntimeError("midnight cleanup response missing required markdown fields")

                if not memory.write_memory_core(memory_core):
                    raise RuntimeError("failed to write MEMORY_CORE.md")
                if not memory.write_tomorrow_topics(tomorrow_topics):
                    raise RuntimeError("failed to write TOMORROW_TOPICS.md")

            self._finish_job(log_id, "completed", self._job_note(llm=llm))
            return True, None
        except Exception as e:
            self._finish_job(log_id, "failed", self._job_note(error=str(e), llm=llm))
            return False, str(e)

    async def _run_group_memory_analysis_for_session(self, base_job_id: str, session_id: str):
        job_id = f"{base_job_id}_group_{session_id}"
        log_id = self._start_job(job_id, "group_memory_analysis", session_id=session_id)
        llm = None
        try:
            if self.group_memory is None:
                raise RuntimeError("group memory manager is not configured")
            llm = self._llm_for_session(session_id)
            if llm is None:
                raise RuntimeError("LLM client is not configured for group memory analysis")

            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            transcript = self._load_group_transcript_for_date(now, session_id)
            if not transcript:
                self.group_memory.read_dm_file(session_id, date_str)
                self._finish_job(log_id, "completed", json.dumps({"status": "skipped", "reason": "no_group_visible_events"}, ensure_ascii=False))
                return True, None

            result = await self.group_memory.analyze_daily_memory_via_llm(
                transcript,
                session_id=session_id,
                llm_client=llm,
                date_str=date_str,
                current_time=now,
            )
            llm_debug = self._llm_call_debug(llm)
            if llm_debug:
                result = {**result, "llm_call_debug": llm_debug}
            self._record_group_memory_audit(session_id, job_id, "group_memory_analysis", result)
            status = "failed" if result.get("status") in {"error", "write_failed"} else "completed"
            note = self._job_note(llm=llm) or json.dumps({k: v for k, v in result.items() if k != "raw_output"}, ensure_ascii=False)[:4000]
            self._finish_job(log_id, status, note)
            return status != "failed", result.get("reason")
        except Exception as e:
            self._finish_job(log_id, "failed", self._job_note(error=str(e), llm=llm))
            return False, str(e)

    async def _run_group_midnight_cleanup_for_session(self, base_job_id: str, session_id: str):
        job_id = f"{base_job_id}_group_{session_id}"
        log_id = self._start_job(job_id, "group_midnight_cleanup", session_id=session_id)
        llm = None
        try:
            if self.group_memory is None:
                raise RuntimeError("group memory manager is not configured")
            llm = self._llm_for_session(session_id)
            if llm is None:
                raise RuntimeError("LLM client is not configured for group midnight cleanup")

            target_day = datetime.now() - timedelta(days=1)
            date_str = target_day.strftime("%Y-%m-%d")
            result = await self.group_memory.cleanup_via_llm(
                session_id=session_id,
                llm_client=llm,
                date_str=date_str,
            )
            llm_debug = self._llm_call_debug(llm)
            if llm_debug:
                result = {**result, "llm_call_debug": llm_debug}
            self._record_group_memory_audit(session_id, job_id, "group_midnight_cleanup", result)
            status = "failed" if result.get("status") in {"error", "write_failed"} else "completed"
            note = self._job_note(llm=llm) or json.dumps({k: v for k, v in result.items() if k != "raw_output"}, ensure_ascii=False)[:4000]
            self._finish_job(log_id, status, note)
            return status != "failed", result.get("reason")
        except Exception as e:
            self._finish_job(log_id, "failed", self._job_note(error=str(e), llm=llm))
            return False, str(e)

    async def _run_context_checkpoint_for_session(self, base_job_id: str, session_id: str):
        job_id = f"{base_job_id}_{session_id}"
        log_id = self._start_job(job_id, "context_checkpoint", session_id=session_id)
        try:
            result = await self._maybe_create_context_checkpoint(job_id, session_id)
            self._record_context_checkpoint_audit(session_id, job_id, result, "completed")
            self._finish_job(log_id, "completed", self._job_note(llm_debug=result.get("llm_call_debug")))
            return True, None
        except Exception as e:
            llm_debug = getattr(e, "_llm_call_debug", None)
            result = {"status": "failed", "error": str(e)}
            if llm_debug:
                result["llm_call_debug"] = llm_debug
            self._record_context_checkpoint_audit(session_id, job_id, result, "failed")
            self._finish_job(log_id, "failed", self._job_note(error=str(e), llm_debug=llm_debug))
            return False, str(e)

    async def _maybe_create_context_checkpoint(self, job_id: str, session_id: str) -> dict:
        config = normalize_context_checkpoint_config(load_settings().get("context_checkpoint"))
        threshold_tokens = int(config["threshold_k"]) * 1000
        latest_debug = latest_prompt_debug_for_session(session_id)
        if not latest_debug:
            return {"status": "skipped", "reason": "no_prompt_debug", "threshold_tokens": threshold_tokens}

        current_tokens = int(latest_debug.get("tokens") or 0)
        if current_tokens < threshold_tokens:
            return {
                "status": "skipped",
                "reason": "below_threshold",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
            }

        latest_checkpoint = latest_context_checkpoint_for_session(session_id)
        if (
            latest_checkpoint
            and latest_checkpoint.get("source_prompt_debug_id")
            and latest_checkpoint["source_prompt_debug_id"] >= latest_debug["id"]
        ):
            return {
                "status": "skipped",
                "reason": "already_checkpointed_for_latest_debug",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
                "context_checkpoint_id": latest_checkpoint.get("id"),
            }

        through_event_id = latest_visible_event_id(session_id)
        if not through_event_id:
            return {
                "status": "skipped",
                "reason": "no_visible_events",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
            }

        after_event_id = latest_checkpoint.get("covered_until_event_id") if latest_checkpoint else None
        visible_events = load_visible_events_for_checkpoint(session_id, after_event_id, through_event_id)
        if not visible_events and latest_checkpoint:
            return {
                "status": "skipped",
                "reason": "no_new_visible_events_after_checkpoint",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
                "context_checkpoint_id": latest_checkpoint.get("id"),
            }
        if not visible_events:
            return {
                "status": "skipped",
                "reason": "no_visible_events_to_compress",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
            }

        llm = self._llm_for_session(session_id)
        if llm is None or not getattr(llm, "api_key", ""):
            raise RuntimeError("LLM client is not configured for context checkpoint")

        memory = self.memory.for_session(session_id)
        messages = build_context_checkpoint_messages(
            session_id=session_id,
            previous_checkpoint_text=(latest_checkpoint or {}).get("checkpoint_text") or "",
            visible_events=visible_events,
            memory_core_md=memory.read_memory_core(),
            today_memory_md=memory.read_dm_file(datetime.now().strftime("%Y-%m-%d")),
            tomorrow_topics_md=memory.read_tomorrow_topics(),
            estimated_tokens_before=current_tokens,
        )
        try:
            raw_output = await llm.chat_completion(messages=messages, temperature=0.2)
            data = self._parse_json_object(raw_output)
        except Exception as exc:
            try:
                setattr(exc, "_llm_call_debug", self._llm_call_debug(llm))
            except Exception:
                pass
            raise
        llm_debug = self._llm_call_debug(llm)
        checkpoint_text = (data.get("checkpoint_text") or "").strip()
        if not checkpoint_text:
            exc = RuntimeError("context checkpoint response missing checkpoint_text")
            try:
                setattr(exc, "_llm_call_debug", llm_debug)
            except Exception:
                pass
            raise exc

        checkpoint_id = self._write_context_checkpoint(
            session_id=session_id,
            checkpoint_text=checkpoint_text,
            covered_until_event_id=through_event_id,
            source_prompt_debug_id=latest_debug["id"],
            estimated_tokens_before=latest_debug.get("estimated_tokens"),
            prompt_tokens_before=latest_debug.get("prompt_tokens"),
        )
        context = load_conversation_context(session_id)
        if self.context_checkpoint_callback:
            self.context_checkpoint_callback(
                session_id,
                context.get("checkpoint_text") or checkpoint_text,
                context.get("history") or [],
            )
        result = {
            "status": "created",
            "context_checkpoint_id": checkpoint_id,
            "covered_until_event_id": through_event_id,
            "source_prompt_debug_id": latest_debug["id"],
            "tokens": current_tokens,
            "threshold_tokens": threshold_tokens,
            "compressed_event_count": len(visible_events),
        }
        if llm_debug:
            result["llm_call_debug"] = llm_debug
        return result

    async def _run_group_context_checkpoint_for_session(self, base_job_id: str, session_id: str):
        job_id = f"{base_job_id}_group_{session_id}"
        log_id = self._start_job(job_id, "group_context_checkpoint", session_id=session_id)
        try:
            result = await self._maybe_create_group_context_checkpoint(job_id, session_id)
            self._record_context_checkpoint_audit(session_id, job_id, result, "completed")
            self._finish_job(log_id, "completed", self._job_note(llm_debug=result.get("llm_call_debug")))
            return True, None
        except Exception as e:
            llm_debug = getattr(e, "_llm_call_debug", None)
            result = {
                "status": "failed",
                "scope": "group",
                "error": str(e),
            }
            if llm_debug:
                result["llm_call_debug"] = llm_debug
            self._record_context_checkpoint_audit(session_id, job_id, result, "failed")
            self._finish_job(log_id, "failed", self._job_note(error=str(e), llm_debug=llm_debug))
            return False, str(e)

    async def _maybe_create_group_context_checkpoint(self, job_id: str, session_id: str) -> dict:
        config = normalize_context_checkpoint_config(load_settings().get("context_checkpoint"))
        threshold_tokens = int(config["threshold_k"]) * 1000
        latest_debug = latest_prompt_debug_for_session(session_id)
        if not latest_debug:
            return {
                "status": "skipped",
                "scope": "group",
                "reason": "skipped_bounded_group_window",
                "detail": "no_group_prompt_debug",
                "threshold_tokens": threshold_tokens,
            }

        current_tokens = int(latest_debug.get("tokens") or 0)
        if current_tokens < threshold_tokens:
            return {
                "status": "skipped",
                "scope": "group",
                "reason": "below_threshold",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
            }

        latest_checkpoint = latest_context_checkpoint_for_session(session_id)
        if (
            latest_checkpoint
            and latest_checkpoint.get("source_prompt_debug_id")
            and latest_checkpoint["source_prompt_debug_id"] >= latest_debug["id"]
        ):
            return {
                "status": "skipped",
                "scope": "group",
                "reason": "already_checkpointed_for_latest_debug",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
                "context_checkpoint_id": latest_checkpoint.get("id"),
            }

        through_event_id = latest_group_visible_event_id(session_id)
        if not through_event_id:
            return {
                "status": "skipped",
                "scope": "group",
                "reason": "no_group_visible_events",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
            }

        after_event_id = latest_checkpoint.get("covered_until_event_id") if latest_checkpoint else None
        visible_events = load_group_visible_events_for_checkpoint(session_id, after_event_id, through_event_id)
        if not visible_events and latest_checkpoint:
            return {
                "status": "skipped",
                "scope": "group",
                "reason": "no_new_group_visible_events_after_checkpoint",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
                "context_checkpoint_id": latest_checkpoint.get("id"),
                "covered_until_raw_log_id": through_event_id,
            }
        if not visible_events:
            return {
                "status": "skipped",
                "scope": "group",
                "reason": "no_group_visible_events_to_compress",
                "tokens": current_tokens,
                "threshold_tokens": threshold_tokens,
                "source_prompt_debug_id": latest_debug.get("id"),
                "covered_until_raw_log_id": through_event_id,
            }

        if self.group_memory is None:
            raise RuntimeError("group memory manager is not configured for group context checkpoint")
        llm = self._llm_for_session(session_id)
        if llm is None or not getattr(llm, "api_key", ""):
            raise RuntimeError("LLM client is not configured for group context checkpoint")

        today = datetime.now().strftime("%Y-%m-%d")
        messages = build_group_context_checkpoint_messages(
            session_id=session_id,
            previous_checkpoint_text=(latest_checkpoint or {}).get("checkpoint_text") or "",
            group_visible_events=visible_events,
            current_group_memory_md=self.group_memory.read(session_id),
            today_group_memory_md=self.group_memory.read_dm_file(session_id, today),
            group_tomorrow_topics_md=self.group_memory.read_tomorrow_topics(session_id),
            estimated_tokens_before=current_tokens,
        )
        try:
            raw_output = await llm.chat_completion(messages=messages, temperature=0.2)
            data = self._parse_json_object(raw_output)
        except Exception as exc:
            try:
                setattr(exc, "_llm_call_debug", self._llm_call_debug(llm))
            except Exception:
                pass
            raise
        llm_debug = self._llm_call_debug(llm)
        checkpoint_text = (data.get("checkpoint_text") or "").strip()
        if not checkpoint_text:
            exc = RuntimeError("group context checkpoint response missing checkpoint_text")
            try:
                setattr(exc, "_llm_call_debug", llm_debug)
            except Exception:
                pass
            raise exc
        if self._is_unsafe_group_checkpoint_text(checkpoint_text):
            exc = RuntimeError("group context checkpoint response contains forbidden internal/platform content")
            try:
                setattr(exc, "_llm_call_debug", llm_debug)
            except Exception:
                pass
            raise exc

        checkpoint_id = self._write_context_checkpoint(
            session_id=session_id,
            checkpoint_text=checkpoint_text,
            covered_until_event_id=through_event_id,
            source_prompt_debug_id=latest_debug["id"],
            estimated_tokens_before=latest_debug.get("estimated_tokens"),
            prompt_tokens_before=latest_debug.get("prompt_tokens"),
        )
        result = {
            "status": "created",
            "scope": "group",
            "context_checkpoint_id": checkpoint_id,
            "covered_until_event_id": through_event_id,
            "covered_until_raw_log_id": through_event_id,
            "source_prompt_debug_id": latest_debug["id"],
            "tokens": current_tokens,
            "threshold_tokens": threshold_tokens,
            "compressed_event_count": len(visible_events),
        }
        if llm_debug:
            result["llm_call_debug"] = llm_debug
        return result

    def _is_unsafe_group_checkpoint_text(self, text: str) -> bool:
        value = str(text or "")
        lowered = value.lower()
        return (
            contains_internal_visible_protocol(value)
            or contains_malformed_visible_meme_marker(value)
            or contains_visible_meme_marker(value)
            or any(token.lower() in lowered for token in GROUP_CHECKPOINT_FORBIDDEN_OUTPUT_TOKENS)
        )

    def _write_context_checkpoint(
        self,
        *,
        session_id: str,
        checkpoint_text: str,
        covered_until_event_id: int,
        source_prompt_debug_id: int,
        estimated_tokens_before: Optional[int],
        prompt_tokens_before: Optional[int],
    ) -> int:
        db = SessionLocal()
        try:
            row = ContextCheckpoint(
                session_id=session_id,
                checkpoint_text=checkpoint_text,
                covered_until_event_id=covered_until_event_id,
                source_prompt_debug_id=source_prompt_debug_id,
                estimated_tokens_before=estimated_tokens_before,
                prompt_tokens_before=prompt_tokens_before,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row.id
        finally:
            db.close()

    def _record_context_checkpoint_audit(self, session_id: str, job_id: str, payload: dict, status: str):
        db = SessionLocal()
        try:
            db.add(RawChatLog(
                event_id=f"context_checkpoint_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
                session_id=session_id,
                event_type="context_checkpoint",
                platform="qq_group" if str(session_id).startswith("qq_group_") else "webui",
                raw_payload=json.dumps(payload or {}, ensure_ascii=False),
                job_id=job_id,
                action=str((payload or {}).get("scope") or "private"),
                status=status,
            ))
            db.commit()
        finally:
            db.close()

    def _record_group_memory_audit(self, session_id: str, job_id: str, event_type: str, payload: dict):
        db = SessionLocal()
        try:
            raw_output = str((payload or {}).get("raw_output") or "")
            safe_payload = {key: value for key, value in (payload or {}).items() if key != "raw_output"}
            db.add(RawChatLog(
                event_id=f"{event_type}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
                session_id=session_id,
                event_type=event_type,
                platform="qq_group",
                raw_payload=json.dumps(safe_payload, ensure_ascii=False),
                llm_raw_output=raw_output,
                parsed_payload=json.dumps(safe_payload, ensure_ascii=False),
                job_id=job_id,
                action=event_type,
                status=str((payload or {}).get("status") or "debug"),
                error_message=str((payload or {}).get("reason") or "")[:2000] or None,
            ))
            db.commit()
        finally:
            db.close()

    async def _run_active_message(self):
        cfg = self._job_configs["active_message"]
        expected_time = f"{cfg['hour']:02d}:{cfg['minute']:02d}"
        await self._run_active_message_job("global", None, expected_time)

    async def _run_active_message_for_session(self, session_id: str, source: str, expected_time: str):
        await self._run_active_message_job(source, session_id, expected_time)

    async def _run_active_message_job(self, source: str, session_id: Optional[str], expected_time: Optional[str]):
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_part = f"_{session_id}" if session_id else ""
        job_id = f"active_message_{source}{session_part}_{suffix}"
        log_id = self._start_job(job_id, "active_message", session_id=session_id or "default")
        try:
            if self.active_message_callback is None:
                raise RuntimeError("active message callback is not configured")
            result = await self.active_message_callback(session_id, source, expected_time)
            note = json.dumps(result, ensure_ascii=False)[:1000]
            status = "failed" if result.get("status") == "error" else "completed"
            self._finish_job(log_id, status, note)
        except Exception as e:
            self._finish_job(log_id, "failed", str(e))

    async def _run_group_activity_update(self):
        job_id = f"group_activity_update_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        log_id = self._start_job(job_id, "group_activity_update", session_id="qq_group")
        try:
            if self.group_activity_callback is None:
                result = {"status": "skipped", "reason": "callback_not_configured"}
            else:
                result = await self.group_activity_callback()
            note = json.dumps(result, ensure_ascii=False)[:1000]
            status = "failed" if result.get("status") == "error" else "completed"
            self._finish_job(log_id, status, note)
            return result
        except Exception as e:
            self._finish_job(log_id, "failed", str(e))
            return {"status": "error", "error_message": str(e)}

    def _load_transcript_for_date(self, day: datetime, session_id: str) -> list[dict]:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        db = SessionLocal()
        try:
            rows = (
                db.query(ConversationEvent)
                .filter(ConversationEvent.session_id == session_id)
                .filter(ConversationEvent.created_at >= start)
                .filter(ConversationEvent.created_at < end)
                .filter(ConversationEvent.is_visible.is_(True))
                .filter(ConversationEvent.event_type.in_(tuple(MEMORY_ANALYSIS_EVENT_ROLES.keys())))
                .order_by(ConversationEvent.created_at)
                .all()
            )
            transcript = []
            for row in rows:
                item = self._conversation_event_to_memory_transcript_item(row)
                if item:
                    transcript.append(item)
            return transcript
        finally:
            db.close()

    def _load_group_transcript_for_date(self, day: datetime, session_id: str) -> list[dict]:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        db = SessionLocal()
        try:
            rows = (
                db.query(RawChatLog)
                .filter(RawChatLog.session_id == session_id)
                .filter(RawChatLog.created_at >= start)
                .filter(RawChatLog.created_at < end)
                .filter(RawChatLog.event_type.in_(tuple(GROUP_MEMORY_EVENT_ROLES.keys())))
                .order_by(RawChatLog.created_at)
                .all()
            )
            transcript = []
            for row in rows:
                item = self._raw_group_log_to_memory_transcript_item(row)
                if item:
                    transcript.append(item)
            return transcript
        finally:
            db.close()

    def _raw_group_log_to_memory_transcript_item(self, row) -> Optional[dict]:
        role = GROUP_MEMORY_EVENT_ROLES.get(row.event_type)
        if not role:
            return None
        payload = self._safe_json_loads(row.raw_payload)
        text = (row.input_text or row.final_text or "").strip()
        if not text and isinstance(payload, dict):
            nested = payload.get("payload")
            if isinstance(nested, dict):
                text = str(nested.get("text") or nested.get("response") or "").strip()
        if not text:
            return None
        if self._contains_platform_identity(text):
            return None

        qid = ""
        if isinstance(payload, dict):
            qid = str(payload.get("sender_qid") or "").strip()
            raw = payload.get("raw")
            if not qid and isinstance(raw, dict):
                qid = str(raw.get("qq_user_id") or "").strip()
            if not qid and "qq_user_id" in payload:
                qid = str(payload.get("qq_user_id") or "").strip()

        item = {
            "role": role,
            "event_type": row.event_type,
            "text": text,
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "action": row.action,
        }
        if qid and re.fullmatch(r"\d{4,}", qid):
            item["qid"] = qid
        if row.item_type:
            item["item_type"] = row.item_type
        return item

    def _safe_json_loads(self, value) -> dict:
        if not value:
            return {}
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _contains_platform_identity(self, text: str) -> bool:
        return any(token in str(text or "") for token in (
            "sender_card",
            "sender_nickname",
            "群名片",
            "平台昵称",
            "QQ昵称",
            "qq昵称",
        ))

    def _conversation_event_to_memory_transcript_item(self, row) -> Optional[dict]:
        role = MEMORY_ANALYSIS_EVENT_ROLES.get(row.event_type)
        text = (row.text or "").strip()
        if not role or not text:
            return None
        return {
            "role": role,
            "text": text,
            "action": row.action,
            "event_type": row.event_type,
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }

    def _merge_tomorrow_topics_sections(
        self,
        current: str,
        proposed: str,
        owned_sections: set[str],
    ) -> str:
        """Keep non-owned TOMORROW_TOPICS.md sections unchanged.

        Memory analysis may only maintain "未闭合话题"; midnight cleanup owns
        "昨日记忆" and may also validate "未闭合话题".
        """
        current_sections = self._extract_tomorrow_sections(current)
        proposed_sections = self._extract_tomorrow_sections(proposed)
        lines = ["# 明日话题", ""]

        for section in TOMORROW_TOPIC_SECTIONS:
            lines.append(f"## {section}")
            body = (
                proposed_sections.get(section, [])
                if section in owned_sections
                else current_sections.get(section, [])
            )
            lines.extend(body)
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _extract_tomorrow_sections(self, markdown: str) -> dict[str, list[str]]:
        sections: dict[str, list[str]] = {section: [] for section in TOMORROW_TOPIC_SECTIONS}
        current_section: Optional[str] = None

        for raw in (markdown or "").splitlines():
            header = self._parse_tomorrow_section_header(raw)
            if header:
                current_section = header if header in sections else None
                continue
            if current_section:
                sections[current_section].append(raw)

        return {
            section: self._trim_blank_edges(lines)
            for section, lines in sections.items()
        }

    def _parse_tomorrow_section_header(self, line: str) -> Optional[str]:
        stripped = (line or "").strip()
        if stripped.startswith("## "):
            return stripped[3:].strip()
        if stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
            return stripped.strip("*").strip()
        return None

    def _trim_blank_edges(self, lines: list[str]) -> list[str]:
        start = 0
        end = len(lines)
        while start < end and not lines[start].strip():
            start += 1
        while end > start and not lines[end - 1].strip():
            end -= 1
        return lines[start:end]

    def _normalize_memory_active_message_setting(self, value) -> dict:
        if not isinstance(value, dict):
            return dict(ACTIVE_SETTING_NONE)
        return normalize_active_message_setting(value, now=datetime.now())

    def _apply_memory_active_message_setting(self, session_id: str, setting: dict):
        if self.active_message_setting_callback is None:
            return
        success, response = self.active_message_setting_callback(session_id, setting)
        if not success:
            raise RuntimeError(response or "failed to apply active message setting")

    def _strip_active_message_setting_lines(self, markdown: str) -> str:
        lines = []
        for line in (markdown or "").splitlines():
            if self._looks_like_active_message_setting_topic_line(line):
                continue
            lines.append(line)
        return "\n".join(lines).rstrip() + "\n"

    def _looks_like_active_message_setting_topic_line(self, line: str) -> bool:
        text = (line or "").strip()
        if not text.startswith("-"):
            return False
        if not ACTIVE_MESSAGE_TOPIC_TIME_RE.search(text):
            return False
        action_hints = (
            "主动消息",
            "主动找",
            "主动来",
            "发消息",
            "提醒",
            "叫",
            "喊",
            "来找",
            "联系",
        )
        request_hints = ("用户请求", "用户要求", "用户希望", "用户想让", "用户让")
        return any(hint in text for hint in action_hints) and any(hint in text for hint in request_hints)

    def _parse_json_object(self, raw_output: str) -> dict:
        text = (raw_output or "").strip()
        if not text:
            raise RuntimeError("empty LLM response")

        try:
            return json.loads(text)
        except Exception:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])

        raise RuntimeError("LLM response is not valid JSON")

    def _start_job(self, job_id: str, job_type: str, session_id: str = "default") -> int:
        db = SessionLocal()
        try:
            log = ScheduledJobLog(
                job_id=job_id,
                session_id=session_id,
                job_type=job_type,
                status="running",
                start_time=datetime.now(),
            )
            db.add(log)
            db.commit()
            db.refresh(log)
            return log.id
        finally:
            db.close()

    def _finish_job(self, log_id: int, status: str, error: str = None):
        db = SessionLocal()
        try:
            log = db.query(ScheduledJobLog).filter(ScheduledJobLog.id == log_id).first()
            if log:
                log.status = status
                log.end_time = datetime.now()
                log.error_message = error
                db.commit()
        finally:
            db.close()
