import json
import unittest

from app.core.decisions import Action, ActionDecision, SendItem, SendItemType
from app.core.repetition_guard import (
    build_repetition_guard_system_reminder,
    filter_recent_repeated_reactions,
)


class RepetitionGuardTest(unittest.TestCase):
    def test_keeps_second_emoji_use_within_recent_window(self):
        decision = ActionDecision(
            action=Action.REPLY,
            items=[SendItem(type=SendItemType.TEXT, content="笑死我了😂")],
        )

        result = filter_recent_repeated_reactions(decision, ["刚才也很离谱😂"])

        self.assertEqual(result.decision.action, Action.REPLY)
        self.assertEqual([item.to_harness_item() for item in result.decision.all_items()], [{"text": "笑死我了😂"}])
        self.assertEqual(result.removed, [])

    def test_strips_third_emoji_use_from_text_without_dropping_words(self):
        decision = ActionDecision(
            action=Action.REPLY,
            items=[SendItem(type=SendItemType.TEXT, content="笑死我了😂")],
        )

        result = filter_recent_repeated_reactions(decision, ["刚才也很离谱😂", "真的绷不住😂"])

        self.assertEqual(result.decision.action, Action.REPLY)
        self.assertEqual([item.to_harness_item() for item in result.decision.all_items()], [{"text": "笑死我了"}])
        self.assertEqual([(item.kind, item.value) for item in result.removed], [("emoji", "😂")])

    def test_keeps_second_meme_use_within_recent_window(self):
        decision = ActionDecision(
            action=Action.REACT,
            items=[
                SendItem(type=SendItemType.TEXT, content="先抱一下"),
                SendItem(type=SendItemType.MEME, content="affection_hug"),
            ],
        )

        result = filter_recent_repeated_reactions(decision, ["meme:affection_hug"])

        self.assertEqual(result.decision.action, Action.REACT)
        self.assertEqual(
            [item.to_harness_item() for item in result.decision.all_items()],
            [{"text": "先抱一下"}, {"meme": "affection_hug"}],
        )
        self.assertEqual(result.removed, [])

    def test_drops_third_meme_item_and_keeps_text(self):
        decision = ActionDecision(
            action=Action.REACT,
            items=[
                SendItem(type=SendItemType.TEXT, content="先抱一下"),
                SendItem(type=SendItemType.MEME, content="affection_hug"),
            ],
        )

        result = filter_recent_repeated_reactions(decision, ["meme:affection_hug", "[表情: meme:affection_hug]"])

        self.assertEqual(result.decision.action, Action.REPLY)
        self.assertEqual([item.to_harness_item() for item in result.decision.all_items()], [{"text": "先抱一下"}])
        self.assertEqual([(item.kind, item.value) for item in result.removed], [("meme", "affection_hug")])

    def test_duplicate_reaction_only_turn_becomes_wait(self):
        decision = ActionDecision(
            action=Action.REACT,
            items=[SendItem(type=SendItemType.MEME, content="helpless_facepalm")],
        )

        result = filter_recent_repeated_reactions(decision, ["[表情: meme:helpless_facepalm]", "meme:helpless_facepalm"])

        self.assertEqual(result.decision.action, Action.WAIT)
        self.assertEqual(result.decision.all_items(), [])
        self.assertEqual([(item.kind, item.value) for item in result.removed], [("meme", "helpless_facepalm")])

    def test_repetition_reminder_is_internal_system_message(self):
        decision = ActionDecision(
            action=Action.REPLY,
            items=[SendItem(type=SendItemType.TEXT, content="笑死😂")],
        )
        result = filter_recent_repeated_reactions(decision, ["刚刚也笑了😂", "真的很离谱😂"])

        message = build_repetition_guard_system_reminder(result.removed)
        payload = json.loads(message["content"])

        self.assertEqual(message["role"], "system")
        self.assertEqual(payload["message_type"], "SYSTEM_REMINDER")
        self.assertEqual(payload["visibility"], "internal_only_not_visible_to_user")
        self.assertEqual(payload["status"], "REPETITION_GUARD_FILTERED")
        self.assertEqual(payload["filtered_items"], [{"kind": "emoji", "value": "😂", "item_index": 0}])
        self.assertTrue(any("第 3 次" in rule for rule in payload["rules"]))


if __name__ == "__main__":
    unittest.main()
