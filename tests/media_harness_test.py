import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.adapters.onebot11.media import MediaDownloadResult, OneBotMediaDownloader
from app.core.event_gate import ProcessContext
from app.core.graph import CompanionGraph
from app.core.media_jobs import MediaJob, MediaJobQueue
from app.core.state import ChatStatus, ConversationSnapshot
from app.llm.client import LLMResponseEnvelope
from app.llm.prompts import build_image_understanding_messages
from app.memes.catalog import MemeCatalog
from app.memes.steal import MemeStealAnalysis, MemeStealSaveResult, MemeStealSaver


class FakeMemory:
    def read_soul(self):
        return "# SOUL\n稳定角色"

    def read_memory_core(self):
        return "# MEMORY_CORE\n"

    def read_today_memory(self):
        return "# 每日记忆\n"

    def read_tomorrow_topics(self):
        return "# 明日话题\n"


class FakeMemeCatalog:
    assets_dir = ""
    base_dir = ""

    def get_images_in_category(self, category):
        return []

    def get_categories(self):
        return {"amused": {"name": "开心", "description": "开心好笑"}}

    def get_category_display_order(self):
        return ["amused"]

    def get_image_path(self, category_id, file_stem):
        return ""


class FakeMediaDownloader:
    def __init__(self, local_path: str):
        self.local_path = local_path

    async def prepare_media_ref(self, media_ref, event_raw=None):
        updated = dict(media_ref)
        updated.update({
            "local_path": self.local_path,
            "sha256": "fake-sha256",
            "download_status": "downloaded",
            "download_error": None,
        })
        return MediaDownloadResult(
            status="downloaded",
            media_ref=updated,
            local_path=self.local_path,
            sha256="fake-sha256",
            source_field="path",
            content_type="image/png",
            bytes=123,
        )


class FakeOneBotManager:
    def __init__(self, image_response: dict):
        self.image_response = image_response
        self.get_image_calls = []

    async def get_image(self, file):
        self.get_image_calls.append(file)
        return self.image_response

    async def get_msg(self, message_id):
        return {"data": {"message": []}}

    async def get_file(self, file_id):
        return {"data": {}}


class FakeVisionAnalyzer:
    def __init__(self):
        self.analyze_calls = []

    def to_model_image_url(self, image_ref: str) -> str:
        tiny_png = base64.b64encode(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        ).decode("ascii")
        return f"data:image/png;base64,{tiny_png}"

    async def analyze(self, image_ref, llm_client, context_text=""):
        self.analyze_calls.append({
            "image_ref": image_ref,
            "context_text": context_text,
        })
        return MemeStealAnalysis(
            should_steal=True,
            category="amused",
            save_name="amused_cat_laugh_sticker",
            keywords=["cat", "laugh"],
            reason="适合作为开心反应图",
            safety="ok",
            image_ref=image_ref,
        )


class FakeMemeSaver:
    def __init__(self):
        self.save_calls = []

    async def save_from_analysis(self, image_ref, analysis):
        self.save_calls.append((image_ref, analysis))
        return MemeStealSaveResult(
            status="saved",
            saved=True,
            category=analysis.category,
            file_stem=analysis.save_name,
            file_path=str(Path(image_ref)),
            reason=analysis.reason,
            analysis=analysis,
        )


class FakeVisionLLM:
    def __init__(self):
        self.main_messages = []

    async def chat_completion(self, messages, temperature=0.7, max_tokens=None, stream=False):
        serialized = json.dumps(messages, ensure_ascii=False)
        if "表情包理解器" in serialized:
            return json.dumps({
                "kind": "sticker",
                "visible_summary": "小猫笑得很开心",
                "user_mood": "开心",
                "interaction_intent": "mood_only",
                "reply_bias": "send_meme_back",
                "confidence": "high",
            }, ensure_ascii=False)
        return json.dumps({
            "kind": "photo",
            "visible_summary": "桌上有一杯咖啡",
            "relation_to_context": "用户在分享日常",
            "user_intent": "分享今天喝咖啡续命",
            "desired_response": "希望被接住并轻松回应",
            "reply_style": "warm",
            "confidence": "high",
        }, ensure_ascii=False)

    async def chat_completion_envelope(self, messages, temperature=0.7):
        self.main_messages.append(messages)
        serialized = json.dumps(messages, ensure_ascii=False)
        if "image_understanding_result" in serialized and "桌上有一杯咖啡" in serialized:
            raw = '{"action":"REPLY","items":[{"text":"看到啦，是咖啡续命现场对吧"}]}'
        elif "media_pending" in serialized:
            raw = '{"action":"REPLY","items":[{"text":"我看看"}]}'
        elif "sticker_pending" in serialized:
            raw = '{"action":"REPLY","items":[{"text":"哈哈哈"}]}'
        else:
            raw = '{"action":"REPLY","items":[{"text":"收到"}]}'
        return LLMResponseEnvelope(
            assistant_message={"role": "assistant", "content": raw},
            content=raw,
        )

    def get_last_usage(self):
        return None


