import asyncio
import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.scheduler.jobs as scheduler_jobs
import app.api.routes as routes
import app.storage.context_checkpoints as checkpoint_store
from app.scheduler.jobs import SchedulerManager
from app.storage.models import Base, ContextCheckpoint, ConversationEvent, RawChatLog


class FakeCheckpointLLM:
    api_key = "test-key"

    def __init__(self):
        self.requests = []

    async def chat_completion(self, messages, temperature=0.7):
        self.requests.append([dict(item) for item in messages])
        return json.dumps({"checkpoint_text": "压缩摘要：用户最近在聊考试。"}, ensure_ascii=False)


class FakeMemoryManager:
    def for_session(self, _session_id):
        return self

    def read_memory_core(self):
        return "# 永久核心记忆\n"

    def read_dm_file(self, _date_str):
        return "# 每日记忆\n"

    def read_tomorrow_topics(self):
        return "# 明日话题\n"


class FakeGroupCheckpointLLM:
    api_key = "test-key"

    def __init__(self):
        self.requests = []

    async def chat_completion(self, messages, temperature=0.7):
        self.requests.append([dict(item) for item in messages])
        return json.dumps({
            "checkpoint_text": "群聊压缩摘要：这不是当前群友刚说的话。群里最近在聊副本和战绩截图。"
        }, ensure_ascii=False)


class FakeGroupMemoryManager:
    def read(self, _session_id):
        return "# 群聊记忆\n## 群友身份\n- 550808201: 夏\n## 共同记忆\n- 常聊副本\n## 个人相关记忆\n"

    def read_dm_file(self, _session_id, _date_str):
        return "# 群聊日记忆\n## 今日群聊大事\n- 讨论副本\n"

    def read_tomorrow_topics(self, _session_id):
        return "# 明日话题\n## 未闭合话题\n- [pending] 继续聊副本\n## 昨日记忆\n## 生活感消息备选\n"

    def clear(self, _session_id=None):
        return None


