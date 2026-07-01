import json
import unittest

from app.core.decisions import Action, SendItemType
from app.core.group_chat import (
    classify_group_visible_text,
    group_chat_runtime_status,
    known_message_ids_from_group_window,
    known_qids_from_group_window,
    parse_group_chat_output,
)
from app.llm.prompts import build_group_chat_messages


class GroupChatRuntimeTest(unittest.TestCase):
    def test_group_prompt_is_isolated_from_private_chat_contract(self):
        window = [{"qid": "550808201", "text": "刚才那个图太抽象了"}]
        messages = build_group_chat_messages(
            group_window=window,
            trigger={"reason": "debug"},
            group_soul="# GROUP_SOUL\n群聊里有主见但不抢戏。",
            current_time="2026-06-30 23:30",
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("你不是任何人的女友", messages[0]["content"])
        self.assertIn("group_soul", messages[0]["content"])
        self.assertIn("只允许输出 WAIT、一条短自然文本", messages[0]["content"])
        self.assertIn("不要立即给建议", messages[0]["content"])
        self.assertIn("沉默后的补接", messages[0]["content"])
        self.assertIn("不要默认以提问收尾", messages[0]["content"])
        self.assertIn("current_time", messages[0]["content"])
        self.assertIn("&&category:keywords&&", messages[0]["content"])
        self.assertIn("不要输出 `meme:...`", messages[0]["content"])
        self.assertIn("主动性与日程边界", messages[0]["content"])
        self.assertIn("群聊主回复可以处理明确的主动消息时间设置请求", messages[0]["content"])
        self.assertIn("不是提醒工具", messages[0]["content"])
        self.assertIn("next/daily", messages[0]["content"])
        self.assertNotIn("SOUL.md", messages[0]["content"])
        self.assertNotIn("HOT", messages[0]["content"])
        self.assertNotIn("COLD", messages[0]["content"])
        self.assertNotIn("ENTER_CHAT", messages[0]["content"])

        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["task"], "group_chat_decision")
        self.assertEqual(payload["group_soul"], "# GROUP_SOUL\n群聊里有主见但不抢戏。")
        self.assertEqual(payload["group_window"], window)
        self.assertFalse(payload["output_contract"]["send_enabled"])
        self.assertIn("short_natural_text_with_one_meme_marker", payload["output_contract"]["allowed"])

    def test_group_prompt_only_contains_confirmed_nickname_mapping(self):
        messages = build_group_chat_messages(
            group_window=[{"qid": "550808201", "text": "不用群名片"}],
            qid_to_nickname={"550808201": "记忆昵称"},
            send_enabled=True,
        )

        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["confirmed_qid_to_nickname"], {"550808201": "记忆昵称"})
        self.assertTrue(payload["output_contract"]["send_enabled"])
        self.assertNotIn("平台群名片", messages[1]["content"])

    def test_group_prompt_includes_group_memory_payload(self):
        messages = build_group_chat_messages(
            group_window=[{"qid": "550808201", "text": "这个梗还在"}],
            group_memory={
                "qid_to_nickname": {"550808201": "小夏"},
                "common_memory": ["- <2026-06-30T23:31:00><群聊/mem 550808201><群里最近都在玩赛博猫猫梗>"],
                "personal_memory": ["- <550808201><2026-06-30T23:30:00><群聊/mem本人><昵称=小夏>"],
            },
        )

        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["group_memory"]["qid_to_nickname"], {"550808201": "小夏"})
        self.assertIn("赛博猫猫梗", "\n".join(payload["group_memory"]["common_memory"]))
        self.assertIn("昵称=小夏", "\n".join(payload["group_memory"]["personal_memory"]))

    def test_parse_group_wait_and_text(self):
        wait_result = parse_group_chat_output("WAIT", known_qids=["550808201"])
        self.assertTrue(wait_result.ok)
        self.assertEqual(wait_result.decision.action, Action.WAIT)
        self.assertFalse(wait_result.should_send)

        text_result = parse_group_chat_output("这个槽点可以先记一笔", known_qids=["550808201"])
        self.assertTrue(text_result.ok)
        self.assertEqual(text_result.decision.action, Action.REPLY)
        self.assertTrue(text_result.should_send)
        self.assertEqual(text_result.decision.text_bubbles(), ["这个槽点可以先记一笔"])

    def test_parse_group_text_with_meme_marker_becomes_internal_search_item(self):
        result = parse_group_chat_output("笑死，这个角度太离谱了 &&amused:laughing cat&&")

        self.assertTrue(result.ok)
        self.assertEqual(result.decision.action, Action.REACT)
        self.assertTrue(result.should_send)
        items = result.decision.all_items()
        self.assertEqual([item.type for item in items], [SendItemType.TEXT, SendItemType.SEARCH_MEME])
        self.assertEqual(items[0].content, "笑死，这个角度太离谱了")
        self.assertEqual(items[1].harness_value(), "amused:laughing cat")

    def test_parse_group_marker_only_is_typed_but_not_send_ready_until_selector_layer(self):
        result = parse_group_chat_output("&&amused:laugh&&")

        self.assertTrue(result.ok)
        self.assertEqual(result.decision.action, Action.REACT)
        self.assertFalse(result.should_send)
        self.assertEqual(result.decision.search_meme_items()[0].harness_value(), "amused:laugh")

    def test_parse_group_output_blocks_qid_leak(self):
        result = parse_group_chat_output("550808201 这句别直接念出来", known_qids=["550808201"])

        self.assertFalse(result.ok)
        self.assertEqual(result.decision.action, Action.WAIT)
        self.assertEqual(result.safety.reason, "known_qid_leak")

    def test_parse_group_output_blocks_known_message_id_leak(self):
        result = parse_group_chat_output(
            "99112233 这条别直接念出来",
            known_qids=["550808201"],
            known_message_ids=["99112233"],
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.decision.action, Action.WAIT)
        self.assertEqual(result.safety.reason, "known_message_id_leak")

    def test_parse_group_output_allows_active_message_side_effect_after_topic_layer(self):
        result = parse_group_chat_output("行，我明天叫你 &&daily:08:00&&")

        self.assertTrue(result.ok)
        self.assertEqual(result.decision.action, Action.REPLY)
        self.assertEqual(result.decision.text_bubbles(), ["行，我明天叫你"])
        self.assertEqual(result.decision.active_message_setting(), {"type": "daily", "time": "08:00"})

    def test_parse_group_output_blocks_internal_protocol_leaks(self):
        bad_samples = [
            "[[quote]]991122",
            "<tool_call>",
            "<meme:amused_cat>",
            "meme:amused_cat",
            "search_meme:amused:laugh",
            "unknown",
            "message_id=99112233",
            '{"action":"REPLY","items":[{"text":"hi"}]}',
        ]

        for sample in bad_samples:
            with self.subTest(sample=sample):
                result = parse_group_chat_output(sample, known_qids=["550808201"])
                self.assertFalse(result.ok)
                self.assertEqual(result.decision.action, Action.WAIT)

    def test_group_visible_classifier_blocks_internal_group_fields(self):
        samples = [
            "sender_card=小夏",
            "sender_nickname=小夏",
            "media_key: abc",
            "平台群名片不可信",
        ]

        for sample in samples:
            with self.subTest(sample=sample):
                result = classify_group_visible_text(sample, known_qids=[])
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, "group_internal_event_leak")

    def test_known_qids_from_group_window_uses_window_and_confirmed_mapping(self):
        qids = known_qids_from_group_window(
            [{"qid": "550808201"}, {"qid": "abc"}, {"qid": "42"}],
            qid_to_nickname={"1057552839": "记忆昵称"},
        )

        self.assertEqual(qids, ["1057552839", "550808201"])

    def test_known_message_ids_from_group_window_uses_media_and_trigger_ids(self):
        ids = known_message_ids_from_group_window(
            [
                {"qid": "550808201", "reply_to_message_id": "99112200"},
                {"qid": "1057552839", "media": [{"kind": "image", "message_id": "99112201"}]},
            ],
            extra_message_ids=["99112202"],
        )

        self.assertEqual(ids, ["99112200", "99112201", "99112202"])

    def test_group_runtime_status_is_phase_10_with_ops_switches_and_config_gated_send_layer(self):
        status = group_chat_runtime_status()

        self.assertTrue(status["enabled"])
        self.assertEqual(status["phase"], 10)
        self.assertEqual(status["prompt_phase"], 4)
        self.assertTrue(status["send_layer_enabled"])
        self.assertEqual(status["send_enabled"], "config_gated")
        self.assertTrue(status["trigger_scheduler_enabled"])
        self.assertTrue(status["image_understanding_enabled"])
        self.assertTrue(status["group_memory_enabled"])
        self.assertTrue(status["repetition_enabled"])
        self.assertTrue(status["activity_dynamic_cooldown_enabled"])
        self.assertFalse(status["uses_private_hot_cold"])
        self.assertFalse(status["uses_private_input_gate"])
        self.assertEqual(status["state_machine"], "GROUP_TYPED_ACTION_HARNESS")
        self.assertIn("text_with_meme_marker", status["allowed_outputs"])
        self.assertIn("text_with_active_message_marker", status["allowed_outputs"])
        self.assertIn("search_meme", status["internal_typed_items"])
        self.assertEqual(status["search_meme_resolution"], "shared_meme_selector")
        self.assertEqual(status["active_message_side_effect"], "group_scoped_after_successful_send")


if __name__ == "__main__":
    unittest.main()
