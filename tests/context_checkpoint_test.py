import asyncio
import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.scheduler.jobs as scheduler_jobs
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


if __name__ == "__main__":
    unittest.main()
