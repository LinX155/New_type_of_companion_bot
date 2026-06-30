import unittest
from datetime import datetime, timedelta

from app.core.events import ChatEvent, EventType
from app.core.group_repetition import GroupRepetitionDetector, normalize_repeated_text


class GroupRepetitionDetectorTest(unittest.TestCase):
    def test_selects_same_text_repeated_by_three_distinct_qids(self):
        detector = GroupRepetitionDetector(
            config={"cooldown_seconds": 0, "dedupe_window_seconds": 3600},
            now_func=lambda: datetime(2026, 6, 30, 23, 0),
        )
        window = [
            _entry("10001", "复读一下", "1"),
            _entry("10002", "复读一下", "2"),
            _entry("10003", "复读一下", "3"),
        ]

        result = detector.evaluate(_event("10003", "复读一下", "3"), window)

        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["text"], "复读一下")
        self.assertEqual(result["distinct_qid_count"], 3)
        self.assertEqual(result["message_ids"], ["1", "2", "3"])

    def test_skips_when_repeated_by_same_qid_only(self):
        detector = GroupRepetitionDetector(config={"cooldown_seconds": 0})
        window = [
            _entry("10001", "复读一下", "1"),
            _entry("10001", "复读一下", "2"),
            _entry("10001", "复读一下", "3"),
        ]

        result = detector.evaluate(_event("10001", "复读一下", "3"), window)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "below_threshold")

    def test_cooldown_and_dedupe_prevent_infinite_repeat(self):
        clock = {"now": datetime(2026, 6, 30, 23, 0)}
        detector = GroupRepetitionDetector(
            config={"cooldown_seconds": 60, "dedupe_window_seconds": 3600},
            now_func=lambda: clock["now"],
        )
        window = [
            _entry("10001", "哈", "1"),
            _entry("10002", "哈", "2"),
            _entry("10003", "哈", "3"),
        ]

        first = detector.evaluate(_event("10003", "哈", "3", clock["now"]), window)
        second = detector.evaluate(_event("10004", "哈", "4", clock["now"] + timedelta(seconds=10)), window)
        clock["now"] = clock["now"] + timedelta(minutes=10)
        third = detector.evaluate(_event("10004", "哈", "4", clock["now"]), window)

        self.assertEqual(first["status"], "selected")
        self.assertEqual(second["reason"], "cooldown")
        self.assertEqual(third["reason"], "duplicate_repeated_text")

    def test_normalize_repeated_text_collapses_whitespace(self):
        self.assertEqual(normalize_repeated_text("  复读   一下  "), "复读 一下")


def _entry(qid: str, text: str, message_id: str) -> dict:
    return {
        "session_id": "qq_group_123456",
        "event_type": EventType.TEXT.value,
        "timestamp": "2026-06-30T23:00:00",
        "group_id": "123456",
        "sender_qid": qid,
        "message_id": message_id,
        "text": text,
        "media": [],
        "meme": None,
    }


def _event(qid: str, text: str, message_id: str, timestamp: datetime | None = None) -> ChatEvent:
    return ChatEvent(
        event_id=f"evt_{message_id}",
        session_id="qq_group_123456",
        platform="qq",
        user_id=qid,
        event_type=EventType.TEXT,
        text=text,
        timestamp=timestamp or datetime(2026, 6, 30, 23, 0),
        raw={
            "message_type": "group",
            "qq_group_id": "123456",
            "qq_user_id": qid,
            "qq_self_id": "999001",
            "onebot_message_id": message_id,
        },
    )


if __name__ == "__main__":
    unittest.main()
