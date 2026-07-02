import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from pydantic import BaseModel, Field

from ..llm.client import LLMClient
from ..llm.prompts import build_meme_reclassify_messages
from .catalog import MemeCatalog
from .steal import MemeStealAnalyzer, MemeStealSaver


class MemeReclassifyAnalysis(BaseModel):
    save_name: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    reason: str = ""
    safety: str = "unclear"
    raw_output: str = ""
    warnings: list[str] = Field(default_factory=list)


class MemeReclassifyResult(BaseModel):
    status: str = "failed"
    job_id: Optional[str] = None
    from_category: str
    target_category: str
    original_file_stem: str
    file_stem: Optional[str] = None
    old_file_path: Optional[str] = None
    file_path: Optional[str] = None
    hash_before: Optional[str] = None
    hash_after: Optional[str] = None
    reason: str = ""
    error: Optional[str] = None
    analysis: Optional[MemeReclassifyAnalysis] = None


@dataclass
class MemeReclassifyJob:
    job_id: str
    from_category: str
    file_stem: str
    target_category: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None

    def to_payload(self, queue_position: Optional[int] = None) -> dict:
        payload = {
            "job_id": self.job_id,
            "from_category": self.from_category,
            "file_stem": self.file_stem,
            "target_category": self.target_category,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
        }
        if queue_position is not None:
            payload["queue_position"] = queue_position
        return payload


class MemeReclassifier:
    def __init__(
        self,
        *,
        catalog: MemeCatalog,
        analyzer: MemeStealAnalyzer,
        saver: MemeStealSaver,
    ):
        self.catalog = catalog
        self.analyzer = analyzer
        self.saver = saver

    async def reclassify(
        self,
        *,
        from_category: str,
        file_stem: str,
        target_category: str,
        llm_client: LLMClient,
        job_id: Optional[str] = None,
    ) -> MemeReclassifyResult:
        self._validate_category(from_category, "from_category")
        self._validate_category(target_category, "target_category")
        source_path = self._source_path(from_category, file_stem)
        image_url = self.analyzer.to_model_image_url(source_path)
        categories_text = self.analyzer._categories_text()
        messages = build_meme_reclassify_messages(
            image_url=image_url,
            categories_text=categories_text,
            source_category=from_category,
            target_category=target_category,
            current_file_stem=file_stem,
        )
        raw_output = await llm_client.chat_completion(messages=messages, temperature=0.2)
        analysis = self._normalize_analysis(
            raw_output=raw_output,
            target_category=target_category,
            current_file_stem=file_stem,
        )
        async with self.saver._write_lock:
            return await asyncio.to_thread(
                self._move_sync,
                from_category,
                file_stem,
                target_category,
                analysis,
                job_id,
            )

    def _validate_category(self, category_id: str, field_name: str) -> None:
        if category_id not in self.catalog.get_categories():
            raise ValueError(f"{field_name} is not a known meme category: {category_id}")

    def _source_path(self, category_id: str, file_stem: str) -> str:
        path = self.catalog.get_image_path(category_id, file_stem)
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(f"meme image not found: {category_id}/{file_stem}")
        return path

    def _normalize_analysis(
        self,
        *,
        raw_output: str,
        target_category: str,
        current_file_stem: str,
    ) -> MemeReclassifyAnalysis:
        data = self.analyzer._parse_json(raw_output)
        warnings: list[str] = []
        keywords = self.analyzer._normalize_keywords(data.get("keywords"))
        save_name = self.analyzer._normalize_save_name(data.get("save_name"), target_category, keywords)
        if not save_name:
            warnings.append("invalid save_name_from_llm")
            save_name = self.analyzer._normalize_save_name(current_file_stem, target_category, keywords)
        if not save_name:
            raise ValueError("LLM did not provide a valid meme save_name")
        reason = self.analyzer._clean_optional_text(data.get("reason")) or ""
        safety = self.analyzer._clean_optional_text(data.get("safety")) or "unclear"
        return MemeReclassifyAnalysis(
            save_name=save_name,
            keywords=keywords,
            reason=reason,
            safety=safety,
            raw_output=raw_output,
            warnings=warnings,
        )

    def _move_sync(
        self,
        from_category: str,
        file_stem: str,
        target_category: str,
        analysis: MemeReclassifyAnalysis,
        job_id: Optional[str],
    ) -> MemeReclassifyResult:
        old_path = self._source_path(from_category, file_stem)
        try:
            with open(old_path, "rb") as f:
                image_bytes = f.read()
            ext = self.saver._detect_extension(image_bytes, os.path.splitext(old_path)[1])
            hash_before = self.catalog._compute_dhash(old_path)
            target_dir = os.path.join(self.catalog.assets_dir, target_category)
            os.makedirs(target_dir, exist_ok=True)
            target_path = self.saver._unique_target_path(target_dir, analysis.save_name or "", ext, image_bytes)
            if os.path.abspath(old_path) == os.path.abspath(target_path):
                self.catalog.sync_dhash_index()
                return MemeReclassifyResult(
                    status="no_op",
                    job_id=job_id,
                    from_category=from_category,
                    target_category=target_category,
                    original_file_stem=file_stem,
                    file_stem=os.path.splitext(os.path.basename(old_path))[0],
                    old_file_path=old_path,
                    file_path=old_path,
                    hash_before=hash_before,
                    hash_after=hash_before,
                    reason=analysis.reason or "already in target path",
                    analysis=analysis,
                )

            shutil.move(old_path, target_path)
            self.catalog.sync_dhash_index()
            hash_after = self.catalog._compute_dhash(target_path)
            return MemeReclassifyResult(
                status="moved",
                job_id=job_id,
                from_category=from_category,
                target_category=target_category,
                original_file_stem=file_stem,
                file_stem=os.path.splitext(os.path.basename(target_path))[0],
                old_file_path=old_path,
                file_path=target_path,
                hash_before=hash_before,
                hash_after=hash_after,
                reason=analysis.reason,
                analysis=analysis,
            )
        except Exception as exc:  # noqa: BLE001
            return MemeReclassifyResult(
                status="failed",
                job_id=job_id,
                from_category=from_category,
                target_category=target_category,
                original_file_stem=file_stem,
                old_file_path=old_path,
                reason=analysis.reason,
                error=str(exc),
                analysis=analysis,
            )


