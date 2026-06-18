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
            raw_output = await self.llm.chat_completion(messages=messages, temperature=0.2, max_tokens=1600)
            data = self._parse_json_object(raw_output)

            today_memory = data.get("today_memory_md")
            tomorrow_topics = data.get("tomorrow_topics_md")
            if not today_memory or not tomorrow_topics:
                raise RuntimeError("memory analysis response missing required markdown fields")

            if not self.memory.write_today_memory(today_memory):
                raise RuntimeError("failed to write today's dm file")
            if not self.memory.write_tomorrow_topics(tomorrow_topics):
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
            raw_output = await self.llm.chat_completion(messages=messages, temperature=0.2, max_tokens=2000)
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
                .filter(ConversationEvent.event_type.in_(["user_text", "assistant_text", "assistant_react"]))
                .order_by(ConversationEvent.created_at)
                .all()
            )
            transcript = []
            for row in rows:
                role = "assistant" if row.event_type.startswith("assistant") else "user"
                transcript.append(
                    {
                        "role": role,
                        "text": row.text,
                        "action": row.action,
                        "created_at": row.created_at.isoformat() if row.created_at else "",
                    }
                )
            return transcript
        finally:
            db.close()

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
