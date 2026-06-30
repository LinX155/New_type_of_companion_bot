import asyncio
import unittest
from datetime import datetime, timedelta

from app.api import routes
from app.core.events import ChatEvent, EventType
from app.core.group_buffer import GroupChatBuffer
from app.core.group_send import (
    GroupSendLimiter,
    evaluate_group_send_policy,
    normalize_group_send_config,
)


class GroupSendLimiterTest(unittest.TestCase):
    def test_group_send_config_defaults_to_disabled_empty_whitelist(self):
        config = normalize_group_send_config(None)

        self.assertFalse(config["enabled"])
        self.assertEqual(config["allowed_group_ids"], [])
        self.assertTrue(config["default_group_policy"]["observe_only"])

    def test_limiter_allows_whitelisted_group_then_blocks_duplicate(self):
        now = datetime(2026, 6, 30, 23, 59)
        limiter = GroupSendLimiter(
            {
                "enabled": True,
                "allowed_group_ids": ["123456"],
                "groups": {"123456": {"observe_only": False}},
                "min_interval_seconds": 0,
                "dedupe_window_seconds": 300,
            },
            now_func=lambda: now,
        )

        first = limiter.evaluate("qq_group_123456", item_type="text", content="这句可以发")
        self.assertTrue(first["ok"])
        limiter.record_sent("qq_group_123456", item_type="text", content="这句可以发")

        duplicate = limiter.evaluate("qq_group_123456", item_type="text", content="这句可以发")
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["reason"], "group_send_duplicate")

    def test_limiter_rate_limits_recent_send(self):
        now = datetime(2026, 6, 30, 23, 59)
        clock = {"value": now}
        limiter = GroupSendLimiter(
            {
                "enabled": True,
                "allowed_group_ids": ["123456"],
                "groups": {"123456": {"observe_only": False}},
                "min_interval_seconds": 60,
                "dedupe_window_seconds": 0,
            },
            now_func=lambda: clock["value"],
        )

        limiter.record_sent("qq_group_123456", item_type="text", content="第一句")
        clock["value"] = now + timedelta(seconds=10)
        second = limiter.evaluate("qq_group_123456", item_type="text", content="第二句")

        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "group_send_rate_limited")

    def test_default_group_policy_keeps_whitelisted_group_in_observation(self):
        limiter = GroupSendLimiter(
            {
                "enabled": True,
                "allowed_group_ids": ["123456"],
                "min_interval_seconds": 0,
            },
        )

        result = limiter.evaluate("qq_group_123456", item_type="text", content="观察期不会发")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "group_observe_only")

    def test_group_policy_blocks_disabled_trigger_types(self):
        config = normalize_group_send_config({
            "enabled": True,
            "groups": {
                "123456": {
                    "observe_only": False,
                    "allow_mention_reply": True,
                    "allow_roll_reply": False,
                    "allow_repetition": False,
                    "allow_meme_send": False,
                }
            },
        })

        mention = evaluate_group_send_policy(
            config,
            "qq_group_123456",
            trigger_reason="mention",
            item_type="text",
        )
        roll = evaluate_group_send_policy(
            config,
            "qq_group_123456",
            trigger_reason="roll",
            item_type="text",
        )
        meme = evaluate_group_send_policy(
            config,
            "qq_group_123456",
            trigger_reason="mention",
            item_type="meme",
        )

        self.assertTrue(mention["ok"])
        self.assertFalse(roll["ok"])
        self.assertEqual(roll["reason"], "group_roll_reply_disabled")
        self.assertFalse(meme["ok"])
        self.assertEqual(meme["reason"], "group_meme_send_disabled")


