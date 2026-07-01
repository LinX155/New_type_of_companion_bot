import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes
from app.core.events import ChatEvent, EventType
from app.core.group_buffer import GroupChatBuffer
from app.core.group_send import (
    GroupSendLimiter,
    evaluate_group_send_policy,
    normalize_group_send_config,
)
from app.storage.models import Base, ConversationEvent, RawChatLog


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

    def test_group_policy_blocks_active_message_unless_explicitly_allowed(self):
        config = normalize_group_send_config({
            "enabled": True,
            "allowed_group_ids": ["123456"],
            "groups": {"123456": {"observe_only": False}},
        })

        blocked = evaluate_group_send_policy(
            config,
            "qq_group_123456",
            trigger_reason="active_message",
            item_type="text",
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "group_active_message_disabled")

        allowed_config = normalize_group_send_config({
            "enabled": True,
            "allowed_group_ids": ["123456"],
            "groups": {"123456": {"observe_only": False, "allow_active_message": True}},
        })
        allowed = evaluate_group_send_policy(
            allowed_config,
            "qq_group_123456",
            trigger_reason="active_message",
            item_type="text",
        )
        self.assertTrue(allowed["ok"])


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

    def test_onebot_group_send_commits_group_memory_command_as_assistant_text(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        original_get_db = routes.get_db

        def fake_get_db():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        routes.get_db = fake_get_db
        try:
            routes._record_onebot_group_send(
                "qq_group_123456",
                "group_memory_test:group_send",
                "123456",
                "text",
                "已把你的群聊昵称记住了。",
                {"status": "ok", "data": {"message_id": 778899}},
                "sent",
                None,
                "99112233",
                send_index=0,
                send_count=1,
                audit={"trigger_id": "group_memory_test", "trigger_reason": "group.command.mem"},
            )

            db = Session()
            try:
                events = db.query(ConversationEvent).all()
                logs = db.query(RawChatLog).all()
            finally:
                db.close()
        finally:
            routes.get_db = original_get_db

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "assistant_text")
        self.assertEqual(events[0].action, "group.command.mem")
        self.assertEqual(events[0].text, "已把你的群聊昵称记住了。")
        self.assertEqual(logs[0].send_index, 0)
        self.assertEqual(logs[0].send_count, 1)
        payload = json.loads(logs[0].raw_payload)
        self.assertTrue(payload["visible_history_commit"]["committed"])
        self.assertEqual(payload["visible_history_commit"]["event_type"], "assistant_text")
        self.assertEqual(payload["visible_history_commit"]["action"], "group.command.mem")
        self.assertEqual(payload["audit"]["trigger_reason"], "group.command.mem")

    def test_group_send_audit_records_guarded_drop_chain(self):
        async def scenario():
            engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)

            def fake_get_db():
                db = Session()
                try:
                    yield db
                finally:
                    db.close()

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
            original_get_db = routes.get_db
            routes.get_db = fake_get_db
            routes._record_group_send_chain_audit = originals["_record_group_send_chain_audit"]
            try:
                await routes.group_chat_buffer.append(_group_event())
                result = await routes._send_group_reply_candidate(
                    "qq_group_123456",
                    {"trigger_id": "group_guard_test", "reason": "roll", "source_message_id": "99112233"},
                    {
                        "should_send_candidate": True,
                        "parse_status": "ok",
                        "final_text": "550808201 这句不能泄露",
                    },
                )
                db = Session()
                try:
                    audit_log = db.query(RawChatLog).filter(RawChatLog.event_type == "group_send_audit").one()
                    payload = json.loads(audit_log.raw_payload)
                finally:
                    db.close()
            finally:
                routes.get_db = original_get_db
                _restore_route_globals(originals)

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "known_qid_leak")
            self.assertEqual(payload["trigger_id"], "group_guard_test")
            self.assertEqual(payload["trigger_reason"], "roll")
            self.assertEqual(payload["status"], "skipped")
            self.assertEqual(payload["drop_reason"], "known_qid_leak")
            self.assertEqual(payload["item_statuses"][0]["visible_history_commit"]["committed"], False)
            self.assertEqual(fake_onebot.group_text_calls, [])

        asyncio.run(scenario())

    def test_group_candidate_sends_text_and_meme_items_in_one_decision(self):
        async def scenario():
            fake_onebot = FakeOneBot()
            sent_logs = []
            states = []
            meme_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            meme_file.write(b"fake-png")
            meme_file.close()
            originals = _patch_route_globals(fake_onebot, sent_logs, states, {
                "group_chat_send": {
                    "enabled": True,
                    "allowed_group_ids": ["123456"],
                    "groups": {
                        "123456": {
                            "observe_only": False,
                            "allow_roll_reply": True,
                            "allow_meme_send": True,
                        }
                    },
                    "min_interval_seconds": 60,
                    "dedupe_window_seconds": 300,
                }
            })
            original_meme_path = routes._meme_path_for_stem
            routes._meme_path_for_stem = lambda stem: meme_file.name if stem == "amused_laugh_001" else ""
            try:
                await routes.group_chat_buffer.append(_group_event())
                result = await routes._send_group_reply_candidate(
                    "qq_group_123456",
                    {"trigger_id": "group_meme_send_test", "reason": "roll", "source_message_id": "99112233"},
                    {
                        "should_send_candidate": True,
                        "items": [
                            {"text": "笑死，这图太会抢班了"},
                            {"meme": "amused_laugh_001"},
                        ],
                        "reply_to_message_id": "99112233",
                    },
                )
            finally:
                routes._meme_path_for_stem = original_meme_path
                _restore_route_globals(originals)
                Path(meme_file.name).unlink(missing_ok=True)

            self.assertEqual(result["status"], "sent")
            self.assertEqual(result["sent_count"], 2)
            self.assertEqual(fake_onebot.group_text_calls, [("123456", "笑死，这图太会抢班了", "99112233")])
            self.assertEqual(len(fake_onebot.group_image_calls), 1)
            self.assertEqual(fake_onebot.group_image_calls[0][0], "123456")
            self.assertEqual(fake_onebot.group_image_calls[0][2], "99112233")
            self.assertEqual([log["item_type"] for log in sent_logs], ["text", "meme"])
            self.assertEqual(states[0]["result"], "group_reply_send")

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
                    allow_active_message=True,
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
            self.assertTrue(after["group_policy"]["allow_active_message"])

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
        "_record_group_send_chain_audit": routes._record_group_send_chain_audit,
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
        "send_index": kwargs.get("send_index"),
        "send_count": kwargs.get("send_count"),
        "audit": kwargs.get("audit"),
    })
    routes._record_group_send_chain_audit = lambda *args, **kwargs: None

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
