import asyncio
import json
import unittest
from types import SimpleNamespace

from app.core.decisions import Action, ActionDecision, SendItem, SendItemType
from app.core.graph import CompanionGraph
from app.core.protocol import build_repair_messages, parse_and_validate_raw_decision
from app.core.state import ChatStatus, ColdStartMeta, ConversationSnapshot
from app.llm.client import LLMClient, LLMResponseEnvelope
from app.llm.prompts import (
    build_memory_analysis_messages,
    build_mem_command_messages,
    build_meme_search_messages,
    build_midnight_cleanup_messages,
    build_system_prompt,
)


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


class FakeReasoningEnvelopeLLM(FakeLLM):
    def __init__(self):
        self.raw = '{"action":"REPLY","items":[{"text":"我在呢"}]}'

    async def chat_completion_envelope(self, messages, temperature=0.7):
        return LLMResponseEnvelope(
            assistant_message={
                "role": "assistant",
                "content": None,
                "reasoning_content": self.raw,
            },
            content=None,
            reasoning_content=self.raw,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 18},
                "prompt_tokens_details": {"cached_tokens": 64},
            },
            raw_response_meta={"finish_reason": "stop", "model": "fake-reasoning"},
        )


class FakeRepairEnvelopeLLM(FakeLLM):
    def __init__(self):
        self.calls = 0
        self.requests = []

    async def chat_completion_envelope(self, messages, temperature=0.7):
        self.calls += 1
        self.requests.append([dict(item) for item in messages])
        if self.calls == 1:
            raw = '{"action":"REACT","items":[{"type":"search_meme","content":"search_meme:amused:laugh"}]}'
        else:
            raw = '{"action":"REPLY","items":[{"text":"修好了"}]}'
        return LLMResponseEnvelope(
            assistant_message={"role": "assistant", "content": raw},
            content=raw,
        )


class FakeEmptyEnvelopeLLM(FakeLLM):
    async def chat_completion_envelope(self, messages, temperature=0.7):
        return LLMResponseEnvelope(
            assistant_message={"role": "assistant", "content": None},
            content=None,
            reasoning_content=None,
        )