class MediaHarnessTest(unittest.TestCase):
    def test_image_understanding_prompt_treats_photo_as_contextual_share(self):
        messages = build_image_understanding_messages(
            image_url="data:image/png;base64,abc",
            is_sticker=False,
            event_text="[图片]",
            context_text="user: 我刚说楼下咖啡太苦了\nassistant: 又苦又续命",
        )
        serialized = json.dumps(messages, ensure_ascii=False)

        self.assertIn("用户发普通图片首先是在分享", serialized)
        self.assertIn("必须主动阅读聊天上下文", serialized)
        self.assertIn("用户为什么分享", serialized)
        self.assertIn("只有真的看不出关系时才写 unknown", serialized)
        self.assertIn("楼下咖啡太苦了", serialized)

    def test_sticker_understanding_prompt_keeps_analysis_internal(self):
        messages = build_image_understanding_messages(
            image_url="data:image/png;base64,abc",
            is_sticker=True,
            event_text="[表情]",
            context_text="user: 今天终于下班了",
        )
        serialized = json.dumps(messages, ensure_ascii=False)

        self.assertIn("用户发表情包通常只是表达心情、语气或接梗", serialized)
        self.assertIn("不要过度分析", serialized)
        self.assertIn("回一个相近表情包", serialized)
        self.assertIn("这个表情包很可爱", serialized)
        self.assertIn("你是不是很开心/发生什么事了", serialized)
        self.assertIn("不要制造新的追问", serialized)

    def test_downloader_copies_local_path_into_onebot_media_store(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = _write_image(root / "napcat-cache.png", (10, 20, 30))
            downloader = OneBotMediaDownloader(str(root))

            result = asyncio.run(downloader.prepare_media_ref({
                "onebot_message_id": "1001",
                "segment_index": 0,
                "file": "cache.png",
                "path": str(source),
                "is_sticker": False,
            }))

            self.assertEqual(result.status, "downloaded")
            self.assertTrue(Path(result.local_path).is_file())
            self.assertIn("data", Path(result.local_path).parts)
            self.assertEqual(result.media_ref["download_status"], "downloaded")
            self.assertEqual(len(result.sha256), 64)

    def test_downloader_corrects_extension_from_file_header(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            png_source = _write_image(root / "napcat-cache.png", (10, 20, 30))
            source = root / "napcat-cache.jpg"
            png_source.replace(source)
            downloader = OneBotMediaDownloader(str(root))

            result = asyncio.run(downloader.prepare_media_ref({
                "onebot_message_id": "1001",
                "segment_index": 0,
                "file": "cache.jpg",
                "path": str(source),
                "is_sticker": False,
            }))

            self.assertEqual(result.status, "downloaded")
            self.assertEqual(Path(result.local_path).suffix, ".png")
            self.assertEqual(result.content_type, "image/png")

    def test_downloader_falls_back_to_get_image_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = _write_image(root / "napcat-cache.png", (10, 20, 30))
            manager = FakeOneBotManager({"data": {"path": str(source)}})
            downloader = OneBotMediaDownloader(str(root), manager)

            result = asyncio.run(downloader.prepare_media_ref({
                "onebot_message_id": "1001",
                "segment_index": 0,
                "file": "D0B97E3C.webp",
                "is_sticker": False,
            }))

            self.assertEqual(result.status, "downloaded")
            self.assertEqual(manager.get_image_calls, ["D0B97E3C.webp"])
            self.assertTrue(Path(result.local_path).is_file())

    def test_downloader_fails_when_file_exceeds_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = _write_image(root / "napcat-cache.png", (10, 20, 30))
            downloader = OneBotMediaDownloader(str(root), max_bytes=4)

            result = asyncio.run(downloader.prepare_media_ref({
                "onebot_message_id": "1001",
                "segment_index": 0,
                "file": "cache.png",
                "path": str(source),
                "is_sticker": False,
            }))

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.media_ref["download_status"], "failed")

    def test_meme_steal_saver_saves_to_assets_and_syncs_dhash(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            media_dir = root / "data" / "onebot_media" / "2026-06-21"
            source = _write_image(media_dir / "incoming.png", (240, 80, 80))
            meme_base = root / "memes"
            (meme_base / "assets").mkdir(parents=True)
            (meme_base / "memes_data.json").write_text(
                json.dumps({"amused": {"name": "开心", "description": "开心好笑"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            catalog = MemeCatalog(str(meme_base))
            saver = MemeStealSaver(catalog, str(root))

            analysis = MemeStealAnalysis(
                should_steal=True,
                category="amused",
                save_name="amused_cat_laugh_sticker",
                keywords=["cat", "laugh"],
                reason="适合作为开心反应图",
                safety="ok",
                image_ref=str(source),
            )
            result = asyncio.run(saver.save_from_analysis(str(source), analysis))

            self.assertEqual(result.status, "saved")
            self.assertTrue(result.saved)
            self.assertEqual(result.file_stem, "amused_cat_laugh_sticker")
            self.assertTrue((meme_base / "assets" / "amused" / "amused_cat_laugh_sticker.png").is_file())
            index = json.loads((meme_base / "image_dhash_index.json").read_text(encoding="utf-8"))
            self.assertTrue(any("amused_cat_laugh_sticker.png" in key for key in index))

    def test_graph_enqueues_ordinary_image_without_waiting_for_vision(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                image_path = _write_image(root / "image.png", (40, 90, 160))
                llm = FakeVisionLLM()
                analyzer = FakeVisionAnalyzer()
                queue = MediaJobQueue(
                    llm_client=llm,
                    media_downloader=FakeMediaDownloader(str(image_path)),
                    meme_steal_analyzer=analyzer,
                )
                graph = CompanionGraph(
                    llm,
                    FakeMemory(),
                    FakeMemeCatalog(),
                    media_job_queue=queue,
                )
                ctx = _process_context({
                    "event_id": "img-1",
                    "event_type": "message.image",
                    "text": "[图片]",
                    "raw": {
                        "media_refs": [{
                            "segment_index": 0,
                            "segment_type": "image",
                            "is_sticker": False,
                            "file": "image.png",
                        }]
                    },
                })

                decision = await graph.run(ctx)

                self.assertEqual(decision.all_items()[0].content, "我看看")
                debug = graph.get_last_media_debug()
                self.assertEqual(debug[0]["internal_event_harness"], "media_pending")
                self.assertEqual(debug[0]["status"], "queued")
                self.assertEqual(queue.status()["queue_size"], 1)
                self.assertEqual(analyzer.analyze_calls, [])
                serialized_main = json.dumps(llm.main_messages[-1], ensure_ascii=False)
                self.assertIn("media_pending", serialized_main)
                self.assertIn("queued_for_download_vision_and_intake", serialized_main)
                self.assertNotIn(str(image_path), serialized_main)

        asyncio.run(scenario())

    def test_media_job_queue_runs_sticker_understanding_and_silent_meme_intake(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                image_path = _write_image(root / "sticker.png", (240, 200, 20))
                analyzer = FakeVisionAnalyzer()
                saver = FakeMemeSaver()
                llm = FakeVisionLLM()
                completed = []
                queue = MediaJobQueue(
                    llm_client=llm,
                    media_downloader=FakeMediaDownloader(str(image_path)),
                    meme_steal_analyzer=analyzer,
                    meme_steal_saver=saver,
                    on_payloads=lambda payloads, job: completed.append((payloads, job)),
                )
                job = MediaJob(
                    media_key="sticker-1:0:sticker.png",
                    session_id="default",
                    snapshot_id=1,
                    buffer_version=1,
                    source_job_id="job-1",
                    event={
                        "event_id": "sticker-1",
                        "event_type": "message.sticker",
                        "text": "[表情]",
                        "raw": {"media_refs": []},
                    },
                    media_ref={
                        "segment_index": 0,
                        "segment_type": "image",
                        "sub_type": 1,
                        "summary": "[动画表情]",
                        "is_sticker": True,
                        "file": "sticker.png",
                    },
                    event_text="[表情]",
                    context_text="user: 今天终于下班了",
                )

                queue.start()
                self.assertTrue(queue.enqueue(job))
                await asyncio.wait_for(queue._queue.join(), timeout=1)
                await queue.shutdown()

                payloads = completed[0][0]
                self.assertEqual([item["internal_event_harness"] for item in payloads], [
                    "image_understanding_result",
                    "meme_intake_result",
                ])
                self.assertEqual(payloads[1]["status"], "saved")
                self.assertEqual(len(analyzer.analyze_calls), 1)
                self.assertEqual(len(saver.save_calls), 1)
                self.assertEqual(payloads[1]["user_visible_behavior"], "silent")

        asyncio.run(scenario())

    def test_completed_image_result_is_available_for_future_prompt_without_paths(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                image_path = _write_image(root / "image.png", (40, 90, 160))
                llm = FakeVisionLLM()
                queue = MediaJobQueue(
                    llm_client=llm,
                    media_downloader=FakeMediaDownloader(str(image_path)),
                    meme_steal_analyzer=FakeVisionAnalyzer(),
                )
                job = MediaJob(
                    media_key="img-1:0:image.png",
                    session_id="default",
                    snapshot_id=1,
                    buffer_version=1,
                    source_job_id="job-1",
                    event={
                        "event_id": "img-1",
                        "event_type": "message.image",
                        "text": "[图片]",
                        "raw": {"media_refs": []},
                    },
                    media_ref={
                        "segment_index": 0,
                        "segment_type": "image",
                        "is_sticker": False,
                        "file": "image.png",
                    },
                    event_text="[图片]",
                    context_text="user: 今天靠咖啡续命",
                )
                queue.start()
                self.assertTrue(queue.enqueue(job))
                await asyncio.wait_for(queue._queue.join(), timeout=1)
                await queue.shutdown()

                prompt_payloads = queue.get_completed_payloads_for_prompt()
                self.assertEqual(prompt_payloads[0]["internal_event_harness"], "image_understanding_result")
                serialized_payloads = json.dumps(prompt_payloads, ensure_ascii=False)
                self.assertIn("桌上有一杯咖啡", serialized_payloads)
                self.assertNotIn("raw_output", serialized_payloads)
                self.assertNotIn(str(image_path), serialized_payloads)

                graph = CompanionGraph(
                    llm,
                    FakeMemory(),
                    FakeMemeCatalog(),
                    media_job_queue=queue,
                )
                decision = await graph.run(_process_context({
                    "event_id": "text-1",
                    "event_type": "message.text",
                    "text": "那个图怎么样",
                    "raw": {},
                }))

                self.assertEqual(decision.all_items()[0].content, "看到啦，是咖啡续命现场对吧")
                serialized_main = json.dumps(llm.main_messages[-1], ensure_ascii=False)
                self.assertIn("桌上有一杯咖啡", serialized_main)
                self.assertNotIn(str(image_path), serialized_main)

        asyncio.run(scenario())

    def test_graph_enqueues_sticker_pending_without_running_intake_on_main_thread(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                image_path = _write_image(root / "sticker.png", (240, 200, 20))
                analyzer = FakeVisionAnalyzer()
                saver = FakeMemeSaver()
                llm = FakeVisionLLM()
                queue = MediaJobQueue(
                    llm_client=llm,
                    media_downloader=FakeMediaDownloader(str(image_path)),
                    meme_steal_analyzer=analyzer,
                    meme_steal_saver=saver,
                )
                graph = CompanionGraph(
                    llm,
                    FakeMemory(),
                    FakeMemeCatalog(),
                    media_job_queue=queue,
                )
                ctx = _process_context({
                    "event_id": "sticker-1",
                    "event_type": "message.sticker",
                    "text": "[表情]",
                    "raw": {
                        "media_refs": [{
                            "segment_index": 0,
                            "segment_type": "image",
                            "sub_type": 1,
                            "summary": "[动画表情]",
                            "is_sticker": True,
                            "file": "sticker.png",
                        }]
                    },
                })

                await graph.run(ctx)

                debug = graph.get_last_media_debug()
                self.assertEqual(debug[0]["internal_event_harness"], "sticker_pending")
                self.assertEqual(debug[0]["status"], "queued")
                self.assertEqual(len(analyzer.analyze_calls), 0)
                self.assertEqual(len(saver.save_calls), 0)
                serialized_main = json.dumps(llm.main_messages[-1], ensure_ascii=False)
                self.assertIn("sticker_pending", serialized_main)
                self.assertIn("queued_for_download_vision_and_intake", serialized_main)
                self.assertNotIn(str(image_path), serialized_main)

        asyncio.run(scenario())


def _process_context(event: dict) -> ProcessContext:
    snapshot = ConversationSnapshot(
        session_id="default",
        snapshot_id=1,
        buffer_version=1,
        status=ChatStatus.HOT,
        events=[event],
    )
    fake_gate = SimpleNamespace(
        state=SimpleNamespace(msg_index_today=1),
        _get_last_message_age=lambda: "just now",
    )
    return ProcessContext(fake_gate, "job-1", snapshot)


def _write_image(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (16, 16), color)
    image.save(path)
    return path


if __name__ == "__main__":
    unittest.main()