class FakeClearable:
    def __init__(self):
        self.calls = []

    def clear(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class FakeAsyncClearable(FakeClearable):
    async def clear(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class ContextCheckpointTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.original_jobs_session = scheduler_jobs.SessionLocal
        self.original_store_session = checkpoint_store.SessionLocal
        self.original_load_settings = scheduler_jobs.load_settings
        scheduler_jobs.SessionLocal = self.Session
        checkpoint_store.SessionLocal = self.Session
        scheduler_jobs.load_settings = lambda: {"context_checkpoint": {"threshold_k": 500}}

    def tearDown(self):
        scheduler_jobs.SessionLocal = self.original_jobs_session
        checkpoint_store.SessionLocal = self.original_store_session
        scheduler_jobs.load_settings = self.original_load_settings

    def test_scheduler_creates_context_checkpoint_above_threshold(self):
        async def scenario():
            db = self.Session()
            try:
                db.add_all([
                    ConversationEvent(
                        session_id="qq_private_10001",
                        event_type="user_text",
                        text="我明天还要继续准备考试",
                        is_visible=True,
                    ),
                    ConversationEvent(
                        session_id="qq_private_10001",
                        event_type="assistant_text",
                        text="那我明天接着陪你梳理。",
                        is_visible=True,
                    ),
                    RawChatLog(
                        event_id="debug1",
                        session_id="qq_private_10001",
                        event_type="prompt_cache_debug",
                        raw_payload=json.dumps({
                            "estimated_prompt_tokens": 510000,
                            "llm_usage": {"prompt_tokens": 600000},
                        }),
                        status="debug",
                    ),
                ])
                db.commit()
                latest_event_id = (
                    db.query(ConversationEvent.id)
                    .filter(ConversationEvent.session_id == "qq_private_10001")
                    .order_by(ConversationEvent.id.desc())
                    .first()[0]
                )
                prompt_debug_id = (
                    db.query(RawChatLog.id)
                    .filter(RawChatLog.event_type == "prompt_cache_debug")
                    .first()[0]
                )
            finally:
                db.close()

            llm = FakeCheckpointLLM()
            callbacks = []
            manager = SchedulerManager(memory_manager=FakeMemoryManager(), llm_client=llm)
            manager.set_context_checkpoint_callback(
                lambda session_id, checkpoint_text, history: callbacks.append((session_id, checkpoint_text, history))
            )

            ok, error = await manager._run_context_checkpoint_for_session("context_checkpoint_test", "qq_private_10001")

            self.assertTrue(ok, error)
            self.assertEqual(len(llm.requests), 1)
            payload = json.loads(llm.requests[0][1]["content"])
            self.assertEqual(payload["estimated_tokens_before"], 600000)
            self.assertEqual(len(payload["visible_events"]), 2)

            db = self.Session()
            try:
                checkpoint = db.query(ContextCheckpoint).one()
                self.assertEqual(checkpoint.session_id, "qq_private_10001")
                self.assertEqual(checkpoint.checkpoint_text, "压缩摘要：用户最近在聊考试。")
                self.assertEqual(checkpoint.covered_until_event_id, latest_event_id)
                self.assertEqual(checkpoint.source_prompt_debug_id, prompt_debug_id)
                self.assertEqual(checkpoint.estimated_tokens_before, 510000)
                self.assertEqual(checkpoint.prompt_tokens_before, 600000)
            finally:
                db.close()

            self.assertEqual(callbacks, [("qq_private_10001", "压缩摘要：用户最近在聊考试。", [])])

        asyncio.run(scenario())

    def test_scheduler_filters_internal_leaks_from_checkpoint_input_but_covers_boundary(self):
        async def scenario():
            db = self.Session()
            try:
                db.add_all([
                    ConversationEvent(
                        session_id="qq_private_903919427",
                        event_type="user_text",
                        text="我们刚才在聊考试压力和道歉。",
                        is_visible=True,
                    ),
                    ConversationEvent(
                        session_id="qq_private_903919427",
                        event_type="assistant_text",
                        text="<tool_call>\n<tool_name>run_background_process</tool_name>\n</tool_call>",
                        is_visible=True,
                    ),
                    ConversationEvent(
                        session_id="qq_private_903919427",
                        event_type="user_text",
                        text="[[RECONNECTION_MEMORY_SILENCE_VIBE_CHECK_TRIGGER]] 🤢",
                        is_visible=True,
                    ),
                    ConversationEvent(
                        session_id="qq_private_903919427",
                        event_type="assistant_text",
                        text="我记得你现在最在意的是别再忘掉关键上下文。",
                        is_visible=True,
                    ),
                    ConversationEvent(
                        session_id="qq_private_903919427",
                        event_type="assistant_text",
                        text="&&resting:zzz&& 🌙",
                        is_visible=True,
                    ),
                    ConversationEvent(
                        session_id="qq_private_903919427",
                        event_type="assistant_text",
                        text="[[quote]]",
                        is_visible=True,
                    ),
                    ConversationEvent(
                        session_id="qq_private_903919427",
                        event_type="assistant_text",
                        text="[/quote]]",
                        is_visible=True,
                    ),
                    ConversationEvent(
                        session_id="qq_private_903919427",
                        event_type="assistant_text",
                        text="confused:pout&&",
                        is_visible=True,
                    ),
                    ConversationEvent(
                        session_id="qq_private_903919427",
                        event_type="assistant_text",
                        text="&&resting:zzz|||",
                        is_visible=True,
                    ),
                    RawChatLog(
                        event_id="debug903",
                        session_id="qq_private_903919427",
                        event_type="prompt_cache_debug",
                        raw_payload=json.dumps({
                            "estimated_prompt_tokens": 610000,
                            "llm_usage": {"prompt_tokens": 650000},
                        }),
                        status="debug",
                    ),
                ])
                db.commit()
                latest_event_id = (
                    db.query(ConversationEvent.id)
                    .filter(ConversationEvent.session_id == "qq_private_903919427")
                    .order_by(ConversationEvent.id.desc())
                    .first()[0]
                )
            finally:
                db.close()

            llm = FakeCheckpointLLM()
            manager = SchedulerManager(memory_manager=FakeMemoryManager(), llm_client=llm)

            ok, error = await manager._run_context_checkpoint_for_session("context_checkpoint_test", "qq_private_903919427")

            self.assertTrue(ok, error)
            self.assertEqual(len(llm.requests), 1)
            payload = json.loads(llm.requests[0][1]["content"])
            self.assertEqual(
                [event["text"] for event in payload["visible_events"]],
                [
                    "我们刚才在聊考试压力和道歉。",
                    "我记得你现在最在意的是别再忘掉关键上下文。",
                ],
            )

            db = self.Session()
            try:
                checkpoint = db.query(ContextCheckpoint).one()
                self.assertEqual(checkpoint.covered_until_event_id, latest_event_id)
            finally:
                db.close()

        asyncio.run(scenario())

    def test_loaded_post_checkpoint_history_filters_internal_leaks(self):
        db = self.Session()
        try:
            db.add(ConversationEvent(
                session_id="qq_private_903919427",
                event_type="user_text",
                text="checkpoint 之前的旧内容",
                is_visible=True,
            ))
            db.flush()
            covered_event_id = (
                db.query(ConversationEvent.id)
                .filter(ConversationEvent.session_id == "qq_private_903919427")
                .order_by(ConversationEvent.id.desc())
                .first()[0]
            )
            db.add(ContextCheckpoint(
                session_id="qq_private_903919427",
                checkpoint_text="压缩摘要：保留考试压力和关系修复上下文。",
                covered_until_event_id=covered_event_id,
                source_prompt_debug_id=1,
            ))
            db.add_all([
                ConversationEvent(
                    session_id="qq_private_903919427",
                    event_type="assistant_text",
                    text="[[MEMORIZATION_INTENTS_START]]",
                    is_visible=True,
                ),
                ConversationEvent(
                    session_id="qq_private_903919427",
                    event_type="assistant_text",
                    text="[[quote]]",
                    is_visible=True,
                ),
                ConversationEvent(
                    session_id="qq_private_903919427",
                    event_type="assistant_text",
                    text="[/quote]]",
                    is_visible=True,
                ),
                ConversationEvent(
                    session_id="qq_private_903919427",
                    event_type="assistant_text",
                    text="confused:pout&&",
                    is_visible=True,
                ),
                ConversationEvent(
                    session_id="qq_private_903919427",
                    event_type="assistant_text",
                    text="&&resting:zzz|||",
                    is_visible=True,
                ),
                ConversationEvent(
                    session_id="qq_private_903919427",
                    event_type="assistant_text",
                    text="checkpoint 之后应该保留的干净内容",
                    is_visible=True,
                ),
            ])
            db.commit()
        finally:
            db.close()

        context = checkpoint_store.load_conversation_context("qq_private_903919427")

        self.assertEqual(context["checkpoint_text"], "压缩摘要：保留考试压力和关系修复上下文。")
        self.assertEqual(
            context["history"],
            [{"role": "assistant", "text": "checkpoint 之后应该保留的干净内容"}],
        )

    def test_scheduler_skips_context_checkpoint_below_threshold(self):
        async def scenario():
            db = self.Session()
            try:
                db.add(RawChatLog(
                    event_id="debug1",
                    session_id="qq_private_10001",
                    event_type="prompt_cache_debug",
                    raw_payload=json.dumps({"estimated_prompt_tokens": 499999}),
                    status="debug",
                ))
                db.commit()
            finally:
                db.close()

            llm = FakeCheckpointLLM()
            manager = SchedulerManager(memory_manager=FakeMemoryManager(), llm_client=llm)

            ok, error = await manager._run_context_checkpoint_for_session("context_checkpoint_test", "qq_private_10001")

            self.assertTrue(ok, error)
            self.assertEqual(llm.requests, [])
            db = self.Session()
            try:
                self.assertEqual(db.query(ContextCheckpoint).count(), 0)
            finally:
                db.close()

        asyncio.run(scenario())

    def test_scheduler_creates_group_context_checkpoint_above_threshold(self):
        async def scenario():
            db = self.Session()
            try:
                db.add_all([
                    RawChatLog(
                        event_id="group_msg_1",
                        session_id="qq_group_123456",
                        event_type="message.text",
                        platform="qq_group",
                        input_text="今晚继续聊副本",
                        raw_payload=json.dumps({
                            "raw": {
                                "qq_user_id": "550808201",
                                "onebot_message_id": "99112233",
                            }
                        }, ensure_ascii=False),
                        status="received",
                    ),
                    RawChatLog(
                        event_id="group_bad_1",
                        session_id="qq_group_123456",
                        event_type="message.text",
                        platform="qq_group",
                        input_text="sender_card=平台群名片不该进入 checkpoint",
                        raw_payload=json.dumps({
                            "raw": {
                                "qq_user_id": "550808201",
                                "onebot_message_id": "99112234",
                            }
                        }, ensure_ascii=False),
                        status="received",
                    ),
                    RawChatLog(
                        event_id="group_img_1",
                        session_id="qq_group_123456",
                        event_type="message.image",
                        platform="qq_group",
                        input_text="[图片]",
                        raw_payload=json.dumps({
                            "raw": {
                                "qq_user_id": "550808201",
                                "onebot_message_id": "99112235",
                                "media_refs": [{
                                    "is_sticker": False,
                                    "segment_type": "image",
                                    "summary": "群友发了一张战绩截图",
                                    "onebot_message_id": "99112235",
                                    "file_id": "secret_file",
                                }],
                            }
                        }, ensure_ascii=False),
                        status="received",
                    ),
                    RawChatLog(
                        event_id="group_image_understanding_1",
                        session_id="qq_group_123456",
                        event_type="group_image_understanding",
                        platform="qq_group",
                        raw_payload=json.dumps({
                            "media_key": "qq_group_123456:99112235:0",
                            "message_id": "99112235",
                            "status": "completed",
                            "result": {
                                "status": "completed",
                                "summary": "战绩页面，像是在讨论副本输出。",
                                "reply_target_candidate": True,
                            },
                        }, ensure_ascii=False),
                        status="completed",
                    ),
                    RawChatLog(
                        event_id="group_meme_1",
                        session_id="qq_group_123456",
                        event_type="meme_intake_result",
                        platform="qq_group",
                        raw_payload=json.dumps({
                            "status": "saved",
                            "media_key": "secret_media_key",
                            "save_result": {"status": "saved", "file_stem": "amused_cat_laugh"},
                        }, ensure_ascii=False),
                        status="saved",
                    ),
                    RawChatLog(
                        event_id="group_send_1",
                        session_id="qq_group_123456",
                        event_type="onebot_group_send",
                        platform="qq_group",
                        final_text="可以，等你们开局。",
                        raw_payload=json.dumps({
                            "target_group_id": "123456",
                            "onebot_message_id": "99112236",
                        }, ensure_ascii=False),
                        item_type="text",
                        status="sent",
                    ),
                    RawChatLog(
                        event_id="debug_group_1",
                        session_id="qq_group_123456",
                        event_type="prompt_cache_debug",
                        platform="qq_group",
                        raw_payload=json.dumps({
                            "scope": "group",
                            "estimated_prompt_tokens": 510000,
                            "llm_usage": {"prompt_tokens": 620000},
                        }, ensure_ascii=False),
                        status="debug",
                    ),
                ])
                db.commit()
                latest_raw_event_id = (
                    db.query(RawChatLog.id)
                    .filter(RawChatLog.session_id == "qq_group_123456")
                    .filter(RawChatLog.event_type != "prompt_cache_debug")
                    .order_by(RawChatLog.id.desc())
                    .first()[0]
                )
                prompt_debug_id = (
                    db.query(RawChatLog.id)
                    .filter(RawChatLog.event_type == "prompt_cache_debug")
                    .first()[0]
                )
            finally:
                db.close()

            llm = FakeGroupCheckpointLLM()
            manager = SchedulerManager(memory_manager=FakeMemoryManager(), llm_client=llm)
            manager.set_group_memory_manager(FakeGroupMemoryManager())

            ok, error = await manager._run_group_context_checkpoint_for_session("context_checkpoint_test", "qq_group_123456")

            self.assertTrue(ok, error)
            self.assertEqual(len(llm.requests), 1)
            payload = json.loads(llm.requests[0][1]["content"])
            self.assertEqual(payload["task"], "group_context_checkpoint")
            self.assertEqual(payload["estimated_tokens_before"], 620000)
            payload_text = json.dumps(payload, ensure_ascii=False)
            self.assertIn("今晚继续聊副本", payload_text)
            self.assertIn("战绩页面", payload_text)
            self.assertIn("amused_cat_laugh", payload_text)
            self.assertIn('"qid": "550808201"', payload_text)
            self.assertNotIn("sender_card", payload_text)
            self.assertNotIn("平台群名片不该进入", payload_text)
            self.assertNotIn("99112233", payload_text)
            self.assertNotIn("99112235", payload_text)
            self.assertNotIn("onebot_message_id", payload_text)
            self.assertNotIn("secret_media_key", payload_text)
            self.assertNotIn("secret_file", payload_text)

            db = self.Session()
            try:
                checkpoint = db.query(ContextCheckpoint).one()
                self.assertEqual(checkpoint.session_id, "qq_group_123456")
                self.assertIn("群聊压缩摘要", checkpoint.checkpoint_text)
                self.assertEqual(checkpoint.covered_until_event_id, latest_raw_event_id)
                self.assertEqual(checkpoint.source_prompt_debug_id, prompt_debug_id)
                self.assertEqual(checkpoint.estimated_tokens_before, 510000)
                self.assertEqual(checkpoint.prompt_tokens_before, 620000)
            finally:
                db.close()

        asyncio.run(scenario())

    def test_scheduler_skips_group_context_checkpoint_below_threshold(self):
        async def scenario():
            db = self.Session()
            try:
                db.add_all([
                    RawChatLog(
                        event_id="group_msg_1",
                        session_id="qq_group_123456",
                        event_type="message.text",
                        platform="qq_group",
                        input_text="还没到压缩阈值",
                        raw_payload=json.dumps({"raw": {"qq_user_id": "550808201"}}, ensure_ascii=False),
                    ),
                    RawChatLog(
                        event_id="debug_group_1",
                        session_id="qq_group_123456",
                        event_type="prompt_cache_debug",
                        platform="qq_group",
                        raw_payload=json.dumps({"scope": "group", "estimated_prompt_tokens": 499999}),
                        status="debug",
                    ),
                ])
                db.commit()
            finally:
                db.close()

            llm = FakeGroupCheckpointLLM()
            manager = SchedulerManager(memory_manager=FakeMemoryManager(), llm_client=llm)
            manager.set_group_memory_manager(FakeGroupMemoryManager())

            ok, error = await manager._run_group_context_checkpoint_for_session("context_checkpoint_test", "qq_group_123456")

            self.assertTrue(ok, error)
            self.assertEqual(llm.requests, [])
            db = self.Session()
            try:
                self.assertEqual(db.query(ContextCheckpoint).count(), 0)
            finally:
                db.close()

        asyncio.run(scenario())

    def test_group_context_checkpoint_without_prompt_debug_has_bounded_window_observation(self):
        async def scenario():
            manager = SchedulerManager(memory_manager=FakeMemoryManager(), llm_client=FakeGroupCheckpointLLM())
            manager.set_group_memory_manager(FakeGroupMemoryManager())

            result = await manager._maybe_create_group_context_checkpoint("context_checkpoint_test", "qq_group_123456")

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "skipped_bounded_group_window")
            self.assertEqual(result["detail"], "no_group_prompt_debug")

        asyncio.run(scenario())

    def test_clear_group_conversation_deletes_only_that_group_checkpoint(self):
        async def scenario():
            db = self.Session()
            try:
                db.add_all([
                    ContextCheckpoint(
                        session_id="qq_group_123456",
                        checkpoint_text="群聊压缩摘要：旧群。",
                        covered_until_event_id=1,
                        source_prompt_debug_id=1,
                    ),
                    ContextCheckpoint(
                        session_id="qq_group_999999",
                        checkpoint_text="群聊压缩摘要：其它群。",
                        covered_until_event_id=2,
                        source_prompt_debug_id=2,
                    ),
                    ContextCheckpoint(
                        session_id="qq_private_10001",
                        checkpoint_text="压缩摘要：私聊。",
                        covered_until_event_id=3,
                        source_prompt_debug_id=3,
                    ),
                ])
                db.commit()
            finally:
                db.close()

            originals = {
                "get_db": routes.get_db,
                "group_chat_buffer": routes.group_chat_buffer,
                "group_image_cache": routes.group_image_cache,
                "group_send_limiter": routes.group_send_limiter,
                "group_repetition_detector": routes.group_repetition_detector,
                "group_activity_tracker": routes.group_activity_tracker,
                "group_memory_manager": routes.group_memory_manager,
                "group_reply_scheduler": routes.group_reply_scheduler,
                "_emit_conversation_changed": routes._emit_conversation_changed,
            }

            def fake_get_db():
                db = self.Session()
                try:
                    yield db
                finally:
                    db.close()

            async def fake_emit(*_args, **_kwargs):
                return None

            try:
                routes.get_db = fake_get_db
                routes.group_chat_buffer = FakeAsyncClearable()
                routes.group_image_cache = FakeClearable()
                routes.group_send_limiter = FakeClearable()
                routes.group_repetition_detector = FakeClearable()
                routes.group_activity_tracker = FakeClearable()
                routes.group_memory_manager = FakeGroupMemoryManager()
                routes.group_reply_scheduler = FakeAsyncClearable()
                routes._emit_conversation_changed = fake_emit

                result = await routes.clear_conversation(session_id="qq_group_123456", all_sessions=False)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            self.assertEqual(result["status"], "ok")
            db = self.Session()
            try:
                remaining = {
                    row.session_id
                    for row in db.query(ContextCheckpoint).order_by(ContextCheckpoint.session_id).all()
                }
                self.assertEqual(remaining, {"qq_group_999999", "qq_private_10001"})
            finally:
                db.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
