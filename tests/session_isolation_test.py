import tempfile
import unittest
import asyncio
import os
from datetime import datetime
from pathlib import Path

from app.adapters.onebot11.events import parse_onebot_event
from app.core.events import ChatEvent, EventType
from app.core.media_jobs import MediaJobQueue
from app.core.runtime import SessionRuntimeManager
from app.core.sessions import SessionRegistry, qq_private_session_id
from app.core.snapshots import SnapshotManager
from app.core.state import ChatStatus
from app.llm.client import LLMClient
from app.memory.files import MemoryFileManager
from app.memes.catalog import MemeCatalog


class SessionIsolationTest(unittest.TestCase):
    def test_snapshot_manager_keeps_session_id(self):
        manager = SnapshotManager(session_id="qq_private_10001")

        snapshot = manager.create_snapshot(
            events=[{"event_id": "evt1", "text": "在吗"}],
            buffer_version=1,
            status="HOT",
            msg_index=3,
        )

        self.assertEqual(snapshot.session_id, "qq_private_10001")

    def test_onebot_private_message_maps_to_qq_private_session(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "private",
            "user_id": 550808201,
            "message_id": 123,
            "time": 1782031609,
            "message": [{"type": "text", "data": {"text": "醒了吗"}}],
        })

        self.assertIsNotNone(event)
        self.assertEqual(event.session_id, qq_private_session_id("550808201"))

    def test_memory_files_are_isolated_by_session(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = MemoryFileManager(tmp_dir)
            session_a = root.for_session("qq_private_10001")
            session_b = root.for_session("qq_private_10002")

            session_a.write_memory_core("# 永久核心记忆\n\n## 用户明确相处偏好\n- A only\n\n## 重要事实\n\n## 用户交际圈\n\n## 相处习惯\n\n## 临时近期状态\n")

            self.assertIn("A only", session_a.read_memory_core())
            self.assertNotIn("A only", session_b.read_memory_core())
            self.assertNotEqual(session_a.memory_core_path, session_b.memory_core_path)
            self.assertIn("memory", session_a.memory_core_path)
            self.assertIn("sessions", session_a.memory_core_path)

    def test_media_completed_payloads_are_filtered_by_session(self):
        queue = MediaJobQueue(llm_client=None, media_downloader=None)
        queue._remember_prompt_payloads([
            {
                "internal_event_harness": "image_understanding_result",
                "session_id": "qq_private_10001",
                "status": "completed",
                "raw_output": "hidden",
                "result": {"summary": "A image"},
            },
            {
                "internal_event_harness": "image_understanding_result",
                "session_id": "qq_private_10002",
                "status": "completed",
                "raw_output": "hidden",
                "result": {"summary": "B image"},
            },
        ])

        self.assertEqual(
            queue.get_completed_payloads_for_prompt("qq_private_10001")[0]["result"]["summary"],
            "A image",
        )
        self.assertEqual(
            queue.get_completed_payloads_for_prompt("qq_private_10002")[0]["result"]["summary"],
            "B image",
        )

    def test_runtime_manager_keeps_two_qq_sessions_independent(self):
        async def scenario():
            async def noop_on_decision(_ctx):
                return None

            with tempfile.TemporaryDirectory() as tmp_dir:
                manager = SessionRuntimeManager(
                    root_dir=tmp_dir,
                    base_llm_client=LLMClient(api_key="", model="test-model"),
                    base_memory_manager=MemoryFileManager(tmp_dir),
                    meme_catalog=MemeCatalog(str(Path(tmp_dir) / "memes")),
                    media_job_queue=None,
                    on_decision=noop_on_decision,
                    hot_duration_minutes=30,
                )

                session_a = qq_private_session_id("10001")
                session_b = qq_private_session_id("10002")
                runtime_a = manager.get(session_a)
                runtime_b = manager.get(session_b)

                await runtime_a.gate.handle_event(ChatEvent(
                    event_id="a1",
                    session_id=session_a,
                    platform="qq",
                    user_id="10001",
                    event_type=EventType.TEXT,
                    text="A",
                    timestamp=datetime.now(),
                ))
                await runtime_b.gate.handle_event(ChatEvent(
                    event_id="b1",
                    session_id=session_b,
                    platform="qq",
                    user_id="10002",
                    event_type=EventType.TEXT,
                    text="B",
                    timestamp=datetime.now(),
                ))
                await asyncio.sleep(0)

                runtime_a.gate.state.status = ChatStatus.HOT

                self.assertEqual(runtime_a.gate.state.msg_index_today, 1)
                self.assertEqual(runtime_b.gate.state.msg_index_today, 1)
                self.assertEqual(runtime_a.gate.snapshot_manager.session_id, session_a)
                self.assertEqual(runtime_b.gate.snapshot_manager.session_id, session_b)
                self.assertEqual(runtime_b.gate.state.status, ChatStatus.COLD)
                self.assertNotEqual(runtime_a.graph.llm.cache_session_id, runtime_b.graph.llm.cache_session_id)

        asyncio.run(scenario())

    def test_provider_user_id_is_stable_and_private_per_session(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry = SessionRegistry(tmp_dir)
            session_id = qq_private_session_id("10001")

            first = registry.provider_user_id(session_id)
            second = SessionRegistry(tmp_dir).provider_user_id(session_id)
            other = registry.provider_user_id(qq_private_session_id("10002"))

            self.assertEqual(first, second)
            self.assertNotEqual(first, other)
            self.assertRegex(first, r"^u_[0-9a-f]{32}$")
            self.assertNotIn("10001", first)

    def test_provider_user_id_rotates_after_session_delete(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            registry = SessionRegistry(tmp_dir)
            session_id = qq_private_session_id("10001")
            first = registry.provider_user_id(session_id)

            self.assertTrue(registry.delete_private_files(session_id))
            second = registry.provider_user_id(session_id)

            self.assertNotEqual(first, second)

    def test_runtime_manager_passes_provider_user_id_to_session_llm(self):
        async def scenario():
            async def noop_on_decision(_ctx):
                return None

            with tempfile.TemporaryDirectory() as tmp_dir:
                manager = SessionRuntimeManager(
                    root_dir=tmp_dir,
                    base_llm_client=LLMClient(
                        api_key="",
                        base_url="https://api.deepseek.com",
                        model="deepseek-chat",
                    ),
                    base_memory_manager=MemoryFileManager(tmp_dir),
                    meme_catalog=MemeCatalog(str(Path(tmp_dir) / "memes")),
                    media_job_queue=None,
                    on_decision=noop_on_decision,
                    hot_duration_minutes=30,
                )

                runtime = manager.get(qq_private_session_id("10001"))

                self.assertRegex(runtime.graph.llm.provider_user_id, r"^u_[0-9a-f]{32}$")
                self.assertTrue(runtime.graph.llm.get_cache_debug()["provider_user_id_sent"])

        asyncio.run(scenario())

    def test_runtime_manager_internal_llm_omits_provider_identity_and_cache_affinity(self):
        async def scenario():
            async def noop_on_decision(_ctx):
                return None

            old_env_user_id = os.environ.get("LLM_PROVIDER_USER_ID")
            os.environ["LLM_PROVIDER_USER_ID"] = "u_ffffffffffffffffffffffffffffffff"
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    manager = SessionRuntimeManager(
                        root_dir=tmp_dir,
                        base_llm_client=LLMClient(
                            api_key="test-key",
                            base_url="https://api.deepseek.com",
                            model="deepseek-chat",
                            provider_user_id="u_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                        ),
                        base_memory_manager=MemoryFileManager(tmp_dir),
                        meme_catalog=MemeCatalog(str(Path(tmp_dir) / "memes")),
                        media_job_queue=None,
                        on_decision=noop_on_decision,
                        hot_duration_minutes=30,
                    )

                    session_id = qq_private_session_id("10001")
                    main_llm = manager.get(session_id).graph.llm
                    internal_llm = manager.make_internal_llm_for_session(session_id)
                    repeated_internal_llm = manager.make_internal_llm_for_session(session_id)
                    background_llm = manager.make_background_llm_for_session(session_id)
                    repeated_background_llm = manager.make_background_llm_for_session(session_id)

                    self.assertIs(internal_llm, repeated_internal_llm)
                    self.assertIs(background_llm, repeated_background_llm)
                    self.assertRegex(main_llm.provider_user_id, r"^u_[0-9a-f]{32}$")
                    main_debug = main_llm.get_cache_debug()
                    self.assertEqual(main_debug["client_scope"], "chat_session")
                    self.assertTrue(main_debug["cache_affinity_enabled"])
                    self.assertTrue(main_debug["provider_user_id_sent"])

                    self.assertIsNone(internal_llm.provider_user_id)
                    internal_debug = internal_llm.get_cache_debug()
                    self.assertEqual(internal_debug["client_scope"], "internal_session")
                    self.assertFalse(internal_debug["cache_affinity_enabled"])
                    self.assertFalse(internal_debug["provider_user_id_configured"])
                    self.assertFalse(internal_debug["provider_user_id_sent"])
                    self.assertIsNone(internal_debug["cache_session_id"])
                    self.assertIsNone(internal_debug["prompt_cache_key"])

                    background_debug = background_llm.get_cache_debug()
                    self.assertEqual(background_debug["client_scope"], "background_batch")
                    self.assertFalse(background_debug["cache_affinity_enabled"])
                    self.assertFalse(background_debug["provider_user_id_configured"])
                    self.assertFalse(background_debug["provider_user_id_sent"])
                    self.assertIsNone(background_debug["cache_session_id"])
                    self.assertIsNone(background_debug["prompt_cache_key"])

                    kwargs = internal_llm._build_kwargs(
                        messages=[{"role": "user", "content": "ping"}],
                        temperature=0.2,
                        max_tokens=None,
                        stream=False,
                    )
                    self.assertNotIn("extra_headers", kwargs)
                    self.assertNotIn("prompt_cache_key", kwargs)
                    self.assertNotIn("user_id", kwargs["extra_body"])
            finally:
                if old_env_user_id is None:
                    os.environ.pop("LLM_PROVIDER_USER_ID", None)
                else:
                    os.environ["LLM_PROVIDER_USER_ID"] = old_env_user_id

        asyncio.run(scenario())

    def test_runtime_manager_keeps_mimo_web_search_chat_scope_only(self):
        async def scenario():
            async def noop_on_decision(_ctx):
                return None

            with tempfile.TemporaryDirectory() as tmp_dir:
                manager = SessionRuntimeManager(
                    root_dir=tmp_dir,
                    base_llm_client=LLMClient(
                        api_key="test-key",
                        base_url="https://custom-compatible-gateway.example/v1",
                        model="mimo-v2.5",
                        thinking_enabled=False,
                        mimo_web_search_mode="adaptive",
                    ),
                    base_memory_manager=MemoryFileManager(tmp_dir),
                    meme_catalog=MemeCatalog(str(Path(tmp_dir) / "memes")),
                    media_job_queue=None,
                    on_decision=noop_on_decision,
                    hot_duration_minutes=30,
                )

                session_id = qq_private_session_id("10001")
                main_llm = manager.get(session_id).graph.llm
                internal_llm = manager.make_internal_llm_for_session(session_id)
                background_llm = manager.make_background_llm_for_session(session_id)

                main_kwargs = main_llm._build_kwargs(
                    messages=[{"role": "user", "content": "今天外面天气怎么样"}],
                    temperature=1.0,
                    max_tokens=None,
                    stream=False,
                )
                internal_kwargs = internal_llm._build_kwargs(
                    messages=[{"role": "user", "content": "整理记忆"}],
                    temperature=0.2,
                    max_tokens=None,
                    stream=False,
                )
                background_kwargs = background_llm._build_kwargs(
                    messages=[{"role": "user", "content": "批处理"}],
                    temperature=0.2,
                    max_tokens=None,
                    stream=False,
                )

                self.assertEqual(main_kwargs["extra_body"]["tools"][0]["type"], "web_search")
                self.assertTrue(main_llm.get_cache_debug()["mimo_web_search_enabled"])
                self.assertNotIn("tools", internal_kwargs["extra_body"])
                self.assertEqual(internal_llm.get_cache_debug()["mimo_web_search_disabled_reason"], "scope_not_chat_session")
                self.assertNotIn("tools", background_kwargs["extra_body"])
                self.assertEqual(background_llm.get_cache_debug()["mimo_web_search_disabled_reason"], "scope_not_chat_session")

        asyncio.run(scenario())

    def test_runtime_manager_rebuilds_runtime_clients_after_llm_config_change(self):
        async def scenario():
            async def noop_on_decision(_ctx):
                return None

            with tempfile.TemporaryDirectory() as tmp_dir:
                manager = SessionRuntimeManager(
                    root_dir=tmp_dir,
                    base_llm_client=LLMClient(
                        api_key="test-key",
                        base_url="https://api.deepseek.com",
                        model="deepseek-chat",
                    ),
                    base_memory_manager=MemoryFileManager(tmp_dir),
                    meme_catalog=MemeCatalog(str(Path(tmp_dir) / "memes")),
                    media_job_queue=None,
                    on_decision=noop_on_decision,
                    hot_duration_minutes=30,
                )

                session_id = qq_private_session_id("10001")
                runtime = manager.get(session_id)
                old_main_llm = runtime.graph.llm
                old_internal_llm = manager.make_internal_llm_for_session(session_id)
                old_background_llm = manager.make_background_llm_for_session(session_id)
                old_runtime_id = old_main_llm.get_cache_debug()["llm_runtime_id"]

                manager.base_llm_client.update_config(model="deepseek-chat-v2")
                manager.reconfigure_llm_clients(reason="test_llm_config_changed")

                new_main_llm = runtime.graph.llm
                new_internal_llm = manager.make_internal_llm_for_session(session_id)
                new_background_llm = manager.make_background_llm_for_session(session_id)

                self.assertIsNot(new_main_llm, old_main_llm)
                self.assertIsNot(new_internal_llm, old_internal_llm)
                self.assertIsNot(new_background_llm, old_background_llm)
                self.assertEqual(new_main_llm.model, "deepseek-chat-v2")
                self.assertNotEqual(new_main_llm.get_cache_debug()["llm_runtime_id"], old_runtime_id)
                self.assertEqual(runtime.graph._prefix_rebuild_reason, "test_llm_config_changed")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