class MemeReclassifyQueue:
    def __init__(
        self,
        *,
        reclassifier: MemeReclassifier,
        llm_client_factory: Callable[[], LLMClient],
        max_history: int = 200,
    ):
        self.reclassifier = reclassifier
        self.llm_client_factory = llm_client_factory
        self.max_history = max(20, int(max_history))
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._jobs: dict[str, MemeReclassifyJob] = {}
        self._worker_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._worker_task and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(self._run())

    async def submit(self, *, from_category: str, file_stem: str, target_category: str) -> dict:
        self.start()
        job = MemeReclassifyJob(
            job_id=f"meme_reclassify_{uuid.uuid4().hex[:10]}",
            from_category=str(from_category or "").strip(),
            file_stem=str(file_stem or "").strip(),
            target_category=str(target_category or "").strip(),
        )
        self._jobs[job.job_id] = job
        await self._queue.put(job.job_id)
        self._trim_jobs()
        return job.to_payload(queue_position=self._queue.qsize())

    def get(self, job_id: str) -> Optional[dict]:
        job = self._jobs.get(job_id)
        if not job:
            return None
        return job.to_payload(queue_position=self._queue_position(job_id))

    def status(self) -> dict:
        jobs = [
            job.to_payload(queue_position=self._queue_position(job_id))
            for job_id, job in sorted(self._jobs.items(), key=lambda item: item[1].created_at, reverse=True)
        ]
        return {
            "running": bool(self._worker_task and not self._worker_task.done()),
            "queued": self._queue.qsize(),
            "jobs": jobs[:50],
        }

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if not job:
                self._queue.task_done()
                continue
            job.status = "running"
            job.started_at = datetime.now().isoformat()
            try:
                llm_client = self.llm_client_factory()
                if not getattr(llm_client, "api_key", ""):
                    raise RuntimeError("LLM API key is not configured")
                result = await self.reclassifier.reclassify(
                    from_category=job.from_category,
                    file_stem=job.file_stem,
                    target_category=job.target_category,
                    llm_client=llm_client,
                    job_id=job.job_id,
                )
                job.result = result.model_dump(mode="json")
                job.status = "completed" if result.status in {"moved", "no_op"} else "failed"
                job.error = result.error
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = str(exc)
                job.result = None
            finally:
                job.completed_at = datetime.now().isoformat()
                self._queue.task_done()
                self._trim_jobs()

    def _queue_position(self, job_id: str) -> Optional[int]:
        queued = list(self._queue._queue)
        try:
            return queued.index(job_id) + 1
        except ValueError:
            return None

    def _trim_jobs(self) -> None:
        if len(self._jobs) <= self.max_history:
            return
        terminal = [
            job
            for job in self._jobs.values()
            if job.status in {"completed", "failed"}
        ]
        terminal.sort(key=lambda job: job.completed_at or job.created_at)
        for job in terminal[: max(0, len(self._jobs) - self.max_history)]:
            self._jobs.pop(job.job_id, None)
