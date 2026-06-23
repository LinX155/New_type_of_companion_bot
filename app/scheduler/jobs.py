import json
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..llm.client import LLMClient
from ..llm.prompts import build_memory_analysis_messages, build_midnight_cleanup_messages
from ..storage.models import ScheduledJobLog, ConversationEvent
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


class SchedulerManager:
    def __init__(self, memory_manager: MemoryFileManager = None, llm_client: Optional[LLMClient] = None):
        self.scheduler = AsyncIOScheduler()
        self.memory = memory_manager or MemoryFileManager()
        self.llm = llm_client
        self.llm_client_factory: Optional[Callable[[str], LLMClient]] = None
        self.active_message_callback: Optional[Callable[[], Awaitable[dict]]] = None
        self.session_ids_provider: Optional[Callable[[], list[str]]] = None
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
        }

    def start(self):
        if self.scheduler.running:
            return
        for job_id, config in self._job_configs.items():
            self.scheduler.add_job(
                config["func"],
                trigger=CronTrigger(hour=config["hour"], minute=config["minute"]),
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
                    trigger=CronTrigger(hour=hour, minute=minute),
                )
            return True
        return False

    def get_schedules(self) -> dict:
        return {
            k: {"hour": v["hour"], "minute": v["minute"]}
            for k, v in self._job_configs.items()
        }

    def set_active_message_callback(self, callback: Callable[[], Awaitable[dict]]):
        self.active_message_callback = callback

    def set_session_ids_provider(self, callback: Callable[[], list[str]]):
        self.session_ids_provider = callback

    def set_llm_client_factory(self, callback: Callable[[str], LLMClient]):
        self.llm_client_factory = callback

    def _session_ids(self) -> list[str]:
        if not self.session_ids_provider:
            return ["default"]
        session_ids = self.session_ids_provider() or []
        return sorted({sid for sid in session_ids if sid}) or ["default"]

    def _llm_for_session(self, session_id: str) -> Optional[LLMClient]:
        if self.llm_client_factory:
            return self.llm_client_factory(session_id)
        return self.llm

    async def _run_memory_analysis(self):
        base_job_id = f"memory_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        failures = []
        for session_id in self._session_ids():
            ok, error = await self._run_memory_analysis_for_session(base_job_id, session_id)
            if not ok:
                failures.append({"session_id": session_id, "error": error})
        return {"status": "completed" if not failures else "partial_failed", "failures": failures}

    async def _run_memory_analysis_for_session(self, base_job_id: str, session_id: str):
        job_id = f"{base_job_id}_{session_id}"
        log_id = self._start_job(job_id, "memory_analysis", session_id=session_id)
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

            if not memory.write_today_memory(today_memory):
                raise RuntimeError("failed to write today's dm file")
            scoped_tomorrow_topics = self._merge_tomorrow_topics_sections(
                current=memory.read_tomorrow_topics(),
                proposed=tomorrow_topics,
                owned_sections=MEMORY_ANALYSIS_TOMORROW_SECTIONS,
            )
            if not memory.write_tomorrow_topics(scoped_tomorrow_topics):
                raise RuntimeError("failed to write TOMORROW_TOPICS.md")

            self._finish_job(log_id, "completed")
            return True, None
        except Exception as e:
            self._finish_job(log_id, "failed", str(e))
            return False, str(e)

    async def _run_midnight_cleanup(self):
        base_job_id = f"midnight_cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        failures = []
        for session_id in self._session_ids():
            ok, error = await self._run_midnight_cleanup_for_session(base_job_id, session_id)
            if not ok:
                failures.append({"session_id": session_id, "error": error})
        return {"status": "completed" if not failures else "partial_failed", "failures": failures}

    async def _run_midnight_cleanup_for_session(self, base_job_id: str, session_id: str):
        job_id = f"{base_job_id}_{session_id}"
        log_id = self._start_job(job_id, "midnight_cleanup", session_id=session_id)
        try:
            llm = self._llm_for_session(session_id)
            if llm is None:
                raise RuntimeError("LLM client is not configured for midnight cleanup")

            memory = self.memory.for_session(session_id)
            target_day = datetime.now() - timedelta(days=1)
            date_str = target_day.strftime("%Y-%m-%d")
            day_memory = memory.read_dm_file(date_str)
            if not day_memory:
                self._finish_job(log_id, "completed")
                return True, None

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

            self._finish_job(log_id, "completed")
            return True, None
        except Exception as e:
            self._finish_job(log_id, "failed", str(e))
            return False, str(e)

    async def _run_active_message(self):
        job_id = f"active_message_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        log_id = self._start_job(job_id, "active_message")
        try:
            if self.active_message_callback is None:
                raise RuntimeError("active message callback is not configured")
            result = await self.active_message_callback()
            note = json.dumps(result, ensure_ascii=False)[:1000]
            status = "failed" if result.get("status") == "error" else "completed"
            self._finish_job(log_id, status, note)
        except Exception as e:
            self._finish_job(log_id, "failed", str(e))

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
