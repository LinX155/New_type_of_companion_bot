import asyncio
import json
import unittest
from types import SimpleNamespace

from app.core.decisions import Action, ActionDecision, SendItem, SendItemType
from app.core.graph import CompanionGraph
from app.core.protocol import build_repair_messages, parse_and_validate_raw_decision
from app.core.state import ChatStatus, ColdStartMeta, ConversationSnapshot
from app.llm.prompts import build_meme_search_messages


class FakeLLM:
    def get_last_usage(self):
        return None


class FailIfCalledLLM(FakeLLM):
    async def chat_completion(self, messages, temperature=0.7, stream=False):
        raise AssertionError("repair LLM should not be called for natural visible text")


class FakeJsonRepairLLM(FakeLLM):
    def __init__(self):
        self.calls = 0

    async def chat_completion(self, messages, temperature=0.7, stream=False):
        self.calls += 1
        return '{"action":"REACT","items":[{"search_meme":"amused:laugh"}]}'


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

    def get_categories(self):
        return {}

    def get_image_path(self, category_id, file_stem):
        return ""


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

    def test_short_harness_items_parse_to_internal_execution_items(self):
        raw = (
            '{"action":"REACT","items":['
            '{"text":"啊这"},'
            '{"search_meme":"helpless:tired facepalm"},'
            '{"text":"有点离谱"}'
            ']}'
        )
        result = parse_and_validate_raw_decision(raw)

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.decision.action, Action.REACT)
        self.assertEqual(
            [item.to_harness_item() for item in result.decision.all_items()],
            [
                {"text": "啊这"},
                {"search_meme": "helpless:tired facepalm"},
                {"text": "有点离谱"},
            ],
        )
        self.assertEqual(result.decision.search_meme_items()[0].content, "search_meme:helpless:tired facepalm")

    def test_short_meme_item_parses_without_meme_prefix(self):
        raw = '{"action":"REACT","items":[{"meme":"affection_anime_girl_hug_chu"}]}'
        result = parse_and_validate_raw_decision(raw)

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.decision.all_items()[0].content, "meme:affection_anime_girl_hug_chu")
        self.assertEqual(result.decision.to_harness_payload(), {
            "action": "REACT",
            "items": [{"meme": "affection_anime_girl_hug_chu"}],
        })

    def test_old_type_content_items_are_rejected_by_main_harness(self):
        raw = (
            '{"action":"REACT","items":['
            '{"type":"search_meme","content":"search_meme:amused:laugh"}'
            ']}'
        )
        result = parse_and_validate_raw_decision(raw)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "protocol_error")
        self.assertTrue(any("text、meme、search_meme" in error for error in result.errors))

    def test_react_text_only_normalizes_to_reply_without_emoji_item(self):
        result = parse_and_validate_raw_decision('{"action":"REACT","items":[{"text":"🙂"}]}')

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.decision.action, Action.REPLY)
        self.assertEqual(result.decision.to_harness_payload(), {
            "action": "REPLY",
            "items": [{"text": "🙂"}],
        })

    def test_debug_parsed_items_use_short_harness_shape(self):
        graph = CompanionGraph(FakeLLM(), FakeMemory(), FakeMemeCatalog())
        graph._record_parsed_decision(
            ActionDecision(
                action=Action.REACT,
                items=[
                    SendItem(type=SendItemType.TEXT, content="啊这"),
                    SendItem(type=SendItemType.SEARCH_MEME, content="amused:laugh"),
                ],
            )
        )

        parsed = graph.get_last_parsed_decision()
        self.assertEqual(parsed["items"], [{"text": "啊这"}, {"search_meme": "amused:laugh"}])

    def test_meme_second_round_prompt_uses_short_harness_shape(self):
        messages = build_meme_search_messages(
            base_messages=[{"role": "system", "content": "base"}],
            search_results=[
                {
                    "request": "amused:laugh",
                    "category": "amused",
                    "keywords": "laugh",
                    "candidates": ["amused_laugh_001"],
                }
            ],
            original_decision={"action": "REACT", "items": [{"search_meme": "amused:laugh"}]},
        )

        assistant_payload = json.loads(messages[1]["content"])
        tool_payload = json.loads(messages[2]["content"])
        final_instruction = messages[3]["content"]

        self.assertEqual(assistant_payload, {"action": "REACT", "items": [{"search_meme": "amused:laugh"}]})
        self.assertIn('{"meme":"<file_stem>"}', final_instruction)
        self.assertNotIn('"type":"meme"', final_instruction)
        self.assertIn('{"meme":"<file_stem>"}', "\n".join(tool_payload["selection_rules"]))

    def test_natural_visible_output_is_coerced_before_repair(self):
        async def scenario():
            graph = CompanionGraph(FailIfCalledLLM(), FakeMemory(), FakeMemeCatalog())
            ctx = SimpleNamespace(
                gate=self._gate(msg_index=2, age="just now"),
                snapshot=ConversationSnapshot(
                    snapshot_id=2,
                    buffer_version=2,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e2",
                            "event_type": "message.text",
                            "text": "还没呢，你吃这么早吗，我一般六点才吃",
                        }
                    ],
                ),
            )

            decision, status = await graph._parse_decision_with_harness(
                ctx=ctx,
                base_messages=[],
                raw_output="我在呢，刚准备找你。",
                repair=True,
            )

            self.assertEqual(status, "natural_text_coerced")
            self.assertEqual(decision.action.value, "REPLY")
            contents = [item.content for item in decision.all_items()]
            self.assertEqual(contents, ["我在呢，刚准备找你。"])
            self.assertNotEqual(contents, ["嗯"])

        asyncio.run(scenario())

    def test_json_like_protocol_error_still_uses_repair(self):
        async def scenario():
            llm = FakeJsonRepairLLM()
            graph = CompanionGraph(llm, FakeMemory(), FakeMemeCatalog())
            ctx = SimpleNamespace(
                gate=self._gate(msg_index=2, age="just now"),
                snapshot=ConversationSnapshot(
                    snapshot_id=2,
                    buffer_version=2,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e2",
                            "event_type": "message.text",
                            "text": "给我个好笑的表情包",
                        }
                    ],
                ),
            )

            decision, status = await graph._parse_decision_with_harness(
                ctx=ctx,
                base_messages=[],
                raw_output='{"action":"REACT","items":[{"type":"search_meme","content":"search_meme:amused:laugh"}]}',
                repair=True,
            )

            self.assertEqual(status, "json_repair_ok")
            self.assertEqual(llm.calls, 1)
            self.assertEqual(decision.to_harness_payload(), {
                "action": "REACT",
                "items": [{"search_meme": "amused:laugh"}],
            })

        asyncio.run(scenario())

    def test_json_array_draft_uses_repair_instead_of_visible_text(self):
        async def scenario():
            llm = FakeJsonRepairLLM()
            graph = CompanionGraph(llm, FakeMemory(), FakeMemeCatalog())
            ctx = SimpleNamespace(
                gate=self._gate(msg_index=16, age="just now"),
                snapshot=ConversationSnapshot(
                    snapshot_id=16,
                    buffer_version=16,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e16",
                            "event_type": "message.text",
                            "text": "终于交了，感觉自己活下来了",
                        }
                    ],
                ),
            )

            decision, status = await graph._parse_decision_with_harness(
                ctx=ctx,
                base_messages=[],
                raw_output='[{"text":"终于！！！"},{"search_meme":"praise:finally done celebrate"}]',
                repair=True,
            )

            self.assertEqual(status, "json_repair_ok")
            self.assertEqual(llm.calls, 1)
            self.assertEqual(decision.to_harness_payload(), {
                "action": "REACT",
                "items": [{"search_meme": "amused:laugh"}],
            })

        asyncio.run(scenario())

    def test_relaxed_invalid_meme_stem_becomes_search_meme(self):
        async def scenario():
            graph = CompanionGraph(FailIfCalledLLM(), FakeMemory(), FakeMemeCatalog())
            ctx = SimpleNamespace(
                gate=self._gate(msg_index=2, age="just now"),
                snapshot=ConversationSnapshot(
                    snapshot_id=2,
                    buffer_version=2,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e2",
                            "event_type": "message.text",
                            "text": "给我个表情包安慰一下",
                        }
                    ],
                ),
            )

            decision, status = await graph._parse_decision_with_harness(
                ctx=ctx,
                base_messages=[],
                raw_output="先抱一下\n[表情: meme:affection_fake_hug]\n撑住撑住",
                repair=True,
            )

            self.assertEqual(status, "natural_text_coerced")
            self.assertEqual(decision.action.value, "REACT")
            self.assertEqual(decision.to_harness_payload(), {
                "action": "REACT",
                "items": [
                    {"text": "先抱一下"},
                    {"search_meme": "affection:fake hug"},
                    {"text": "撑住撑住"},
                ],
            })

        asyncio.run(scenario())

    def test_meme_request_without_rich_item_is_forced_to_search_meme(self):
        graph = CompanionGraph(FakeLLM(), FakeMemory(), FakeMemeCatalog())
        ctx = SimpleNamespace(
            gate=self._gate(msg_index=13, age="just now"),
            snapshot=ConversationSnapshot(
                snapshot_id=13,
                buffer_version=13,
                status=ChatStatus.HOT,
                events=[
                    {
                        "event_id": "e13",
                        "event_type": "message.text",
                        "text": "给我个表情包安慰一下",
                    }
                ],
            ),
        )

        decision = ActionDecision(
            action=Action.REPLY,
            items=[SendItem(type=SendItemType.TEXT, content="抱抱你，先缓一口气。")],
        )

        fixed = graph._apply_protocol_guards(ctx, decision)

        self.assertEqual(fixed.to_harness_payload(), {
            "action": "REACT",
            "items": [{"search_meme": "amused:funny"}],
        })

    def test_repair_prompt_marks_blocked_output_as_system_context_not_assistant_history(self):
        messages = build_repair_messages(
            base_messages=[{"role": "system", "content": "base"}],
            errors=["模型输出不是合法 JSON 对象"],
            original_raw="在呢宝，怎么啦？",
        )

        self.assertEqual([item["role"] for item in messages], ["system", "system", "system"])
        joined = "\n".join(item["content"] for item in messages)
        self.assertIn("已经被拦截，用户没有看到它", joined)
        self.assertIn("不要顺着它继续说话", joined)
        self.assertIn("把 original_raw_output 中适合用户看到的自然内容搬进 items", joined)
        self.assertIn("不要丢成“嗯”", joined)
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["schema"]["items"], [
            {"text": "用户可见文本或 emoji"},
            {"meme": "<file_stem>"},
            {"search_meme": "<category>:<keywords>"},
        ])
        self.assertEqual(payload["conversion_examples"][0]["good"], {
            "action": "REPLY",
            "items": [{"text": "在呢宝，怎么啦？"}],
        })

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