class GroupSendRouteTest(unittest.TestCase):
    def test_group_candidate_sends_text_to_whitelisted_group_with_reply_segment(self):
        async def scenario():
            fake_onebot = FakeOneBot()
            sent_logs = []
            states = []
            originals = _patch_route_globals(fake_onebot, sent_logs, states, {
                "group_chat_send": {
                    "enabled": True,
                    "allowed_group_ids": ["123456"],
                    "groups": {"123456": {"observe_only": False}},
                    "min_interval_seconds": 0,
                    "dedupe_window_seconds": 300,
                }
            })
            try:
                await routes.group_chat_buffer.append(_group_event())
                result = await routes._send_group_reply_candidate(
                    "qq_group_123456",
                    {"trigger_id": "group_test", "source_message_id": "99112233"},
                    {
                        "should_send_candidate": True,
                        "final_text": "这个槽点可以接一下",
                        "reply_to_message_id": "99112233",
                    },
                )
            finally:
                _restore_route_globals(originals)

            self.assertEqual(result["status"], "sent")
            self.assertEqual(fake_onebot.group_text_calls, [("123456", "这个槽点可以接一下", "99112233")])
            self.assertEqual(sent_logs[0]["status"], "sent")
            self.assertEqual(states[0]["result"], "group_reply_send")

        asyncio.run(scenario())

    def test_group_candidate_is_skipped_when_group_send_is_disabled(self):
        async def scenario():
            fake_onebot = FakeOneBot()
            sent_logs = []
            states = []
            originals = _patch_route_globals(fake_onebot, sent_logs, states, {})
            try:
                await routes.group_chat_buffer.append(_group_event())
                result = await routes._send_group_reply_candidate(
                    "qq_group_123456",
                    {"trigger_id": "group_test", "source_message_id": "99112233"},
                    {"should_send_candidate": True, "final_text": "不会发出去"},
                )
            finally:
                _restore_route_globals(originals)

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "group_send_disabled")
            self.assertEqual(fake_onebot.group_text_calls, [])

        asyncio.run(scenario())

    def test_group_candidate_final_guard_blocks_qid_leak_before_onebot(self):
        async def scenario():
            fake_onebot = FakeOneBot()
            sent_logs = []
            states = []
            originals = _patch_route_globals(fake_onebot, sent_logs, states, {
                "group_chat_send": {
                    "enabled": True,
                    "allowed_group_ids": ["123456"],
                    "groups": {"123456": {"observe_only": False}},
                    "min_interval_seconds": 0,
                }
            })
            try:
                await routes.group_chat_buffer.append(_group_event())
                result = await routes._send_group_reply_candidate(
                    "qq_group_123456",
                    {"trigger_id": "group_test", "source_message_id": "99112233"},
                    {"should_send_candidate": True, "final_text": "550808201 这句不能泄露"},
                )
            finally:
                _restore_route_globals(originals)

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "known_qid_leak")
            self.assertEqual(fake_onebot.group_text_calls, [])

        asyncio.run(scenario())

    def test_group_config_api_can_end_observation_for_one_group(self):
        async def scenario():
            store = {"group_chat_send": {}}
            original_load = routes.load_settings
            original_save = routes.save_settings
            original_limiter = routes.group_send_limiter
            routes.load_settings = lambda: dict(store)
            routes.save_settings = lambda updates: store.update(updates) or True
            routes.group_send_limiter = GroupSendLimiter()
            try:
                before = await routes.get_group_chat_config(group_id="123456")
                result = await routes.update_group_chat_config(routes.GroupChatOpsConfig(
                    enabled=True,
                    group_id="123456",
                    observe_only=False,
                    allow_mention_reply=True,
                    allow_roll_reply=True,
                    allow_repetition=False,
                    allow_meme_send=False,
                ))
                after = await routes.get_group_chat_config(group_id="123456")
            finally:
                routes.load_settings = original_load
                routes.save_settings = original_save
                routes.group_send_limiter = original_limiter

            self.assertTrue(before["group_policy"]["observe_only"])
            self.assertEqual(result["status"], "ok")
            self.assertTrue(store["group_chat_send"]["enabled"])
            self.assertIn("123456", store["group_chat_send"]["allowed_group_ids"])
            self.assertFalse(after["group_policy"]["observe_only"])
            self.assertTrue(after["group_policy"]["allow_roll_reply"])

        asyncio.run(scenario())


class FakeOneBot:
    def __init__(self):
        self.group_text_calls = []
        self.group_image_calls = []

    async def send_group_text(self, group_id, text, reply_to_message_id=None):
        self.group_text_calls.append((str(group_id), text, reply_to_message_id))
        return {"status": "ok", "data": {"message_id": 778899}}

    async def send_group_image(self, group_id, file_uri, reply_to_message_id=None):
        self.group_image_calls.append((str(group_id), file_uri, reply_to_message_id))
        return {"status": "ok", "data": {"message_id": 778900}}


def _patch_route_globals(fake_onebot, sent_logs, states, settings):
    originals = {
        "onebot_manager": routes.onebot_manager,
        "group_chat_buffer": routes.group_chat_buffer,
        "group_send_limiter": routes.group_send_limiter,
        "load_settings": routes.load_settings,
        "_record_onebot_group_send": routes._record_onebot_group_send,
        "_emit_state": routes._emit_state,
    }
    routes.onebot_manager = fake_onebot
    routes.group_chat_buffer = GroupChatBuffer()
    routes.group_send_limiter = GroupSendLimiter()
    routes.load_settings = lambda: dict(settings)
    routes._record_onebot_group_send = lambda *args, **kwargs: sent_logs.append({
        "session_id": args[0],
        "send_key": args[1],
        "group_id": args[2],
        "item_type": args[3],
        "content": args[4],
        "status": args[6],
        "error_message": args[7] if len(args) > 7 else None,
    })

    async def emit_state(payload):
        states.append(payload)

    routes._emit_state = emit_state
    return originals


def _restore_route_globals(originals):
    for key, value in originals.items():
        setattr(routes, key, value)


def _group_event() -> ChatEvent:
    return ChatEvent(
        event_id="evt_group_text",
        session_id="qq_group_123456",
        platform="qq",
        user_id="550808201",
        event_type=EventType.TEXT,
        text="普通群聊",
        timestamp=datetime(2026, 6, 30, 23, 30),
        raw={
            "message_type": "group",
            "qq_group_id": "123456",
            "qq_user_id": "550808201",
            "onebot_message_id": "99112233",
        },
    )


if __name__ == "__main__":
    unittest.main()
