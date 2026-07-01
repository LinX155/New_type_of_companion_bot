import asyncio
import json
import tempfile
import unittest
from datetime import datetime

from app.core.group_memory import GroupMemoryManager
from app.llm.prompts import (
    build_group_forget_command_messages,
    build_group_mem_command_messages,
    build_group_memory_analysis_messages,
    build_group_midnight_cleanup_messages,
)
from app.scheduler.jobs import SchedulerManager


class FakeGroupMemoryLLM:
    api_key = "test-key"

    def __init__(self, response, delay: float = 0.0):
        self.response = response
        self.delay = delay
        self.messages = None
        self.calls = []

    async def chat_completion(self, messages, temperature=0.0, **_kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.messages = messages
        self.calls.append(messages)
        if isinstance(self.response, list):
            response = self.response.pop(0)
        else:
            response = self.response
        self.temperature = temperature
        return json.dumps(response, ensure_ascii=False)


class GroupMemoryManagerTest(unittest.TestCase):
    def test_group_memory_prompts_exist_and_forbid_private_or_platform_fields(self):
        prompts = [
            build_group_memory_analysis_messages(
                date_str="2026-07-01",
                transcript=[],
                today_group_memory_md="# 群聊日记忆\n\n## 今日群聊大事\n\n## 群友身份候选\n\n## 共同话题与梗\n\n## q号相关近期状态\n",
                current_group_memory_md="# 群聊记忆\n\n## 群友身份\n\n## 共同记忆\n\n## 个人相关记忆\n",
            ),
            build_group_midnight_cleanup_messages(
                date_str="2026-07-01",
                current_group_memory_md="# 群聊记忆\n\n## 群友身份\n\n## 共同记忆\n\n## 个人相关记忆\n",
                day_memory_md="# 群聊日记忆\n\n## 今日群聊大事\n\n## 群友身份候选\n\n## 共同话题与梗\n\n## q号相关近期状态\n",
            ),
            build_group_mem_command_messages(
                content="我叫小夏",
                current_group_memory_md="# 群聊记忆\n\n## 群友身份\n\n## 共同记忆\n\n## 个人相关记忆\n",
                sender_qid="550808201",
                today_date="2026-07-01",
            ),
            build_group_forget_command_messages(
                query="我的昵称",
                current_group_memory_md="# 群聊记忆\n\n## 群友身份\n\n## 共同记忆\n\n## 个人相关记忆\n",
                sender_qid="550808201",
            ),
        ]

        for messages in prompts:
            system = messages[0]["content"]
            self.assertIn("平台群名片", system)
            self.assertIn("sender_card", system)
            self.assertIn("sender_nickname", system)
            self.assertIn("非当前群聊会话的 MEMORY_CORE.md", system)
            self.assertIn("非当前群聊会话的 MEMORY_CORE.md、TODAY_MEMORY.md、TOMORROW_TOPICS.md", system)
            self.assertIn("[[quote]]", system)
            self.assertIn("tool tag", system)
            self.assertIn("不要读取 raw chat log", system)
            self.assertIn("CoT", system)
            self.assertIn("只维护当前群自己的 dm、GROUP_MEMORY.md 和 group_tomorrow_topics_md", system)
            self.assertIn("active_message_setting", system)
            self.assertIn("群友原话 > 群友文字 + 图片理解 > 单独图片理解", system)
            self.assertIn("对每条候选先判断生命周期", system)
            self.assertIn("写入个人相关记忆时必须消除说话人歧义", system)

        memory_system = prompts[0][0]["content"]
        self.assertIn("assistant 可见回复只用于理解对话承接", memory_system)
        self.assertIn("不写流水账", memory_system)
        self.assertIn("完整的群聊 TOMORROW_TOPICS.md", memory_system)
        self.assertIn("不要把提醒事项正文写入 dm 或 group_tomorrow_topics_md", memory_system)

        cleanup_system = prompts[1][0]["content"]
        self.assertIn("long 只能来自明确身份", cleanup_system)
        self.assertIn("不要把当天情绪", cleanup_system)
        self.assertIn("GROUP_MEMORY.md 中已有过期近期状态", cleanup_system)

        mem_system = prompts[2][0]["content"]
        self.assertIn("content_to_remember 是 sender_qid", mem_system)
        self.assertIn("其中“我/我的/本人/俺”都指 sender_qid", mem_system)
        self.assertIn("不要原样保留“我……”或“你……”", mem_system)

        forget_system = prompts[3][0]["content"]
        self.assertIn("按语义匹配要删除的条目", forget_system)
        self.assertIn("权限不清或范围过大时宁可不删", forget_system)
        self.assertIn("不能因为一句泛化请求清空共同记忆", forget_system)

    def test_mem_self_identity_updates_confirmed_qid_nickname_and_personal_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            success, response, payload = manager.apply_mem_command(
                "/mem 我是小夏",
                session_id="qq_group_123456",
                sender_qid="550808201",
                timestamp=datetime(2026, 6, 30, 23, 30),
            )

            self.assertTrue(success)
            self.assertEqual(response, "已把你的群聊昵称记住了。")
            self.assertEqual(payload["status"], "identity_updated")
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {"550808201": "小夏"})
            content = manager.read("qq_group_123456")
            self.assertIn("- 550808201: 小夏", content)
            self.assertIn("<550808201><2026-06-30T23:30:00><群聊/mem本人><昵称=小夏>", content)

    def test_mem_rejects_other_person_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            success, response, payload = manager.apply_mem_command(
                "/mem 1057552839 是小明",
                session_id="qq_group_123456",
                sender_qid="550808201",
            )

            self.assertFalse(success)
            self.assertEqual(payload["reason"], "other_person")
            self.assertIn("不能替别人写记忆", response)
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {})

    def test_mem_common_memory_uses_common_section_not_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            success, response, payload = manager.apply_mem_command(
                "/mem 群里最近都在玩赛博猫猫梗",
                session_id="qq_group_123456",
                sender_qid="550808201",
                timestamp=datetime(2026, 6, 30, 23, 31),
            )

            self.assertTrue(success)
            self.assertEqual(response, "已写入群聊共同记忆。")
            self.assertEqual(payload["status"], "common_memory_added")
            context = manager.prompt_context("qq_group_123456", qids=["550808201"])
            self.assertEqual(context["qid_to_nickname"], {})
            self.assertIn("群里最近都在玩赛博猫猫梗", "\n".join(context["common_memory"]))

    def test_forget_only_removes_sender_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)
            manager.apply_mem_command(
                "/mem 我叫小夏",
                session_id="qq_group_123456",
                sender_qid="550808201",
                timestamp=datetime(2026, 6, 30, 23, 30),
            )
            manager.apply_mem_command(
                "/mem 我叫小明",
                session_id="qq_group_123456",
                sender_qid="1057552839",
                timestamp=datetime(2026, 6, 30, 23, 31),
            )

            success, response, payload = manager.apply_forget_command(
                "/forget 我的昵称",
                session_id="qq_group_123456",
                sender_qid="550808201",
            )

            self.assertTrue(success)
            self.assertEqual(payload["status"], "identity_removed")
            self.assertIn("群聊身份记忆", response)
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {"1057552839": "小明"})

    def test_prompt_context_filters_personal_memory_to_window_qids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)
            manager.apply_mem_command("/mem 我叫小夏", session_id="qq_group_123456", sender_qid="550808201")
            manager.apply_mem_command("/mem 我叫小明", session_id="qq_group_123456", sender_qid="1057552839")

            context = manager.prompt_context("qq_group_123456", qids=["550808201"])

            self.assertEqual(context["qid_to_nickname"], {"550808201": "小夏"})
            self.assertIn("昵称=小夏", "\n".join(context["personal_memory"]))
            self.assertNotIn("昵称=小明", "\n".join(context["personal_memory"]))

    def test_observe_group_text_returns_candidate_without_core_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            payload = manager.observe_group_text(
                "我叫小夏",
                session_id="qq_group_123456",
                sender_qid="550808201",
                timestamp=datetime(2026, 6, 30, 23, 32),
            )

            self.assertEqual(payload["status"], "identity_candidate")
            self.assertEqual(payload["nickname"], "小夏")
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {})
            content = manager.read("qq_group_123456")
            self.assertNotIn("昵称=小夏", content)

    def test_observe_group_text_rejects_generic_self_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = GroupMemoryManager(tmpdir)

            payload = manager.observe_group_text(
                "我是一个路过的人",
                session_id="qq_group_123456",
                sender_qid="550808201",
            )

            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["reason"], "no_self_identity")
            self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {})

    def test_group_memory_thread_updates_daily_dm_not_group_core(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                manager = GroupMemoryManager(tmpdir)
                llm = FakeGroupMemoryLLM({
                    "today_group_memory_md": """# 群聊日记忆

## 今日群聊大事
- 群里在聊赛博猫猫梗

## 群友身份候选
- 550808201 明确自称小夏

## 共同话题与梗
- 赛博猫猫梗当天反复出现

## q号相关近期状态
- 550808201 当天参与赛博猫猫梗
""",
                    "group_tomorrow_topics_md": """# 明日话题

## 未闭合话题
- [pending] [2026-06-30]: 群里还可以自然接赛博猫猫梗

## 昨日记忆

## 生活感消息备选
""",
                    "note": "updated",
                })

                result = await manager.analyze_window_via_llm(
                    [
                        {
                            "timestamp": "2026-06-30T23:40:00",
                            "event_type": "text",
                            "sender_qid": "550808201",
                            "text": "我叫小夏，赛博猫猫这个梗笑死",
                        }
                    ],
                    session_id="qq_group_123456",
                    llm_client=llm,
                    current_time=datetime(2026, 6, 30, 23, 40),
                )

                self.assertEqual(result["status"], "updated")
                self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {})
                core = manager.read("qq_group_123456")
                self.assertNotIn("赛博猫猫", core)
                dm = manager.read_dm_file("qq_group_123456", "2026-06-30")
                self.assertIn("赛博猫猫梗当天反复出现", dm)
                self.assertIn("550808201 明确自称小夏", dm)
                topics = manager.read_tomorrow_topics("qq_group_123456")
                self.assertIn("群里还可以自然接赛博猫猫梗", topics)
                self.assertTrue(result["tomorrow_topics_updated"])
                self.assertIn("群聊日间记忆线程", llm.messages[0]["content"])
                self.assertIn("不能直接修改 GROUP_MEMORY.md", llm.messages[0]["content"])
                self.assertIn("current_group_tomorrow_topics_md", llm.messages[1]["content"])
                self.assertNotIn("MEMORY_CORE.md 的合适分区", llm.messages[0]["content"])

        asyncio.run(scenario())

    def test_group_memory_thread_rejects_platform_nickname_output(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                manager = GroupMemoryManager(tmpdir)
                llm = FakeGroupMemoryLLM({
                    "today_group_memory_md": """# 群聊日记忆

## 今日群聊大事
- sender_card=群名片

## 群友身份候选
- 999999999: 平台昵称坏例子

## 共同话题与梗
- 群里最近反复玩赛博猫猫梗

## q号相关近期状态
- 550808201 自称小夏
"""
                })

                result = await manager.analyze_window_via_llm(
                    [
                        {
                            "timestamp": "2026-06-30T23:40:00",
                            "event_type": "text",
                            "sender_qid": "550808201",
                            "text": "我叫小夏",
                        }
                    ],
                    session_id="qq_group_123456",
                    llm_client=llm,
                    current_time=datetime(2026, 6, 30, 23, 40),
                )

                self.assertEqual(result["status"], "skipped")
                self.assertIn("blocked_token", result["reason"])
                dm = manager.read_dm_file("qq_group_123456", "2026-06-30")
                self.assertNotIn("sender_card", dm)
                self.assertNotIn("平台昵称坏例子", dm)

        asyncio.run(scenario())

    def test_group_midnight_cleanup_promotes_daily_dm_to_group_core(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                manager = GroupMemoryManager(tmpdir)
                manager.write_dm_file("qq_group_123456", "2026-06-30", """# 群聊日记忆

## 今日群聊大事
- 群里在聊赛博猫猫梗

## 群友身份候选
- 550808201 明确自称小夏

## 共同话题与梗
- 赛博猫猫梗当天反复出现

## q号相关近期状态
- 550808201 当天参与赛博猫猫梗
""")
                llm = FakeGroupMemoryLLM({
                    "group_memory_md": """# 群聊记忆

## 群友身份
- 550808201: 小夏

## 共同记忆
- <2026-06-30><群聊凌晨整理><群里有赛博猫猫梗>

## 个人相关记忆
- <550808201><2026-06-30><群聊凌晨整理><明确自称小夏>
""",
                    "note": "promoted",
                })

                result = await manager.cleanup_via_llm(
                    session_id="qq_group_123456",
                    llm_client=llm,
                    date_str="2026-06-30",
                )

                self.assertEqual(result["status"], "updated")
                self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {"550808201": "小夏"})
                core = manager.read("qq_group_123456")
                self.assertIn("群里有赛博猫猫梗", core)
                self.assertIn("群聊凌晨整理线程", llm.messages[0]["content"])
                self.assertIn("GROUP_MEMORY.md", llm.messages[0]["content"])

        asyncio.run(scenario())

    def test_mem_command_via_llm_updates_sender_scope_and_preserves_others(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                manager = GroupMemoryManager(tmpdir)
                manager.apply_mem_command("/mem 我叫小明", session_id="qq_group_123456", sender_qid="1057552839")
                llm = FakeGroupMemoryLLM({
                    "group_memory_md": """# 群聊记忆

## 群友身份
- 550808201: 小夏

## 共同记忆
- <2026-07-01><群聊/mem 550808201><群里喜欢赛博猫猫梗>

## 个人相关记忆
- <550808201><2026-07-01><群聊/mem本人><昵称=小夏>
""",
                    "note": "mem",
                })

                success, response, payload = await manager.apply_mem_command_via_llm(
                    "/mem 我叫小夏",
                    session_id="qq_group_123456",
                    sender_qid="550808201",
                    llm_client=llm,
                    timestamp=datetime(2026, 7, 1, 1, 2),
                )

                self.assertTrue(success)
                self.assertIn("群聊昵称", response)
                self.assertEqual(payload["status"], "llm_updated")
                names = manager.qid_to_nickname("qq_group_123456")
                self.assertEqual(names["550808201"], "小夏")
                self.assertEqual(names["1057552839"], "小明")
                self.assertIn("群聊 /mem 记忆写入线程", llm.messages[0]["content"])

        asyncio.run(scenario())

    def test_forget_command_via_llm_cannot_delete_other_qid_memory(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                manager = GroupMemoryManager(tmpdir)
                manager.apply_mem_command("/mem 我叫小明", session_id="qq_group_123456", sender_qid="1057552839")
                manager.apply_mem_command("/mem 我叫小夏", session_id="qq_group_123456", sender_qid="550808201")
                llm = FakeGroupMemoryLLM({
                    "group_memory_md": """# 群聊记忆

## 群友身份

## 共同记忆

## 个人相关记忆
""",
                    "removed": "全部删除",
                })

                success, response, payload = await manager.apply_forget_command_via_llm(
                    "/forget 我的昵称",
                    session_id="qq_group_123456",
                    sender_qid="550808201",
                    llm_client=llm,
                )

                self.assertTrue(success)
                self.assertIn(payload["status"], {"llm_removed", "not_found"})
                names = manager.qid_to_nickname("qq_group_123456")
                self.assertEqual(names, {"1057552839": "小明"})
                self.assertIn("群聊 /forget 记忆删除线程", llm.messages[0]["content"])

        asyncio.run(scenario())

    def test_same_group_mem_commands_are_serialized(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                manager = GroupMemoryManager(tmpdir)
                llm = FakeGroupMemoryLLM([
                    {
                        "group_memory_md": """# 群聊记忆

## 群友身份
- 550808201: 小夏

## 共同记忆

## 个人相关记忆
- <550808201><2026-07-01><群聊/mem本人><昵称=小夏>
"""
                    },
                    {
                        "group_memory_md": """# 群聊记忆

## 群友身份
- 550808201: 小夏
- 1057552839: 小明

## 共同记忆

## 个人相关记忆
- <550808201><2026-07-01><群聊/mem本人><昵称=小夏>
- <1057552839><2026-07-01><群聊/mem本人><昵称=小明>
"""
                    },
                ], delay=0.01)

                await asyncio.gather(
                    manager.apply_mem_command_via_llm("/mem 我叫小夏", session_id="qq_group_123456", sender_qid="550808201", llm_client=llm),
                    manager.apply_mem_command_via_llm("/mem 我叫小明", session_id="qq_group_123456", sender_qid="1057552839", llm_client=llm),
                )

                self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {"550808201": "小夏", "1057552839": "小明"})
                self.assertEqual(len(llm.calls), 2)

        asyncio.run(scenario())

    def test_scheduler_runs_private_and_group_memory_scopes_separately(self):
        class FakePrivateMemory:
            def __init__(self):
                self.today_memory = "# 每日记忆\n"
                self.tomorrow_topics = "# 明日话题\n\n## 未闭合话题\n\n## 昨日记忆\n\n## 生活感消息备选\n"

            def read_today_memory(self):
                return self.today_memory

            def read_tomorrow_topics(self):
                return self.tomorrow_topics

            def write_today_memory(self, value):
                self.today_memory = value
                return True

            def write_tomorrow_topics(self, value):
                self.tomorrow_topics = value
                return True

        class FakePrivateMemoryManager:
            def __init__(self):
                self.memory = FakePrivateMemory()

            def for_session(self, session_id):
                if session_id.startswith("qq_group_"):
                    raise AssertionError("private memory manager must not receive group sessions")
                return self.memory

        class FakeLLM:
            api_key = "test-key"

            async def chat_completion(self, **_kwargs):
                return json.dumps({
                    "today_memory_md": "# 每日记忆\n- 私聊记忆\n",
                    "tomorrow_topics_md": "# 明日话题\n\n## 未闭合话题\n\n## 昨日记忆\n\n## 生活感消息备选\n",
                    "active_message_setting": {"type": "none", "time": None},
                }, ensure_ascii=False)

            def get_last_call_debug(self):
                return {"llm_runtime_id": "test-runtime", "client_scope": "background_batch"}

        class FakeGroupMemory:
            def __init__(self):
                self.calls = []

            async def analyze_daily_memory_via_llm(self, transcript, **kwargs):
                self.calls.append(("analysis", kwargs["session_id"], transcript))
                return {"status": "updated", "path": "dm/2026-07-01.md"}

        private_manager = FakePrivateMemoryManager()
        group_memory = FakeGroupMemory()
        scheduler = SchedulerManager(memory_manager=private_manager, llm_client=FakeLLM())
        scheduler.set_session_ids_provider(lambda: ["qq_private_1"])
        scheduler.set_group_session_ids_provider(lambda: ["qq_group_123456"])
        scheduler.set_group_memory_manager(group_memory)
        scheduler._start_job = lambda *_args, **_kwargs: 1
        scheduler._finish_job = lambda *_args, **_kwargs: None
        scheduler._load_transcript_for_date = lambda *_args, **_kwargs: [{"role": "user", "text": "私聊"}]
        scheduler._load_group_transcript_for_date = lambda *_args, **_kwargs: [{"role": "user", "qid": "550808201", "text": "群聊"}]

        result = asyncio.run(scheduler._run_memory_analysis())

        self.assertEqual(result["status"], "completed")
        self.assertEqual(private_manager.memory.today_memory, "# 每日记忆\n- 私聊记忆\n")
        self.assertEqual(group_memory.calls, [("analysis", "qq_group_123456", [{"role": "user", "qid": "550808201", "text": "群聊"}])])

    def test_scheduler_group_failure_does_not_block_other_sessions(self):
        class FakePrivateMemoryManager:
            def for_session(self, _session_id):
                raise AssertionError("private sessions are not part of this test")

        class FakeLLM:
            api_key = "test-key"

            def get_last_call_debug(self):
                return {"llm_runtime_id": "test-runtime", "client_scope": "background_batch"}

        class FakeGroupMemory:
            def __init__(self):
                self.calls = []

            async def analyze_daily_memory_via_llm(self, transcript, **kwargs):
                self.calls.append(kwargs["session_id"])
                if kwargs["session_id"] == "qq_group_bad":
                    raise RuntimeError("bad group")
                return {"status": "updated"}

        group_memory = FakeGroupMemory()
        scheduler = SchedulerManager(memory_manager=FakePrivateMemoryManager(), llm_client=FakeLLM())
        scheduler.set_session_ids_provider(lambda: [])
        scheduler.set_group_session_ids_provider(lambda: ["qq_group_bad", "qq_group_ok"])
        scheduler.set_group_memory_manager(group_memory)
        scheduler._session_ids = lambda: []
        scheduler._start_job = lambda *_args, **_kwargs: 1
        scheduler._finish_job = lambda *_args, **_kwargs: None
        scheduler._load_group_transcript_for_date = lambda *_args, **_kwargs: [{"role": "user", "qid": "550808201", "text": "群聊"}]
        scheduler._record_group_memory_audit = lambda *_args, **_kwargs: None

        result = asyncio.run(scheduler._run_memory_analysis())

        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual(group_memory.calls, ["qq_group_bad", "qq_group_ok"])
        self.assertEqual(result["failures"][0]["session_id"], "qq_group_bad")


if __name__ == "__main__":
    unittest.main()