class FakeIdentityEnvelopeLLM(FakeLLM):
    def __init__(self, identity="model-a"):
        self.identity = identity

    def get_transcript_identity(self):
        return self.identity

    def get_cache_debug(self):
        return {"cache_session_id": f"cache-{self.identity}"}


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
            graph._append_provider_assistant_message({
                "role": "assistant",
                "content": '{"action":"ENTER_CHAT","items":[{"text":"在。怎么了？"}]}',
            })

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
            self.assertEqual(self._count_content(hot_messages, "在。怎么了？"), 0)
            self.assertTrue(any(
                item.get("role") == "assistant" and "在。怎么了？" in str(item.get("content") or "")
                for item in hot_messages
            ))
            self.assertEqual(self._count_content(hot_messages, "想聊聊"), 1)

        asyncio.run(scenario())

    def test_hot_turn_system_reminder_is_system_message_near_current_input(self):
        async def scenario():
            graph = CompanionGraph(FakeLLM(), FakeMemory(), FakeMemeCatalog())
            gate = self._gate(msg_index=1, age="just now")
            hot_ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=1,
                    buffer_version=1,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e1",
                            "event_type": "message.text",
                            "text": "今天有点困",
                        }
                    ],
                ),
            )
            hot_state = await graph._build_context({"ctx": hot_ctx})
            hot_messages = hot_state["messages"]

            reminder_indexes = [
                index
                for index, item in enumerate(hot_messages)
                if "HOT_TURN_REMINDER" in str(item.get("content") or "")
            ]
            self.assertEqual(len(reminder_indexes), 1)
            reminder_index = reminder_indexes[0]
            self.assertEqual(hot_messages[reminder_index]["role"], "system")
            reminder_payload = json.loads(hot_messages[reminder_index]["content"])
            self.assertEqual(reminder_payload["message_type"], "SYSTEM_REMINDER")
            self.assertEqual(reminder_payload["visibility"], "internal_only_not_visible_to_user")
            self.assertTrue(any("不要每轮都用问句结尾" in rule for rule in reminder_payload["rules"]))
            self.assertTrue(any("Action Harness JSON" in rule for rule in reminder_payload["rules"]))

            user_index = next(
                index for index, item in enumerate(hot_messages)
                if item.get("role") == "user" and item.get("content") == "今天有点困"
            )
            final_index = next(
                index for index, item in enumerate(hot_messages)
                if "最终输出前强提醒" in str(item.get("content") or "")
            )
            self.assertLess(user_index, reminder_index)
            self.assertLess(reminder_index, final_index)
            self.assertIsNotNone(graph.get_prompt_observability()["hot_turn_reminder_hash"])

            cold_graph = CompanionGraph(FakeLLM(), FakeMemory(), FakeMemeCatalog())
            cold_ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=2,
                    buffer_version=2,
                    status=ChatStatus.COLD,
                    events=[
                        {
                            "event_id": "e2",
                            "event_type": "message.text",
                            "text": "在吗",
                        }
                    ],
                ),
            )
            cold_state = await cold_graph._build_context({"ctx": cold_ctx})
            self.assertFalse(any(
                "HOT_TURN_REMINDER" in str(item.get("content") or "")
                for item in cold_state["messages"]
            ))
            self.assertIsNone(cold_graph.get_prompt_observability()["hot_turn_reminder_hash"])

        asyncio.run(scenario())

    def test_internal_system_reminder_appends_to_provider_transcript_only(self):
        graph = CompanionGraph(FakeLLM(), FakeMemory(), FakeMemeCatalog())
        graph._prompt_transcript = [{"role": "system", "content": "base"}]

        graph.append_internal_system_reminder({"role": "assistant", "content": "bad"})
        graph.append_internal_system_reminder({"role": "system", "content": ""})
        graph.append_internal_system_reminder({"role": "system", "content": "remind"})

        transcript = graph._copy_prompt_transcript()
        self.assertEqual(transcript, [
            {"role": "system", "content": "base"},
            {"role": "system", "content": "remind"},
        ])
        self.assertEqual(graph._conversation_history, [])

    def test_reasoning_content_rescue_is_preserved_in_provider_transcript(self):
        async def scenario():
            graph = CompanionGraph(FakeReasoningEnvelopeLLM(), FakeMemory(), FakeMemeCatalog())
            gate = self._gate(msg_index=1, age="unknown")
            first_ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=1,
                    buffer_version=1,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e1",
                            "event_type": "message.text",
                            "text": "在吗",
                        }
                    ],
                ),
            )
            first_state = await graph._build_context({"ctx": first_ctx})
            decision_state = await graph._call_llm_for_decision(first_state)

            self.assertEqual(decision_state["decision"].action, Action.REPLY)
            self.assertEqual(graph.get_prompt_observability()["raw_output_source"], "reasoning_content_rescue")
            self.assertTrue(graph.get_prompt_observability()["content_empty_with_reasoning"])
            self.assertEqual(graph.get_prompt_observability()["cached_tokens"], 64)
            self.assertEqual(graph.get_prompt_observability()["llm_response_meta"]["finish_reason"], "stop")

            gate.state.msg_index_today = 2
            second_ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=2,
                    buffer_version=2,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e2",
                            "event_type": "message.text",
                            "text": "继续说",
                        }
                    ],
                ),
            )
            second_state = await graph._build_context({"ctx": second_ctx})
            assistant_messages = [
                item for item in second_state["messages"]
                if item.get("role") == "assistant" and item.get("reasoning_content")
            ]

            self.assertEqual(len(assistant_messages), 1)
            self.assertIsNone(assistant_messages[0].get("content"))
            self.assertIn('"action":"REPLY"', assistant_messages[0]["reasoning_content"])
            self.assertTrue(graph.get_prompt_observability()["append_only_check"])
            serialized_messages = json.dumps(second_state["messages"], ensure_ascii=False)
            self.assertNotIn("prompt_tokens", serialized_messages)
            self.assertNotIn("cached_tokens", serialized_messages)
            self.assertNotIn("finish_reason", serialized_messages)

        asyncio.run(scenario())

    def test_repair_uses_same_provider_transcript_without_tool_role(self):
        async def scenario():
            llm = FakeRepairEnvelopeLLM()
            graph = CompanionGraph(llm, FakeMemory(), FakeMemeCatalog())
            ctx = SimpleNamespace(
                gate=self._gate(msg_index=1, age="unknown"),
                snapshot=ConversationSnapshot(
                    snapshot_id=1,
                    buffer_version=1,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e1",
                            "event_type": "message.text",
                            "text": "讲一句",
                        }
                    ],
                ),
            )

            state = await graph._build_context({"ctx": ctx})
            decision_state = await graph._call_llm_for_decision(state)

            self.assertEqual(llm.calls, 2)
            self.assertEqual(decision_state["decision"].to_harness_payload(), {
                "action": "REPLY",
                "items": [{"text": "修好了"}],
            })
            repair_request = llm.requests[1]
            roles = [item.get("role") for item in repair_request]
            self.assertIn("assistant", roles)
            self.assertNotIn("tool", roles)
            self.assertTrue(any(
                item.get("role") == "assistant" and '"type":"search_meme"' in str(item.get("content") or "")
                for item in repair_request
            ))
            self.assertTrue(graph.get_prompt_observability()["append_only_check"])

        asyncio.run(scenario())

    def test_empty_provider_assistant_message_is_not_replayed(self):
        async def scenario():
            graph = CompanionGraph(FakeEmptyEnvelopeLLM(), FakeMemory(), FakeMemeCatalog())
            graph._prompt_transcript = [{"role": "system", "content": "base"}]
            raw = await graph._call_llm_for_messages(
                messages=graph._copy_prompt_transcript(),
                temperature=0.3,
                append_assistant_to_transcript=True,
            )

            self.assertEqual(raw, "")
            self.assertFalse(any(item.get("role") == "assistant" for item in graph._copy_prompt_transcript()))
            self.assertEqual(
                graph.get_prompt_observability()["assistant_message_skipped_reason"],
                "empty_assistant_message",
            )

        asyncio.run(scenario())

    def test_llm_identity_change_resets_provider_transcript_but_keeps_visible_history(self):
        async def scenario():
            llm = FakeIdentityEnvelopeLLM()
            graph = CompanionGraph(llm, FakeMemory(), FakeMemeCatalog())
            gate = self._gate(msg_index=1, age="unknown")
            first_ctx = SimpleNamespace(
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
            await graph._build_context({"ctx": first_ctx})
            graph._append_provider_assistant_message({
                "role": "assistant",
                "content": '{"action":"ENTER_CHAT","items":[{"text":"RAW_JSON_VISIBLE"}]}',
            })
            graph.commit_sent_items(first_ctx, [SendItem(type=SendItemType.TEXT, content="在。")])

            llm.identity = "model-b"
            gate.state.msg_index_today = 2
            second_ctx = SimpleNamespace(
                gate=gate,
                snapshot=ConversationSnapshot(
                    snapshot_id=2,
                    buffer_version=2,
                    status=ChatStatus.HOT,
                    events=[
                        {
                            "event_id": "e2",
                            "event_type": "message.text",
                            "text": "继续",
                        }
                    ],
                ),
            )
            state = await graph._build_context({"ctx": second_ctx})
            messages = state["messages"]
            serialized = json.dumps(messages, ensure_ascii=False)

            self.assertEqual(graph.get_prompt_observability()["prefix_rebuild_reason"], "llm_identity_changed")
            self.assertIn("你在吗", serialized)
            self.assertIn("在。", serialized)
            self.assertIn("继续", serialized)
            self.assertNotIn("RAW_JSON_VISIBLE", serialized)

        asyncio.run(scenario())

    def test_llm_client_adds_generic_cache_affinity_without_changing_messages(self):
        client = LLMClient(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="test-model",
            thinking_enabled=True,
        )
        messages = [{"role": "user", "content": "hi"}]
        kwargs = client._build_kwargs(messages, temperature=0.3, max_tokens=None, stream=False)
        cache_debug = client.get_cache_debug()
        old_session_id = client.cache_session_id

        self.assertIs(kwargs["messages"], messages)
        self.assertIn("prompt_cache_key", kwargs)
        self.assertEqual(kwargs["prompt_cache_key"], cache_debug["prompt_cache_key"])
        self.assertEqual(kwargs["extra_headers"]["session_id"], old_session_id)
        self.assertEqual(kwargs["extra_headers"]["x-client-request-id"], old_session_id)
        self.assertNotIn("temperature", kwargs)
        self.assertIn("thinking", kwargs["extra_body"])
        self.assertTrue(client._should_retry_without_prompt_cache_key(Exception("unknown parameter prompt_cache_key")))

        old_identity = client.get_transcript_identity()
        client.update_config(model="test-model-2")
        self.assertNotEqual(client.cache_session_id, old_session_id)
        self.assertNotEqual(client.get_transcript_identity(), old_identity)

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
            self.assertIn("不常见的陌生的名词", messages[0]["content"])
            self.assertIn("根据知识库分析一下用户为什么会提到这个陌生名词", messages[0]["content"])
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

    def test_meme_second_round_prompt_uses_compact_short_harness_shape(self):
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
        serialized_messages = json.dumps(messages, ensure_ascii=False)

        self.assertEqual(len(messages), 3)
        self.assertEqual(assistant_payload, {"action": "REACT", "items": [{"search_meme": "amused:laugh"}]})
        self.assertEqual(tool_payload["results"], [
            {"request": "amused:laugh", "candidates": ["amused_laugh_001"]}
        ])
        self.assertIn('{"meme":"<file_stem>"}', tool_payload["instruction"])
        self.assertNotIn('"type":"meme"', tool_payload["instruction"])
        self.assertNotIn("候选表情 JSON", serialized_messages)
        self.assertEqual(serialized_messages.count("amused_laugh_001"), 1)

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

    def test_system_prompt_guides_cold_hot_entry_judgment(self):
        prompt = build_system_prompt(
            soul_md="",
            memory_core_md="",
            today_memory_md="",
            tomorrow_topics_md="",
            include_profile=False,
        )

        self.assertIn("COLD 下每轮先做入热判断", prompt)
        self.assertIn("进入 HOT 代表接下来一段时间你会更在场", prompt)
        self.assertIn("不是为了绕过 COLD 限制发一句普通回复", prompt)
        self.assertIn("COLD 下 REACT 必须保持纯表情包", prompt)
        self.assertIn("COLD 下如果文字+表情值得进入热聊，用 ENTER_CHAT.items", prompt)
        self.assertIn("用户: 早，醒了吗", prompt)
        self.assertIn('"action":"ENTER_CHAT"', prompt)

    def test_midnight_cleanup_prompt_handles_shared_context_and_expired_memory(self):
        messages = build_midnight_cleanup_messages(
            date_str="2026-06-20",
            memory_core_md="# 永久核心记忆",
            day_memory_md="# 每日记忆",
            tomorrow_topics_md="# 明日话题",
        )
        system_prompt = messages[0]["content"]

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("共同梗", system_prompt)
        self.assertIn("暗号", system_prompt)
        self.assertIn("昵称", system_prompt)
        self.assertIn("专属表情含义", system_prompt)
        self.assertIn("用户明确要求记住、多次自然出现", system_prompt)
        self.assertIn("生命周期", system_prompt)
        self.assertIn("expired", system_prompt)
        self.assertIn("临时近期状态", system_prompt)
        self.assertIn("TOMORROW_TOPICS.md 中移除或降权", system_prompt)
        self.assertIn("图片理解结果不是用户事实", system_prompt)
        self.assertIn("单独普通图片分析结果不能直接进入 MEMORY_CORE.md", system_prompt)
        self.assertIn("用户原话 > 用户文字 + 图片理解 > 单独图片理解", system_prompt)
        self.assertIn("表情包理解结果不要进入 MEMORY_CORE.md", system_prompt)
        self.assertNotIn("删除 dm", system_prompt)

    def test_memory_analysis_prompt_limits_image_understanding_memory(self):
        messages = build_memory_analysis_messages(
            date_str="2026-06-21",
            transcript=[
                {
                    "created_at": "2026-06-21T12:00:00",
                    "role": "user",
                    "text": "今天下班路上看到这个晚霞 [图片]",
                }
            ],
            today_memory_md="# 每日记忆",
            tomorrow_topics_md="# 明日话题",
        )
        system_prompt = messages[0]["content"]
        payload = json.loads(messages[1]["content"])

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("图片理解结果只是一种低优先级辅助证据", system_prompt)
        self.assertIn("用户原话 > 用户文字 + 图片理解 > 单独图片理解", system_prompt)
        self.assertIn("用户只发图片、没有文字确认时", system_prompt)
        self.assertIn("表情包理解结果通常只代表当下心情、语气或接梗信号", system_prompt)
        self.assertIn("今天下班路上看到这个晚霞", payload["user_visible_events"])

    def test_mem_command_prompt_disambiguates_user_first_person(self):
        messages = build_mem_command_messages(
            content="我不喜欢初音未来了",
            memory_core_md="# 永久核心记忆",
            today_date="2026-06-21",
        )
        system_prompt = messages[0]["content"]
        payload = json.loads(messages[1]["content"])

        self.assertIn("其中“我/我的/本人/俺”都指用户", system_prompt)
        self.assertIn("写入 MEMORY_CORE.md 时必须消除说话人歧义", system_prompt)
        self.assertIn("用户现在不喜欢初音未来", system_prompt)
        self.assertIn("不要写成“我不喜欢初音未来了”", system_prompt)
        self.assertIn("我应该多主动找用户聊天", system_prompt)
        self.assertIn("不要写成“你应该多主动找我聊天”", system_prompt)
        self.assertIn("我以后不要频繁追问用户", system_prompt)
        self.assertEqual(payload["content_speaker"], "用户")
        self.assertIn("用户说的“我”必须落成“用户”", payload["memory_perspective"])
        self.assertIn("你应该", payload["memory_perspective"])

    def test_repair_prompt_marks_blocked_output_as_system_context_not_assistant_history(self):
        messages = build_repair_messages(
            base_messages=[{"role": "system", "content": "base"}],
            errors=["模型输出不是合法 JSON 对象"],
            original_raw="在呢宝，怎么啦？",
        )

        self.assertEqual([item["role"] for item in messages], ["system", "system", "system"])
        joined = "\n".join(item["content"] for item in messages)
        self.assertIn("SYSTEM_REMINDER", joined)
        self.assertIn("ACTION_HARNESS_PROTOCOL_ERROR", joined)
        self.assertIn("internal blocked draft", joined)
        self.assertIn("not user-visible chat history", joined)
        self.assertIn("Do not produce a follow-up reply to original_raw_output", joined)
        self.assertIn("不要丢成“嗯”", joined)
        self.assertIn("修复 COLD 错误时先判断入热价值", joined)
        self.assertIn("COLD 下混合文字和表情必须先判断是否值得进入 HOT", joined)
        self.assertIn("不要为了保留长句机械使用 ENTER_CHAT", joined)
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["message_type"], "SYSTEM_REMINDER")
        self.assertEqual(payload["status"], "ACTION_HARNESS_PROTOCOL_ERROR")
        self.assertEqual(payload["blocked_output_visibility"], "internal_only_not_visible_to_user")
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
