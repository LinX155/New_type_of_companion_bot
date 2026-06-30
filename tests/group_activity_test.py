import unittest
from datetime import datetime

from app.core.group_activity import GroupActivityTracker, day_type_for_datetime


class GroupActivityTrackerTest(unittest.TestCase):
    def test_daily_update_builds_weekday_active_and_quiet_labels(self):
        tracker = GroupActivityTracker(
            config={"ema_alpha": 1.0, "update_hour": 6},
            now_func=lambda: datetime(2026, 6, 30, 6, 0),
        )
        entries = [
            _entry("2026-06-29T21:00:00"),
            _entry("2026-06-29T21:05:00"),
            _entry("2026-06-29T21:10:00"),
            _entry("2026-06-29T22:00:00"),
        ]

        result = tracker.maybe_update("qq_group_123456", entries)
        status = tracker.status("qq_group_123456")

        self.assertEqual(result["status"], "updated")
        self.assertIn(21, status["profile"]["labels"]["weekday"]["active_hours"])
        self.assertIn(3, status["profile"]["labels"]["weekday"]["quiet_hours"])
        self.assertEqual(status["profile"]["last_source_event_count"], 4)

    def test_update_runs_once_after_six_unless_forced(self):
        now = datetime(2026, 6, 30, 6, 0)
        tracker = GroupActivityTracker(config={"ema_alpha": 1.0}, now_func=lambda: now)

        first = tracker.maybe_update("qq_group_123456", [_entry("2026-06-29T21:00:00")])
        second = tracker.maybe_update("qq_group_123456", [_entry("2026-06-29T22:00:00")])
        forced = tracker.maybe_update("qq_group_123456", [_entry("2026-06-29T22:00:00")], force=True)

        self.assertEqual(first["status"], "updated")
        self.assertEqual(second["reason"], "already_updated_today")
        self.assertEqual(forced["status"], "updated")

    def test_roll_cooldown_uses_active_and_quiet_multipliers(self):
        clock = {"now": datetime(2026, 6, 30, 6, 0)}
        tracker = GroupActivityTracker(
            config={
                "ema_alpha": 1.0,
                "active_cooldown_multiplier": 0.5,
                "quiet_cooldown_multiplier": 2.0,
                "min_roll_cooldown_minutes": 1.0,
                "max_roll_cooldown_minutes": 120.0,
            },
            now_func=lambda: clock["now"],
        )
        tracker.maybe_update(
            "qq_group_123456",
            [
                _entry("2026-06-29T21:00:00"),
                _entry("2026-06-29T21:05:00"),
                _entry("2026-06-29T21:10:00"),
            ],
            force=True,
        )

        active = tracker.roll_cooldown_adjustment(
            "qq_group_123456",
            30.0,
            now=datetime(2026, 6, 30, 21, 0),
        )
        quiet = tracker.roll_cooldown_adjustment(
            "qq_group_123456",
            30.0,
            now=datetime(2026, 6, 30, 3, 0),
        )

        self.assertEqual(active["label"], "active")
        self.assertEqual(active["roll_cooldown_minutes"], 15.0)
        self.assertEqual(quiet["label"], "quiet")
        self.assertEqual(quiet["roll_cooldown_minutes"], 60.0)

    def test_day_type_distinguishes_weekend(self):
        self.assertEqual(day_type_for_datetime(datetime(2026, 6, 30, 12, 0)), "weekday")
        self.assertEqual(day_type_for_datetime(datetime(2026, 7, 4, 12, 0)), "weekend")


def _entry(timestamp: str) -> dict:
    return {
        "session_id": "qq_group_123456",
        "timestamp": timestamp,
        "sender_qid": "10001",
        "message_id": timestamp,
        "text": "hi",
        "media": [],
    }


if __name__ == "__main__":
    unittest.main()
