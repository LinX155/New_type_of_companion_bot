import asyncio
import unittest
from types import SimpleNamespace

from app.core.decisions import SendItem, SendItemType
from app.core.graph import CompanionGraph
from app.core.state import ChatStatus, ColdStartMeta, ConversationSnapshot


class FakeLLM:
    def get_last_usage(self):
        return None


class FakeMemory:
    def read_soul(self):
        return "# SOUL\n稳定角色"

    def read_memory_core(self):
        return "# MEMORY_CORE\n- [测试]: 用户喜欢短句"

    def read_today_memory(self):
        return "# 每日记忆\n"

    def read_tomorrow_topics(self):
        return "# 明日话题\n"


class FakeMemeCatalog:
    def get_images_in_category(self, category):
        return []


class FakeMemoryWithTomorrow(FakeMemory):
    def read_tomorrow_topics(self):
        return """# 明日话题

## 未闭合话题
- [pending] 还想聊一下考试
- [used] 已经发过的问候
- [pending] [2020-01-01]: 已经过期的话题

## 昨日记忆
- [blocked] 不适合主动提的事

## 生活感消息备选
- [pending] 楼下桂花开了
"""


class PromptAppendOnlyTest(unittest.TestCase):
    def test_hot_request_appends_to_cold_request(self):
        async def scenario():
            graph = CompanionGraph(FakeLLM(), FakeMemory(), FakeMemeCatalog())
            gate = self._gate(msg_index=1, age="unknown")
            cold_ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=1,
                    buffer_version=1,
                    status=ChatStatus.COLD,
                    events=[
                        {
                            "event_id": "e1",
                            "event_type": "message.text",
                            "text": "你在吗",
                        }
                    ],
                    cold_start_meta=ColdStartMeta(
                        timestamp="10:00",
                        last_user_message_age="unknown",
                        msg_index=1,
                        status=ChatStatus.COLD,
                    ),
                ),
            )
            cold_state = await graph._build_context({"ctx": cold_ctx})
            cold_messages = cold_state["messages"]

            graph.commit_sent_items(
                cold_ctx,
                [SendItem(type=SendItemType.TEXT, content="在。怎么了？")],
            )

            gate.state.msg_index_today = 2
            gate.age = "just now"
            hot_ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=2,
                    buffer_version=2,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e2",
                            "event_type": "message.text",
                            "text": "想聊聊",
                        }
                    ],
                ),
            )
            hot_state = await graph._build_context({"ctx": hot_ctx})
            hot_messages = hot_state["messages"]

            self.assertEqual(hot_messages[: len(cold_messages)], cold_messages)
            self.assertTrue(graph.get_prompt_observability()["append_only_check"])
            self.assertEqual(self._count_content(hot_messages, "你在吗"), 1)
            self.assertEqual(self._count_content(hot_messages, "在。怎么了？"), 1)
            self.assertEqual(self._count_content(hot_messages, "想聊聊"), 1)

        asyncio.run(scenario())

    def test_repeated_buffer_event_is_not_duplicated(self):
        async def scenario():
            graph = CompanionGraph(FakeLLM(), FakeMemory(), FakeMemeCatalog())
            gate = self._gate(msg_index=1, age="unknown")
            cold_ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=1,
                    buffer_version=1,
                    status=ChatStatus.COLD,
                    events=[
                        {
                            "event_id": "e1",
                            "event_type": "message.text",
                            "text": "我还没说完",
                        }
                    ],
                    cold_start_meta=ColdStartMeta(
                        timestamp="10:00",
                        last_user_message_age="unknown",
                        msg_index=1,
                        status=ChatStatus.COLD,
                    ),
                ),
            )
            first_state = await graph._build_context({"ctx": cold_ctx})
            first_messages = first_state["messages"]

            gate.state.msg_index_today = 2
            second_ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=2,
                    buffer_version=2,
                    status=ChatStatus.COLD,
                    events=[
                        {
                            "event_id": "e1",
                            "event_type": "message.text",
                            "text": "我还没说完",
                        },
                        {
                            "event_id": "e2",
                            "event_type": "message.text",
                            "text": "还有一句",
                        },
                    ],
                    cold_start_meta=ColdStartMeta(
                        timestamp="10:01",
                        last_user_message_age="just now",
                        msg_index=2,
                        status=ChatStatus.COLD,
                    ),
                ),
            )
            second_state = await graph._build_context({"ctx": second_ctx})
            second_messages = second_state["messages"]

            self.assertEqual(second_messages[: len(first_messages)], first_messages)
            self.assertTrue(graph.get_prompt_observability()["append_only_check"])
            self.assertEqual(self._count_content(second_messages, "我还没说完"), 1)
            self.assertEqual(self._count_content(second_messages, "还有一句"), 1)

        asyncio.run(scenario())

    def test_external_assistant_before_first_llm_keeps_system_prompt(self):
        async def scenario():
            graph = CompanionGraph(FakeLLM(), FakeMemory(), FakeMemeCatalog())
            graph.commit_external_assistant_text("早。")

            gate = self._gate(msg_index=1, age="unknown")
            ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=1,
                    buffer_version=1,
                    status=ChatStatus.COLD,
                    events=[
                        {
                            "event_id": "e1",
                            "event_type": "message.text",
                            "text": "早",
                        }
                    ],
                    cold_start_meta=ColdStartMeta(
                        timestamp="10:00",
                        last_user_message_age="unknown",
                        msg_index=1,
                        status=ChatStatus.COLD,
                    ),
                ),
            )
            state = await graph._build_context({"ctx": ctx})
            messages = state["messages"]

            self.assertEqual(messages[0]["role"], "system")
            self.assertIn("Action Harness", messages[0]["content"])
            self.assertEqual(self._count_content(messages, "早。"), 1)
            self.assertEqual(self._count_content(messages, "早"), 1)

        asyncio.run(scenario())

    def test_memory_prompt_view_filters_non_pending_tomorrow_topics_and_logs_hashes(self):
        async def scenario():
            graph = CompanionGraph(FakeLLM(), FakeMemoryWithTomorrow(), FakeMemeCatalog())
            gate = self._gate(msg_index=1, age="unknown")
            ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=1,
                    buffer_version=1,
                    status=ChatStatus.COLD,
                    events=[
                        {
                            "event_id": "e1",
                            "event_type": "message.text",
                            "text": "早",
                        }
                    ],
                    cold_start_meta=ColdStartMeta(
                        timestamp="10:00",
                        last_user_message_age="unknown",
                        msg_index=1,
                        status=ChatStatus.COLD,
                    ),
                ),
            )
            state = await graph._build_context({"ctx": ctx})
            system_prompt = state["messages"][0]["content"]
            observability = graph.get_prompt_observability()

            self.assertIn("还想聊一下考试", system_prompt)
            self.assertIn("楼下桂花开了", system_prompt)
            self.assertNotIn("已经发过的问候", system_prompt)
            self.assertNotIn("不适合主动提的事", system_prompt)
            self.assertNotIn("已经过期的话题", system_prompt)
            self.assertIn("stable_block_hash", observability)
            self.assertIn("profile_block_hash", observability)
            self.assertIn("runtime_block_hash", observability)
            self.assertIn("estimated_prompt_tokens", observability)

        asyncio.run(scenario())

    def _gate(self, msg_index: int, age: str):
        gate = SimpleNamespace()
        gate.state = SimpleNamespace(msg_index_today=msg_index)
        gate.age = age
        gate._get_last_message_age = lambda: gate.age
        return gate

    def _count_content(self, messages, content):
        return sum(1 for item in messages if item.get("content") == content)


if __name__ == "__main__":
    unittest.main()
