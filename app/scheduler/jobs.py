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


class SchedulerManager:
    def __init__(self, memory_manager: MemoryFileManager = None, llm_client: Optional[LLMClient] = None):
        self.scheduler = AsyncIOScheduler()
        self.memory = memory_manager or MemoryFileManager()
        self.llm = llm_client
        self.active_message_callback: Optional[Callable[[], Awaitable[dict]]] = None
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

    async def _run_memory_analysis(self):
        job_id = f"memory_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        log_id = self._start_job(job_id, "memory_analysis")
        try:
            if self.llm is None:
                raise RuntimeError("LLM client is not configured for memory analysis")

            date_str = datetime.now().strftime("%Y-%m-%d")
            transcript = self._load_transcript_for_date(datetime.now())
            if not transcript:
                self.memory.read_today_memory()
                self._finish_job(log_id, "completed")
                return

            messages = build_memory_analysis_messages(
                date_str=date_str,
                transcript=transcript,
                today_memory_md=self.memory.read_today_memory(),
                tomorrow_topics_md=self.memory.read_tomorrow_topics(),
            )
            raw_output = await self.llm.chat_completion(messages=messages, temperature=0.2)
            data = self._parse_json_object(raw_output)

            today_memory = data.get("today_memory_md")
            tomorrow_topics = data.get("tomorrow_topics_md")
            if not today_memory or not tomorrow_topics:
                raise RuntimeError("memory analysis response missing required markdown fields")

            if not self.memory.write_today_memory(today_memory):
                raise RuntimeError("failed to write today's dm file")
            scoped_tomorrow_topics = self._merge_tomorrow_topics_sections(
                current=self.memory.read_tomorrow_topics(),
                proposed=tomorrow_topics,
                owned_sections=MEMORY_ANALYSIS_TOMORROW_SECTIONS,
            )
            if not self.memory.write_tomorrow_topics(scoped_tomorrow_topics):
                raise RuntimeError("failed to write TOMORROW_TOPICS.md")

            self._finish_job(log_id, "completed")
        except Exception as e:
            self._finish_job(log_id, "failed", str(e))

    async def _run_midnight_cleanup(self):
        job_id = f"midnight_cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        log_id = self._start_job(job_id, "midnight_cleanup")
        try:
            if self.llm is None:
                raise RuntimeError("LLM client is not configured for midnight cleanup")

            target_day = datetime.now() - timedelta(days=1)
            date_str = target_day.strftime("%Y-%m-%d")
            day_memory = self.memory.read_dm_file(date_str)
            if not day_memory:
                self._finish_job(log_id, "completed")
                return

            messages = build_midnight_cleanup_messages(
                date_str=date_str,
                memory_core_md=self.memory.read_memory_core(),
                day_memory_md=day_memory,
                tomorrow_topics_md=self.memory.read_tomorrow_topics(),
            )
            raw_output = await self.llm.chat_completion(messages=messages, temperature=0.2)
            data = self._parse_json_object(raw_output)

            memory_core = data.get("memory_core_md")
            tomorrow_topics = data.get("tomorrow_topics_md")
            if not memory_core or not tomorrow_topics:
                raise RuntimeError("midnight cleanup response missing required markdown fields")

            if not self.memory.write_memory_core(memory_core):
                raise RuntimeError("failed to write MEMORY_CORE.md")
            if not self.memory.write_tomorrow_topics(tomorrow_topics):
                raise RuntimeError("failed to write TOMORROW_TOPICS.md")

            self._finish_job(log_id, "completed")
        except Exception as e:
            self._finish_job(log_id, "failed", str(e))

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

    def _load_transcript_for_date(self, day: datetime) -> list[dict]:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        db = SessionLocal()
        try:
            rows = (
                db.query(ConversationEvent)
                .filter(ConversationEvent.created_at >= start)
                .filter(ConversationEvent.created_at < end)
                .filter(ConversationEvent.event_type == "user_text")
                .order_by(ConversationEvent.created_at)
                .all()
            )
            transcript = []
            for row in rows:
                transcript.append(
                    {
                        "role": "user",
                        "text": row.text,
                        "action": row.action,
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                    }
                )
            return transcript
        finally:
            db.close()

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

    def _start_job(self, job_id: str, job_type: str) -> int:
        db = SessionLocal()
        try:
            log = ScheduledJobLog(
                job_id=job_id,
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
