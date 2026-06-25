import asyncio
import copy
import inspect
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

from ..adapters.onebot11.media import OneBotMediaDownloader
from ..llm.client import LLMClient
from ..llm.prompts import build_image_understanding_messages
from ..memes.steal import MemeStealAnalyzer, MemeStealSaver


MediaJobCallback = Callable[[list[dict], "MediaJob"], Optional[Awaitable[None]]]


@dataclass(slots=True)
class MediaJob:
    media_key: str
    session_id: str
    snapshot_id: int
    buffer_version: int
    source_job_id: str
    event: dict
    media_ref: dict
    event_text: str = ""
    context_text: str = ""
    queued_at: datetime = field(default_factory=datetime.now)
    job_id: str = field(default_factory=lambda: f"media_{uuid.uuid4().hex[:10]}")
    generation: int = 0

    @property
    def is_sticker(self) -> bool:
        return bool(self.media_ref.get("is_sticker"))


class MediaJobQueue:
    """Background worker for QQ media understanding and silent meme intake.

    The main chat graph only appends a pending system payload and enqueues a
    MediaJob. Slow operations run here: media download, vision calls, detailed
    steal analysis, duplicate detection, file copy and index sync.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        media_downloader: OneBotMediaDownloader,
        meme_steal_analyzer: Optional[MemeStealAnalyzer] = None,
        meme_steal_saver: Optional[MemeStealSaver] = None,
        on_payloads: Optional[MediaJobCallback] = None,
        llm_client_factory: Optional[Callable[[str], LLMClient]] = None,
        max_workers: int = 1,
    ):
        self.llm = llm_client
        self.llm_client_factory = llm_client_factory
        self.media_downloader = media_downloader
        self.meme_steal_analyzer = meme_steal_analyzer
        self.meme_steal_saver = meme_steal_saver
        self.on_payloads = on_payloads
        self.max_workers = max(1, int(max_workers or 1))
        self._queue: asyncio.Queue[MediaJob] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._seen_keys: set[str] = set()
        self._running_keys: set[str] = set()
        self._last_payloads: list[dict] = []
        self._completed_payloads_for_prompt_by_session: dict[str, list[dict]] = {}
        self._generation = 0
        self._stats = {
            "queued": 0,
            "started": 0,
            "completed": 0,
            "failed": 0,
            "duplicates": 0,
            "skipped": 0,
        }
        self._lock = asyncio.Lock()

    def start(self):
        if self._workers:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for index in range(self.max_workers):
            self._workers.append(loop.create_task(self._worker_loop(index)))

    def set_llm_client_factory(self, factory: Optional[Callable[[str], LLMClient]]):
        self.llm_client_factory = factory

    async def shutdown(self):
        tasks = list(self._workers)
        self._workers.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def clear(self, session_id: Optional[str] = None):
        self._generation += 1
        if session_id:
            prefix = f"{session_id}:"
            self._seen_keys = {key for key in self._seen_keys if not key.startswith(prefix)}
            self._running_keys = {key for key in self._running_keys if not key.startswith(prefix)}
            self._completed_payloads_for_prompt_by_session.pop(session_id, None)
        else:
            self._seen_keys.clear()
            self._running_keys.clear()
            self._completed_payloads_for_prompt_by_session.clear()
        self._last_payloads.clear()
        self._stats = {key: 0 for key in self._stats}
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

    def enqueue(self, job: MediaJob) -> bool:
        if not job.media_key:
            return False
        if job.media_key in self._seen_keys:
            return False
        job.generation = self._generation
        self._seen_keys.add(job.media_key)
        self._stats["queued"] += 1
        self._queue.put_nowait(job)
        return True

    def get_last_payloads(self) -> list[dict]:
        return copy.deepcopy(self._last_payloads)

    def get_completed_payloads_for_prompt(self, session_id: Optional[str] = None) -> list[dict]:
        if session_id:
            return copy.deepcopy(self._completed_payloads_for_prompt_by_session.get(session_id, []))
        merged: list[dict] = []
        for payloads in self._completed_payloads_for_prompt_by_session.values():
            merged.extend(payloads)
        return copy.deepcopy(merged)

    def status(self, session_id: Optional[str] = None) -> dict:
        if session_id:
            prefix = f"{session_id}:"
            running = sorted(key for key in self._running_keys if key.startswith(prefix))
            seen_count = sum(1 for key in self._seen_keys if key.startswith(prefix))
            last_payloads = [
                payload
                for payload in self.get_last_payloads()
                if payload.get("session_id") == session_id
            ]
        else:
            running = sorted(self._running_keys)
            seen_count = len(self._seen_keys)
            last_payloads = self.get_last_payloads()
        return {
            "enabled": True,
            "session_id": session_id,
            "queue_size": self._queue.qsize(),
            "workers": len(self._workers),
            "running": running,
            "seen_count": seen_count,
            "generation": self._generation,
            "stats": dict(self._stats),
            "last_payloads": last_payloads,
        }

    async def _worker_loop(self, worker_index: int):
        while True:
            job = await self._queue.get()
            self._running_keys.add(job.media_key)
            self._stats["started"] += 1
            try:
                payloads = await self._process_job(job)
                if job.generation != self._generation:
                    self._stats["skipped"] += 1
                    continue
                self._last_payloads = payloads
                self._remember_prompt_payloads(payloads)
                self._update_stats_from_payloads(payloads)
                await self._emit_payloads(payloads, job)
            except Exception as exc:  # noqa: BLE001
                payload = self._job_failed_payload(job, f"media worker {worker_index} failed: {exc}")
                if job.generation != self._generation:
                    self._stats["skipped"] += 1
                    continue
                self._last_payloads = [payload]
                self._stats["failed"] += 1
                await self._emit_payloads([payload], job)
            finally:
                self._running_keys.discard(job.media_key)
                self._queue.task_done()

    async def _process_job(self, job: MediaJob) -> list[dict]:
        prepared_ref = dict(job.media_ref)
        download_payload = None
        local_path = str(prepared_ref.get("local_path") or "").strip()

        if self.media_downloader:
            download = await self.media_downloader.prepare_media_ref(
                prepared_ref,
                self._event_raw(job.event),
            )
            prepared_ref = download.media_ref
            local_path = download.local_path or ""
            download_payload = download.to_debug_payload()

        if not local_path:
            return [
                self._failed_image_understanding_payload(
                    job=job,
                    media_ref=prepared_ref,
                    error=(
                        download_payload.get("error")
                        if isinstance(download_payload, dict)
                        else "media has no local downloadable file"
                    ),
                    download=download_payload,
                )
            ]

        image_payload = await self._build_image_understanding_payload(
            job=job,
            media_ref=prepared_ref,
            local_path=local_path,
            download=download_payload,
        )
        payloads = [image_payload]

        if bool(prepared_ref.get("is_sticker")):
            payloads.append(
                await self._build_meme_intake_payload(
                    job=job,
                    media_ref=prepared_ref,
                    local_path=local_path,
                    image_understanding=image_payload,
                )
            )
        return payloads

    async def _build_image_understanding_payload(
        self,
        job: MediaJob,
        media_ref: dict,
        local_path: str,
        download: Optional[dict],
    ) -> dict:
        base = self._base_payload(
            job=job,
            media_ref=media_ref,
            download=download,
            harness="image_understanding_result",
            visibility="internal_only",
        )
        if not self.meme_steal_analyzer:
            return {
                **base,
                "status": "failed",
                "error": "meme_steal_analyzer is not configured, cannot prepare vision image_url",
                "instruction": self._image_harness_failure_instruction(job.is_sticker),
            }

        llm = None
        try:
            image_url = self.meme_steal_analyzer.to_model_image_url(local_path)
            messages = build_image_understanding_messages(
                image_url=image_url,
                is_sticker=bool(media_ref.get("is_sticker")),
                event_text=job.event_text,
                context_text=job.context_text,
            )
            llm = self._llm_for_job(job)
            raw_output = await llm.chat_completion(messages=messages, temperature=0.2)
            parsed = self._parse_internal_json_object(raw_output)
            return {
                **base,
                "status": "completed",
                "result": parsed,
                "raw_output": raw_output,
                "instruction": self._image_harness_success_instruction(job.is_sticker),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                **base,
                "status": "failed",
                "error": str(exc),
                "llm_call_debug": self._llm_call_debug(llm),
                "instruction": self._image_harness_failure_instruction(job.is_sticker),
            }

    async def _build_meme_intake_payload(
        self,
        job: MediaJob,
        media_ref: dict,
        local_path: str,
        image_understanding: dict,
    ) -> dict:
        base = self._base_payload(
            job=job,
            media_ref=media_ref,
            download=None,
            harness="meme_intake_result",
            visibility="internal_debug_only",
        )
        base.update({
            "user_visible_behavior": "silent",
            "instruction": (
                "这是后台偷表情入库结果。主聊天不能告诉用户已保存、已偷、重复、失败或任何技术细节；"
                "只把表情包当作用户当前心情/语气信号自然接住。"
            ),
        })
        if not self.meme_steal_analyzer:
            return {**base, "status": "skipped", "reason": "meme_steal_analyzer is not configured"}

        llm = None
        try:
            llm = self._llm_for_job(job)
            analysis = await self.meme_steal_analyzer.analyze(
                image_ref=local_path,
                llm_client=llm,
                context_text=self._meme_intake_context_text(job.context_text, image_understanding),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                **base,
                "status": "failed",
                "error": str(exc),
                "llm_call_debug": self._llm_call_debug(llm),
            }

        save_result = None
        if analysis.should_steal and self.meme_steal_saver:
            save_result = await self.meme_steal_saver.save_from_analysis(local_path, analysis)
        elif analysis.should_steal:
            save_result = {
                "status": "skipped",
                "error": "meme_steal_saver is not configured",
            }

        if hasattr(save_result, "to_internal_payload"):
            save_payload = save_result.to_internal_payload()
        else:
            save_payload = save_result

        return {
            **base,
            "status": (save_payload or {}).get("status", "skipped") if isinstance(save_payload, dict) else "skipped",
            "analysis": analysis.model_dump(mode="json"),
            "save_result": save_payload,
        }

    def _base_payload(
        self,
        job: MediaJob,
        media_ref: dict,
        download: Optional[dict],
        harness: str,
        visibility: str,
    ) -> dict:
        payload = {
            "internal_event_harness": harness,
            "media_key": job.media_key,
            "media_job_id": job.job_id,
            "session_id": job.session_id,
            "source_job_id": job.source_job_id,
            "snapshot_id": job.snapshot_id,
            "buffer_version": job.buffer_version,
            "is_sticker": bool(media_ref.get("is_sticker")),
            "media_ref": self._safe_media_ref_for_debug(media_ref),
            "visibility": visibility,
            "completed_at": datetime.now().isoformat(),
        }
        safe_download = self._safe_download_for_debug(download)
        if safe_download:
            payload["download"] = safe_download
        return payload

    def _failed_image_understanding_payload(
        self,
        job: MediaJob,
        media_ref: dict,
        error: Optional[str],
        download: Optional[dict],
    ) -> dict:
        return {
            **self._base_payload(
                job=job,
                media_ref=media_ref,
                download=download,
                harness="image_understanding_result",
                visibility="internal_only",
            ),
            "status": "failed",
            "error": error or "media download failed",
            "instruction": self._image_harness_failure_instruction(bool(media_ref.get("is_sticker"))),
        }

    def _job_failed_payload(self, job: MediaJob, error: str) -> dict:
        return {
            "internal_event_harness": "media_job",
            "media_key": job.media_key,
            "media_job_id": job.job_id,
            "source_job_id": job.source_job_id,
            "snapshot_id": job.snapshot_id,
            "buffer_version": job.buffer_version,
            "status": "failed",
            "error": error,
            "visibility": "internal_debug_only",
            "completed_at": datetime.now().isoformat(),
        }

    async def _emit_payloads(self, payloads: list[dict], job: MediaJob):
        if not self.on_payloads or not payloads:
            return
        result = self.on_payloads(payloads, job)
        if inspect.isawaitable(result):
            await result

    def _update_stats_from_payloads(self, payloads: list[dict]):
        if any(payload.get("status") == "failed" for payload in payloads):
            self._stats["failed"] += 1
        else:
            self._stats["completed"] += 1
        if any(payload.get("status") == "duplicate" for payload in payloads):
            self._stats["duplicates"] += 1
        if any(payload.get("status") == "skipped" for payload in payloads):
            self._stats["skipped"] += 1

    def _remember_prompt_payloads(self, payloads: list[dict]):
        for payload in payloads:
            if payload.get("internal_event_harness") != "image_understanding_result":
                continue
            prompt_payload = self._payload_for_prompt(payload)
            if prompt_payload:
                session_id = str(prompt_payload.get("session_id") or "")
                if session_id:
                    self._completed_payloads_for_prompt_by_session.setdefault(session_id, []).append(prompt_payload)

    def _payload_for_prompt(self, payload: dict) -> dict:
        def sanitize(value):
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if key in {"raw_output", "local_path", "file_path", "matched_file", "llm_call_debug"}:
                        continue
                    result[key] = sanitize(item)
                return result
            if isinstance(value, list):
                return [sanitize(item) for item in value]
            return value

        result = sanitize(payload)
        result["visibility"] = "internal_only_not_visible_to_user"
        result["prompt_usage"] = "available_for_future_turns"
        return result

    def _event_raw(self, event: dict) -> dict:
        raw = event.get("raw") or {}
        return raw if isinstance(raw, dict) else {}

    def _llm_for_job(self, job: MediaJob) -> LLMClient:
        if self.llm_client_factory:
            return self.llm_client_factory(job.session_id)
        return self.llm

    def _llm_call_debug(self, llm: Optional[LLMClient]) -> Optional[dict]:
        if llm is None or not hasattr(llm, "get_last_call_debug"):
            return None
        debug = llm.get_last_call_debug()
        return debug if debug else None

    def _meme_intake_context_text(self, context_text: str, image_understanding: dict) -> str:
        result = image_understanding.get("result") if isinstance(image_understanding, dict) else None
        if result:
            return f"{context_text}\n\n表情包轻量理解结果: {json.dumps(result, ensure_ascii=False)}"
        return context_text

    def _parse_internal_json_object(self, raw_output: str) -> dict:
        text = (raw_output or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                raise ValueError("internal harness did not return a JSON object")
            data = json.loads(match.group(0))
        if not isinstance(data, dict):
            raise ValueError("internal harness JSON result must be an object")
        return data

    def _safe_media_ref_for_debug(self, media_ref: dict) -> dict:
        allowed_keys = (
            "source",
            "qq_user_id",
            "onebot_message_id",
            "segment_index",
            "segment_type",
            "sub_type",
            "summary",
            "file",
            "file_id",
            "file_unique",
            "file_size",
            "is_sticker",
            "local_path",
            "sha256",
            "download_status",
            "download_error",
        )
        return {key: media_ref.get(key) for key in allowed_keys if media_ref.get(key) is not None}

    def _safe_download_for_debug(self, download: Optional[dict]) -> Optional[dict]:
        if not isinstance(download, dict):
            return None
        return {
            "status": download.get("status"),
            "local_path": download.get("local_path"),
            "sha256": download.get("sha256"),
            "source_field": download.get("source_field"),
            "content_type": download.get("content_type"),
            "bytes": download.get("bytes"),
            "error": download.get("error"),
        }

    def _image_harness_success_instruction(self, is_sticker: bool) -> str:
        if is_sticker:
            return (
                "用户发的是表情包/贴纸，通常只是表达心情或接梗；主聊天轻轻接住即可，"
                "不要把分析结果讲给用户，必要时可短句或回表情包。"
            )
        return (
            "用户发的是普通图片，第一语义是分享；主聊天应明确回应图片内容及其和前文的关系。"
        )

    def _image_harness_failure_instruction(self, is_sticker: bool) -> str:
        if is_sticker:
            return "表情包没有成功看清；主聊天不要编造表情内容，可以按用户文字或轻量互动继续。"
        return "图片没有成功看清；主聊天不能编造图片内容，可以自然说明没看清或基于用户文字轻问一句。"
