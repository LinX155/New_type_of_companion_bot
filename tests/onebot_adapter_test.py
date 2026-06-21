import unittest
import asyncio
from datetime import datetime
from types import SimpleNamespace

from app.api.routes import _message_target_from_snapshot
from app.adapters.onebot11.client import OneBotConnectionManager
from app.adapters.onebot11.events import parse_onebot_event
from app.core.event_gate import EventGate
from app.core.events import ChatEvent, EventType


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class OneBotAdapterTest(unittest.TestCase):
    def test_private_text_message_parses_to_text_event(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": 1354983729,
            "user_id": 550808201,
            "time": 1782031609,
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "raw_message": "你好",
        })

        self.assertIsNotNone(event)
        self.assertEqual(event.platform, "qq")
        self.assertEqual(event.user_id, "550808201")
        self.assertEqual(event.event_type, EventType.TEXT)
        self.assertEqual(event.text, "你好")
        self.assertEqual(event.raw["onebot_message_id"], "1354983729")

    def test_private_image_parses_to_image_event(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "private",
            "message_id": 1411302046,
            "user_id": 550808201,
            "message": [
                {
                    "type": "image",
                    "data": {
                        "summary": "",
                        "file": "D0B97E3C.webp",
                        "sub_type": 0,
                        "url": "https://example.invalid/image.webp",
                        "file_size": "52798",
                    },
                }
            ],
        })

        self.assertEqual(event.event_type, EventType.IMAGE)
        self.assertEqual(event.text, "[图片]")
        self.assertEqual(event.raw["media_refs"][0]["is_sticker"], False)

    def test_subtype_one_image_parses_as_sticker(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "private",
            "message_id": 111211704,
            "user_id": 550808201,
            "message": [
                {
                    "type": "image",
                    "data": {
                        "summary": "[动画表情]",
                        "file": "6B70A008.jpg",
                        "sub_type": 1,
                        "url": "https://example.invalid/sticker.jpg",
                        "file_size": "107796",
                    },
                }
            ],
        })

        self.assertEqual(event.event_type, EventType.STICKER)
        self.assertEqual(event.text, "[表情]")
        self.assertEqual(event.raw["media_refs"][0]["is_sticker"], True)

    def test_reply_segment_keeps_reply_id_and_text(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "private",
            "message_id": 1994031847,
            "user_id": 550808201,
            "message": [
                {"type": "reply", "data": {"id": "893164517"}},
                {"type": "text", "data": {"text": "引用测试"}},
            ],
        })

        self.assertEqual(event.event_type, EventType.TEXT)
        self.assertEqual(event.text, "引用测试")
        self.assertEqual(event.raw["reply_to_message_id"], "893164517")

    def test_input_status_notice_parses_to_user_composing(self):
        event = parse_onebot_event({
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "input_status",
            "user_id": 550808201,
            "event_type": 1,
            "time": 1782031609,
        })

        self.assertEqual(event.event_type, EventType.USER_COMPOSING)
        self.assertTrue(event.raw["composing"])
        self.assertEqual(event.raw["ttl_ms"], 8000)

    def test_input_status_event_type_zero_parses_to_not_composing(self):
        event = parse_onebot_event({
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "input_status",
            "user_id": 550808201,
            "event_type": 0,
            "time": 1782031609,
        })

        self.assertEqual(event.event_type, EventType.USER_COMPOSING)
        self.assertFalse(event.raw["composing"])

    def test_message_received_clears_visible_composing_meta(self):
        async def scenario():
            async def noop_decision(_ctx):
                return None

            gate = EventGate(on_decision=noop_decision)
            await gate.handle_event(ChatEvent(
                event_id="typing",
                platform="qq",
                user_id="550808201",
                event_type=EventType.USER_COMPOSING,
                timestamp=datetime.now(),
                raw={"composing": True, "ttl_ms": 8000},
            ))
            self.assertTrue(gate.get_user_composing_meta()["active"])

            await gate.handle_event(ChatEvent(
                event_id="msg",
                platform="qq",
                user_id="550808201",
                event_type=EventType.TEXT,
                text="发出来了",
                timestamp=datetime.now(),
                raw={"source": "onebot11"},
            ))
            meta = gate.get_user_composing_meta()
            self.assertFalse(meta["active"])
            self.assertFalse(meta["last_event"]["composing"])
            self.assertEqual(meta["last_event"]["reason"], "message_received")

        asyncio.run(scenario())

    def test_connection_manager_sends_private_msg_and_resolves_echo(self):
        async def scenario():
            manager = OneBotConnectionManager()
            websocket = FakeWebSocket()
            manager._websocket = websocket

            task = asyncio.create_task(manager.send_private_text("550808201", "在"))
            await asyncio.sleep(0)

            self.assertEqual(len(websocket.sent), 1)
            action = websocket.sent[0]
            self.assertEqual(action["action"], "send_private_msg")
            self.assertEqual(action["params"]["user_id"], 550808201)
            self.assertEqual(action["params"]["message"], [
                {"type": "text", "data": {"text": "在"}}
            ])

            manager._resolve_action_response({
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 123},
                "echo": action["echo"],
            })
            response = await task
            self.assertEqual(response["data"]["message_id"], 123)

        asyncio.run(scenario())

    def test_snapshot_target_uses_latest_qq_event(self):
        snapshot = SimpleNamespace(events=[
            {"platform": "webui", "user_id": "user", "raw": {"source": "webui"}},
            {"platform": "qq", "user_id": "550808201", "raw": {"qq_user_id": "550808201"}},
        ])

        self.assertEqual(
            _message_target_from_snapshot(snapshot),
            {"target_platform": "qq", "target_user_id": "550808201"},
        )

    def test_snapshot_target_does_not_leak_old_qq_event_to_webui(self):
        snapshot = SimpleNamespace(events=[
            {"platform": "qq", "user_id": "550808201", "raw": {"qq_user_id": "550808201"}},
            {"platform": "webui", "user_id": "user", "raw": {"source": "webui"}},
        ])

        self.assertEqual(_message_target_from_snapshot(snapshot), {})


if __name__ == "__main__":
    unittest.main()
