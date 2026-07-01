import unittest
import asyncio
import tempfile
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import app.api.routes as routes
from app.api.routes import _message_target_from_snapshot, _reply_target_from_context
from app.adapters.onebot11.client import OneBotConnectionManager
from app.adapters.onebot11.events import parse_onebot_event
from app.core.event_gate import EventGate, USER_COMPOSING_MAX_BLOCK_SECONDS
from app.core.events import ChatEvent, EventType
from app.core.group_buffer import GroupChatBuffer
from app.core.group_memory import GroupMemoryManager
from app.core.group_repetition import GroupRepetitionDetector


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def _append_async(target, value):
    target.append(value)


class OneBotAdapterTest(unittest.TestCase):
    def test_onebot_client_send_group_text_uses_group_msg_with_reply_segment(self):
        async def scenario():
            manager = OneBotConnectionManager()
            calls = []

            async def fake_request(action, params, timeout=10.0):
                calls.append((action, params, timeout))
                return {"status": "ok", "data": {"message_id": 123}}

            manager.request = fake_request
            response = await manager.send_group_text("123456", "群聊回复", reply_to_message_id="99112233")

            self.assertEqual(response["data"]["message_id"], 123)
            self.assertEqual(calls[0][0], "send_group_msg")
            self.assertEqual(calls[0][1]["group_id"], 123456)
            self.assertEqual(calls[0][1]["message"][0], {"type": "reply", "data": {"id": 99112233}})
            self.assertEqual(calls[0][1]["message"][1], {"type": "text", "data": {"text": "群聊回复"}})

        asyncio.run(scenario())

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
        media_ref = event.raw["media_refs"][0]
        self.assertEqual(media_ref["is_sticker"], False)
        self.assertEqual(media_ref["source"], "qq")
        self.assertEqual(media_ref["qq_user_id"], "550808201")
        self.assertEqual(media_ref["onebot_message_id"], "1411302046")
        self.assertEqual(media_ref["segment_index"], 0)
        self.assertIn("local_path", media_ref)

    def test_private_record_parses_to_audio_event(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "private",
            "message_id": 306163478,
            "user_id": 550808201,
            "message": [
                {
                    "type": "record",
                    "data": {
                        "file": "voice.amr",
                        "path": "D:\\QQ\\Ptt\\voice.amr",
                        "url": "https://example.invalid/voice.amr?token=secret",
                        "file_size": "3463",
                    },
                }
            ],
        })

        self.assertEqual(event.event_type, EventType.AUDIO)
        self.assertEqual(event.text, "[语音]")
        self.assertEqual(event.raw["media_refs"], [])
        audio_ref = event.raw["audio_refs"][0]
        self.assertEqual(audio_ref["segment_type"], "record")
        self.assertEqual(audio_ref["file"], "voice.amr")
        self.assertEqual(audio_ref["url"], "https://example.invalid/voice.amr?token=secret")

    def test_private_video_parses_to_video_event(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "private",
            "message_id": 306163479,
            "user_id": 550808201,
            "message": [
                {
                    "type": "video",
                    "data": {
                        "file": "clip.mp4",
                        "url": "https://example.invalid/clip.mp4?token=secret",
                        "file_size": "2048",
                    },
                }
            ],
        })

        self.assertEqual(event.event_type, EventType.VIDEO)
        self.assertEqual(event.text, "[视频]")
        self.assertEqual(event.raw["media_refs"], [])
        video_ref = event.raw["video_refs"][0]
        self.assertEqual(video_ref["segment_type"], "video")
        self.assertEqual(video_ref["file"], "clip.mp4")
        self.assertEqual(video_ref["url"], "https://example.invalid/clip.mp4?token=secret")

    def test_text_mixed_with_record_and_video_stays_text_with_refs(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "private",
            "message_id": 306163480,
            "user_id": 550808201,
            "message": [
                {"type": "text", "data": {"text": "你听这个"}},
                {"type": "record", "data": {"file": "voice.amr"}},
                {"type": "video", "data": {"file": "clip.mp4", "url": "https://example.invalid/clip.mp4"}},
            ],
        })

        self.assertEqual(event.event_type, EventType.TEXT)
        self.assertEqual(event.text, "你听这个[语音][视频]")
        self.assertEqual(event.raw["audio_refs"][0]["file"], "voice.amr")
        self.assertEqual(event.raw["video_refs"][0]["file"], "clip.mp4")

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

    def test_face_segment_is_builtin_qq_expression_not_downloadable_media(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "private",
            "message_id": 222333444,
            "user_id": 550808201,
            "message": [
                {
                    "type": "face",
                    "data": {
                        "id": "343",
                        "raw": {
                            "faceType": 3,
                            "faceText": "/我方了",
                            "packId": "1",
                            "stickerId": "27",
                        },
                    },
                }
            ],
        })

        self.assertEqual(event.event_type, EventType.STICKER)
        self.assertEqual(event.text, "[QQ表情: face:343]")
        self.assertEqual(event.raw["media_refs"], [])

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

    def test_group_text_message_parses_to_readonly_group_event_without_platform_nickname(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "group_id": 123456,
            "user_id": 550808201,
            "message_id": 99112233,
            "time": 1782031609,
            "sender": {
                "user_id": 550808201,
                "card": "平台群名片不可信",
                "nickname": "平台昵称不可信",
            },
            "message": [{"type": "text", "data": {"text": "群里一句话"}}],
            "raw_message": "群里一句话",
        })

        self.assertIsNotNone(event)
        self.assertEqual(event.session_id, "qq_group_123456")
        self.assertEqual(event.platform, "qq")
        self.assertEqual(event.user_id, "550808201")
        self.assertEqual(event.event_type, EventType.TEXT)
        self.assertEqual(event.text, "群里一句话")
        self.assertEqual(event.raw["message_type"], "group")
        self.assertTrue(event.raw["read_only_group"])
        self.assertEqual(event.raw["qq_group_id"], "123456")
        self.assertEqual(event.raw["qq_user_id"], "550808201")
        self.assertNotIn("onebot", event.raw)
        self.assertNotIn("sender", event.raw)
        self.assertNotIn("平台群名片不可信", str(event.raw))
        self.assertNotIn("平台昵称不可信", str(event.raw))

    def test_group_at_segment_records_bot_mention_without_qid_in_text(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "group",
            "self_id": 999001,
            "group_id": 123456,
            "user_id": 550808201,
            "message_id": 99112239,
            "message": [
                {"type": "at", "data": {"qq": "999001"}},
                {"type": "text", "data": {"text": " 这个怎么看"}},
            ],
            "raw_message": "[CQ:at,qq=999001] 这个怎么看",
        })

        self.assertEqual(event.event_type, EventType.TEXT)
        self.assertTrue(event.raw["mentions_bot"])
        self.assertEqual(event.raw["at_user_ids"], ["999001"])
        self.assertEqual(event.raw["qq_self_id"], "999001")
        self.assertNotIn("999001", event.text)

    def test_group_image_keeps_engineering_refs_without_sender_card(self):
        event = parse_onebot_event({
            "post_type": "message",
            "message_type": "group",
            "group_id": 123456,
            "user_id": 550808201,
            "message_id": 99112234,
            "sender": {"card": "不要保存", "nickname": "也不要保存"},
            "message": [
                {
                    "type": "image",
                    "data": {
                        "file": "group_image.webp",
                        "url": "https://example.invalid/group.webp",
                        "sub_type": 0,
                    },
                }
            ],
        })

        self.assertEqual(event.event_type, EventType.IMAGE)
        self.assertEqual(event.text, "[图片]")
        media_ref = event.raw["media_refs"][0]
        self.assertEqual(media_ref["qq_group_id"], "123456")
        self.assertEqual(media_ref["qq_user_id"], "550808201")
        self.assertEqual(media_ref["onebot_message_id"], "99112234")
        self.assertFalse(media_ref["is_sticker"])
        self.assertNotIn("不要保存", str(event.raw))
        self.assertNotIn("也不要保存", str(event.raw))

    def test_onebot_group_payload_is_recorded_readonly_without_runtime_dispatch(self):
        async def scenario():
            recorded = []
            emitted = []
            states = []
            originals = {
                "_record_incoming_event": routes._record_incoming_event,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_emit_state": routes._emit_state,
                "_runtime_for_event": routes._runtime_for_event,
                "_ensure_group_reply_scheduler": routes._ensure_group_reply_scheduler,
                "init_gate": routes.init_gate,
                "group_chat_buffer": routes.group_chat_buffer,
            }

            class FakeGroupScheduler:
                async def on_group_event(self, _event, _group_window):
                    return {"status": "skipped", "reason": "test"}

            def fake_record(event):
                recorded.append(event)

            async def fake_emit(reason, event=None, session_id=None):
                emitted.append((reason, event, session_id))

            async def fake_state(data):
                states.append(data)

            def fail_runtime(_event):
                raise AssertionError("group readonly event must not enter runtime")

            def fail_init_gate():
                raise AssertionError("group readonly event must not initialize private runtime")

            routes._record_incoming_event = fake_record
            routes._emit_conversation_changed = fake_emit
            routes._emit_state = fake_state
            routes._runtime_for_event = fail_runtime
            routes._ensure_group_reply_scheduler = lambda: FakeGroupScheduler()
            routes.init_gate = fail_init_gate
            routes.group_chat_buffer = GroupChatBuffer()
            try:
                await routes.handle_onebot_payload({
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 123456,
                    "user_id": 550808201,
                    "message_id": 99112235,
                    "sender": {"card": "群名片", "nickname": "群昵称"},
                    "message": [{"type": "text", "data": {"text": "只读观察"}}],
                })
            finally:
                routes._record_incoming_event = originals["_record_incoming_event"]
                routes._emit_conversation_changed = originals["_emit_conversation_changed"]
                routes._emit_state = originals["_emit_state"]
                routes._runtime_for_event = originals["_runtime_for_event"]
                routes._ensure_group_reply_scheduler = originals["_ensure_group_reply_scheduler"]
                routes.init_gate = originals["init_gate"]
                routes.group_chat_buffer = originals["group_chat_buffer"]

            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0].session_id, "qq_group_123456")
            self.assertEqual(recorded[0].text, "只读观察")
            self.assertEqual(emitted[0][0], "incoming_group_event")
            self.assertEqual(states[0]["result"], "group_buffered")
            self.assertEqual(states[0]["group_buffer"]["buffered_events"], 1)
            self.assertNotIn("群名片", str(states[0]["group_buffer"]))
            self.assertNotIn("群昵称", str(states[0]["group_buffer"]))

        asyncio.run(scenario())

    def test_onebot_group_mem_command_updates_group_memory_without_reply_scheduler(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                recorded = []
                emitted = []
                messages = []
                states = []
                sends = []
                command_logs = []
                manager = GroupMemoryManager(tmpdir)
                originals = {
                    "_record_incoming_event": routes._record_incoming_event,
                    "_record_group_memory_command_response": routes._record_group_memory_command_response,
                    "_group_internal_llm_for_session": routes._group_internal_llm_for_session,
                    "_emit_conversation_changed": routes._emit_conversation_changed,
                    "_emit_state": routes._emit_state,
                    "_emit_message": routes._emit_message,
                    "_runtime_for_event": routes._runtime_for_event,
                    "_ensure_group_reply_scheduler": routes._ensure_group_reply_scheduler,
                    "_send_group_reply_candidate": routes._send_group_reply_candidate,
                    "init_gate": routes.init_gate,
                    "group_chat_buffer": routes.group_chat_buffer,
                    "group_memory_manager": routes.group_memory_manager,
                }

                def fail_scheduler():
                    raise AssertionError("group /mem command must not enter group reply scheduler")

                def fail_runtime(_event):
                    raise AssertionError("group /mem command must not enter private runtime")

                def fail_init_gate():
                    raise AssertionError("group /mem command must not initialize private runtime")

                def fake_record(event):
                    recorded.append(event)

                def fake_command_log(event, action, response_text, success, payload, **kwargs):
                    command_logs.append((event, action, response_text, success, payload, kwargs))

                async def fake_emit(reason, event=None, session_id=None):
                    emitted.append((reason, event, session_id))

                async def fake_state(data):
                    states.append(data)

                async def fake_message(data):
                    messages.append(data)

                async def fake_send(session_id, trigger, decision):
                    sends.append((session_id, trigger, decision))
                    return {"status": "sent", "test": True}

                class FakeGroupLLM:
                    api_key = "test-key"

                    async def chat_completion(self, **_kwargs):
                        return json.dumps({
                            "group_memory_md": """# 群聊记忆

## 群友身份
- 550808201: 小夏

## 共同记忆

## 个人相关记忆
- <550808201><2026-07-01><群聊/mem本人><昵称=小夏>
"""
                        }, ensure_ascii=False)

                    def get_last_call_debug(self):
                        return {"llm_runtime_id": "test-runtime", "client_scope": "internal_session"}

                routes._record_incoming_event = fake_record
                routes._record_group_memory_command_response = fake_command_log
                routes._group_internal_llm_for_session = lambda _sid: FakeGroupLLM()
                routes._emit_conversation_changed = fake_emit
                routes._emit_state = fake_state
                routes._emit_message = fake_message
                routes._runtime_for_event = fail_runtime
                routes._ensure_group_reply_scheduler = fail_scheduler
                routes._send_group_reply_candidate = fake_send
                routes.init_gate = fail_init_gate
                routes.group_chat_buffer = GroupChatBuffer()
                routes.group_memory_manager = manager
                try:
                    await routes.handle_onebot_payload({
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 123456,
                        "user_id": 550808201,
                        "message_id": 99112240,
                        "sender": {"card": "群名片", "nickname": "群昵称"},
                        "message": [{"type": "text", "data": {"text": "/mem 我叫小夏"}}],
                    })
                finally:
                    routes._record_incoming_event = originals["_record_incoming_event"]
                    routes._record_group_memory_command_response = originals["_record_group_memory_command_response"]
                    routes._group_internal_llm_for_session = originals["_group_internal_llm_for_session"]
                    routes._emit_conversation_changed = originals["_emit_conversation_changed"]
                    routes._emit_state = originals["_emit_state"]
                    routes._emit_message = originals["_emit_message"]
                    routes._runtime_for_event = originals["_runtime_for_event"]
                    routes._ensure_group_reply_scheduler = originals["_ensure_group_reply_scheduler"]
                    routes._send_group_reply_candidate = originals["_send_group_reply_candidate"]
                    routes.init_gate = originals["init_gate"]
                    routes.group_chat_buffer = originals["group_chat_buffer"]
                    routes.group_memory_manager = originals["group_memory_manager"]

                self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {"550808201": "小夏"})
                self.assertEqual(len(recorded), 1)
                self.assertEqual(recorded[0].event_type, EventType.COMMAND_MEM)
                self.assertEqual(command_logs[0][1], "group.command.mem")
                self.assertTrue(command_logs[0][3])
                self.assertEqual(messages[0]["target_platform"], "qq_group")
                self.assertEqual(messages[0]["target_group_id"], "123456")
                self.assertEqual(sends[0][0], "qq_group_123456")
                self.assertEqual(sends[0][2]["final_text"], "已把你的群聊昵称记住了。")
                self.assertEqual(states[-1]["group_reply_trigger"]["reason"], "group_memory_command")
                self.assertTrue(states[-1]["group_memory_command"]["success"])
                self.assertIsNone(states[-1]["group_memory_observation"])

        asyncio.run(scenario())

    def test_onebot_group_repetition_triggers_repeat_without_llm_scheduler(self):
        async def scenario():
            states = []
            sends = []
            buffer = GroupChatBuffer()
            await buffer.append(_group_text_event("evt_repeat_1", "10001", "99112251", "复读一下"))
            await buffer.append(_group_text_event("evt_repeat_2", "10002", "99112252", "复读一下"))
            detector = GroupRepetitionDetector(config={"cooldown_seconds": 0, "dedupe_window_seconds": 3600})
            originals = {
                "_record_incoming_event": routes._record_incoming_event,
                "_record_group_repetition_event": routes._record_group_repetition_event,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_emit_state": routes._emit_state,
                "_runtime_for_event": routes._runtime_for_event,
                "_ensure_group_reply_scheduler": routes._ensure_group_reply_scheduler,
                "_send_group_reply_candidate": routes._send_group_reply_candidate,
                "_maybe_update_group_activity": routes._maybe_update_group_activity,
                "load_settings": routes.load_settings,
                "init_gate": routes.init_gate,
                "group_chat_buffer": routes.group_chat_buffer,
                "group_repetition_detector": routes.group_repetition_detector,
            }

            def fail_scheduler():
                raise AssertionError("selected group repetition must not enter LLM scheduler")

            async def fake_send(session_id, trigger, decision):
                sends.append((session_id, trigger, decision))
                return {"status": "sent", "test": True}

            routes._record_incoming_event = lambda _event: None
            routes._record_group_repetition_event = lambda _event, _payload: None
            routes._emit_conversation_changed = lambda *args, **kwargs: asyncio.sleep(0)
            routes._emit_state = lambda data: _append_async(states, data)
            routes._runtime_for_event = lambda _event: (_ for _ in ()).throw(
                AssertionError("group repetition must not enter private runtime")
            )
            routes._ensure_group_reply_scheduler = fail_scheduler
            routes._send_group_reply_candidate = fake_send
            routes._maybe_update_group_activity = lambda _session_id: {"status": "skipped", "reason": "test"}
            routes.load_settings = lambda: {
                "group_chat_send": {
                    "enabled": True,
                    "groups": {
                        "123456": {
                            "observe_only": False,
                            "allow_repetition": True,
                        }
                    },
                    "min_interval_seconds": 0,
                }
            }
            routes.init_gate = lambda: (_ for _ in ()).throw(
                AssertionError("group repetition must not initialize private runtime")
            )
            routes.group_chat_buffer = buffer
            routes.group_repetition_detector = detector
            try:
                await routes.handle_onebot_payload({
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 123456,
                    "user_id": 10003,
                    "message_id": 99112253,
                    "message": [{"type": "text", "data": {"text": "复读一下"}}],
                })
            finally:
                routes._record_incoming_event = originals["_record_incoming_event"]
                routes._record_group_repetition_event = originals["_record_group_repetition_event"]
                routes._emit_conversation_changed = originals["_emit_conversation_changed"]
                routes._emit_state = originals["_emit_state"]
                routes._runtime_for_event = originals["_runtime_for_event"]
                routes._ensure_group_reply_scheduler = originals["_ensure_group_reply_scheduler"]
                routes._send_group_reply_candidate = originals["_send_group_reply_candidate"]
                routes._maybe_update_group_activity = originals["_maybe_update_group_activity"]
                routes.load_settings = originals["load_settings"]
                routes.init_gate = originals["init_gate"]
                routes.group_chat_buffer = originals["group_chat_buffer"]
                routes.group_repetition_detector = originals["group_repetition_detector"]

            self.assertEqual(len(sends), 1)
            self.assertEqual(sends[0][0], "qq_group_123456")
            self.assertEqual(sends[0][1]["reason"], "group_repetition")
            self.assertEqual(sends[0][2]["final_text"], "复读一下")
            self.assertEqual(states[-1]["group_repetition"]["status"], "selected")
            self.assertEqual(states[-1]["group_reply_trigger"]["reason"], "group_repetition_triggered")

        asyncio.run(scenario())

    def test_onebot_group_clear_self_identity_is_observed_without_command_response(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmpdir:
                states = []
                messages = []
                manager = GroupMemoryManager(tmpdir)
                originals = {
                    "_record_incoming_event": routes._record_incoming_event,
                    "_record_group_memory_observation": routes._record_group_memory_observation,
                    "_emit_conversation_changed": routes._emit_conversation_changed,
                    "_emit_state": routes._emit_state,
                    "_emit_message": routes._emit_message,
                    "_runtime_for_event": routes._runtime_for_event,
                    "_ensure_group_reply_scheduler": routes._ensure_group_reply_scheduler,
                    "init_gate": routes.init_gate,
                    "group_chat_buffer": routes.group_chat_buffer,
                    "group_memory_manager": routes.group_memory_manager,
                }

                class FakeGroupScheduler:
                    async def on_group_event(self, _event, _group_window):
                        return {"status": "skipped", "reason": "test"}

                routes._record_incoming_event = lambda _event: None
                routes._record_group_memory_observation = lambda _event, _payload: None
                routes._emit_conversation_changed = lambda *args, **kwargs: asyncio.sleep(0)
                routes._emit_state = lambda data: _append_async(states, data)
                routes._emit_message = lambda data: _append_async(messages, data)
                routes._runtime_for_event = lambda _event: (_ for _ in ()).throw(
                    AssertionError("group identity observation must not enter private runtime")
                )
                routes._ensure_group_reply_scheduler = lambda: FakeGroupScheduler()
                routes.init_gate = lambda: (_ for _ in ()).throw(
                    AssertionError("group identity observation must not initialize private runtime")
                )
                routes.group_chat_buffer = GroupChatBuffer()
                routes.group_memory_manager = manager
                try:
                    await routes.handle_onebot_payload({
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 123456,
                        "user_id": 550808201,
                        "message_id": 99112241,
                        "message": [{"type": "text", "data": {"text": "我叫小夏"}}],
                    })
                finally:
                    routes._record_incoming_event = originals["_record_incoming_event"]
                    routes._record_group_memory_observation = originals["_record_group_memory_observation"]
                    routes._emit_conversation_changed = originals["_emit_conversation_changed"]
                    routes._emit_state = originals["_emit_state"]
                    routes._emit_message = originals["_emit_message"]
                    routes._runtime_for_event = originals["_runtime_for_event"]
                    routes._ensure_group_reply_scheduler = originals["_ensure_group_reply_scheduler"]
                    routes.init_gate = originals["init_gate"]
                    routes.group_chat_buffer = originals["group_chat_buffer"]
                    routes.group_memory_manager = originals["group_memory_manager"]

                self.assertEqual(manager.qid_to_nickname("qq_group_123456"), {})
                self.assertEqual(messages, [])
                self.assertEqual(states[-1]["group_memory_observation"]["status"], "identity_candidate")
                self.assertEqual(states[-1]["group_memory_observation"]["nickname"], "小夏")

        asyncio.run(scenario())

    def test_onebot_group_sticker_is_enqueued_for_background_meme_intake_only(self):
        async def scenario():
            class FakeMediaQueue:
                def __init__(self):
                    self.jobs = []

                def enqueue(self, job):
                    self.jobs.append(job)
                    return True

            recorded = []
            states = []
            queue = FakeMediaQueue()
            originals = {
                "_record_incoming_event": routes._record_incoming_event,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_emit_state": routes._emit_state,
                "_runtime_for_event": routes._runtime_for_event,
                "_ensure_media_job_queue": routes._ensure_media_job_queue,
                "_ensure_group_reply_scheduler": routes._ensure_group_reply_scheduler,
                "init_gate": routes.init_gate,
                "group_chat_buffer": routes.group_chat_buffer,
            }

            class FakeGroupScheduler:
                async def on_group_event(self, _event, _group_window):
                    return {"status": "skipped", "reason": "test"}

            def fake_record(event):
                recorded.append(event)

            async def fake_emit(_reason, event=None, session_id=None):
                return None

            async def fake_state(data):
                states.append(data)

            def fail_runtime(_event):
                raise AssertionError("group sticker must not enter private runtime")

            def fail_init_gate():
                raise AssertionError("group sticker must not initialize private runtime")

            routes._record_incoming_event = fake_record
            routes._emit_conversation_changed = fake_emit
            routes._emit_state = fake_state
            routes._runtime_for_event = fail_runtime
            routes._ensure_media_job_queue = lambda: queue
            routes._ensure_group_reply_scheduler = lambda: FakeGroupScheduler()
            routes.init_gate = fail_init_gate
            routes.group_chat_buffer = GroupChatBuffer()
            try:
                await routes.handle_onebot_payload({
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 123456,
                    "user_id": 550808201,
                    "message_id": 99112237,
                    "sender": {"card": "群名片", "nickname": "群昵称"},
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "summary": "[动画表情]",
                                "sub_type": 1,
                                "file": "sticker.webp",
                                "url": "https://example.invalid/sticker.webp",
                            },
                        }
                    ],
                })
            finally:
                routes._record_incoming_event = originals["_record_incoming_event"]
                routes._emit_conversation_changed = originals["_emit_conversation_changed"]
                routes._emit_state = originals["_emit_state"]
                routes._runtime_for_event = originals["_runtime_for_event"]
                routes._ensure_media_job_queue = originals["_ensure_media_job_queue"]
                routes._ensure_group_reply_scheduler = originals["_ensure_group_reply_scheduler"]
                routes.init_gate = originals["init_gate"]
                routes.group_chat_buffer = originals["group_chat_buffer"]

            self.assertEqual(len(recorded), 1)
            self.assertEqual(len(queue.jobs), 1)
            self.assertTrue(queue.jobs[0].is_sticker)
            self.assertEqual(queue.jobs[0].session_id, "qq_group_123456")
            self.assertEqual(queue.jobs[0].snapshot_id, 0)
            self.assertIn("group_window", queue.jobs[0].context_text)
            self.assertNotIn("群名片", queue.jobs[0].context_text)
            self.assertNotIn("群昵称", queue.jobs[0].context_text)
            self.assertEqual(states[0]["group_meme_jobs"]["queued"], 1)
            self.assertEqual(states[0]["group_buffer"]["latest"]["meme"], "unknown")

        asyncio.run(scenario())

    def test_group_meme_intake_payload_updates_readonly_buffer(self):
        async def scenario():
            states = []
            originals = {
                "_record_media_job_payloads": routes._record_media_job_payloads,
                "_emit_state": routes._emit_state,
                "group_chat_buffer": routes.group_chat_buffer,
            }

            async def fake_state(data):
                states.append(data)

            routes._record_media_job_payloads = lambda payloads, job: None
            routes._emit_state = fake_state
            routes.group_chat_buffer = GroupChatBuffer()
            try:
                event = parse_onebot_event({
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 123456,
                    "user_id": 550808201,
                    "message_id": 99112238,
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "summary": "[动画表情]",
                                "sub_type": 1,
                                "file": "sticker.webp",
                            },
                        }
                    ],
                })
                await routes.group_chat_buffer.append(event)
                job = SimpleNamespace(
                    session_id="qq_group_123456",
                    job_id="media_test",
                    media_key="qq_group_123456:evt:0:sticker.webp",
                    snapshot_id=0,
                    buffer_version=1,
                    source_job_id="group_meme:evt",
                    media_ref={
                        "onebot_message_id": "99112238",
                        "segment_index": 0,
                        "is_sticker": True,
                    },
                )
                await routes._record_background_media_payloads(
                    [
                        {
                            "internal_event_harness": "meme_intake_result",
                            "media_key": job.media_key,
                            "session_id": "qq_group_123456",
                            "status": "saved",
                            "media_ref": {
                                "onebot_message_id": "99112238",
                                "segment_index": 0,
                                "is_sticker": True,
                            },
                            "save_result": {
                                "status": "saved",
                                "file_stem": "amused_cat_laugh",
                            },
                        }
                    ],
                    job,
                )
                model_window = routes.group_chat_buffer.get_model_window("qq_group_123456")
            finally:
                routes._record_media_job_payloads = originals["_record_media_job_payloads"]
                routes._emit_state = originals["_emit_state"]
                routes.group_chat_buffer = originals["group_chat_buffer"]

            self.assertEqual(model_window[0]["meme"], "amused_cat_laugh")
            self.assertEqual(states[0]["group_meme_update"]["updated"], True)
            self.assertEqual(states[0]["group_meme_update"]["items"][0]["meme"], "amused_cat_laugh")
            self.assertNotIn("<meme:", str(model_window))

        asyncio.run(scenario())

    def test_scheduler_session_ids_exclude_group_sessions(self):
        original_list_sessions = routes._list_sessions
        routes._list_sessions = lambda: [
            {"session_id": "qq_private_10001"},
            {"session_id": "qq_group_123456"},
            {"session_id": "webui_default"},
        ]
        try:
            self.assertEqual(routes._scheduler_session_ids(), ["qq_private_10001", "webui_default"])
        finally:
            routes._list_sessions = original_list_sessions

    def test_group_scheduler_session_ids_include_only_group_sessions(self):
        original_list_sessions = routes._list_sessions
        original_group_buffer_sessions = routes._group_buffer_session_ids
        routes._list_sessions = lambda: [
            {"session_id": "qq_private_10001"},
            {"session_id": "qq_group_123456"},
            {"session_id": "webui_default"},
        ]
        routes._group_buffer_session_ids = lambda: ["qq_group_777888"]
        try:
            self.assertEqual(routes._group_scheduler_session_ids(), ["qq_group_123456", "qq_group_777888"])
        finally:
            routes._list_sessions = original_list_sessions
            routes._group_buffer_session_ids = original_group_buffer_sessions

    def test_group_status_uses_readonly_buffer_without_runtime_dispatch(self):
        async def scenario():
            original_buffer = routes.group_chat_buffer
            original_runtime_for_session = routes._runtime_for_session

            def fail_runtime(_sid):
                raise AssertionError("group status must not initialize private runtime")

            routes.group_chat_buffer = GroupChatBuffer()
            routes._runtime_for_session = fail_runtime
            try:
                event = parse_onebot_event({
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 123456,
                    "user_id": 550808201,
                    "message_id": 99112236,
                    "message": [{"type": "text", "data": {"text": "状态页只读"}}],
                })
                await routes.group_chat_buffer.append(event)
                status = await routes.get_status("qq_group_123456")
            finally:
                routes.group_chat_buffer = original_buffer
                routes._runtime_for_session = original_runtime_for_session

            self.assertEqual(status["status"], "GROUP_CHAT")
            self.assertEqual(status["gate"]["pending_job_id"], None)
            self.assertEqual(status["group_buffer"]["buffered_events"], 1)
            self.assertEqual(status["group_buffer"]["latest"]["sender_qid"], "550808201")
            self.assertEqual(status["group_buffer"]["model_window"][0]["qid"], "550808201")
            self.assertEqual(status["group_buffer"]["model_window"][0]["text"], "状态页只读")
            self.assertIn("group_repetition", status)
            self.assertIn("group_activity", status)

        asyncio.run(scenario())

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

    def test_poke_notice_parses_to_nudge_event(self):
        event = parse_onebot_event({
            "self_id": 3665612616,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "user_id": 550808201,
            "target_id": 3665612616,
            "time": 1782031609,
        })

        self.assertIsNotNone(event)
        self.assertEqual(event.platform, "qq")
        self.assertEqual(event.user_id, "550808201")
        self.assertEqual(event.event_type, EventType.NUDGE)
        self.assertEqual(event.text, "拍了拍你")
        self.assertEqual(event.raw["nudge_type"], "poke")
        self.assertEqual(event.raw["qq_user_id"], "550808201")
        self.assertEqual(event.raw["qq_target_id"], "3665612616")

    def test_self_initiated_poke_notice_is_ignored(self):
        event = parse_onebot_event({
            "self_id": 3665612616,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "user_id": 3665612616,
            "target_id": 550808201,
            "time": 1782031609,
        })

        self.assertIsNone(event)

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

    def test_input_status_refresh_keeps_raw_composing_until_send_gate_decides(self):
        async def scenario():
            async def noop_decision(_ctx):
                return None

            gate = EventGate(on_decision=noop_decision)
            await gate.handle_event(ChatEvent(
                event_id="typing-1",
                platform="qq",
                user_id="550808201",
                event_type=EventType.USER_COMPOSING,
                timestamp=datetime.now(),
                raw={"composing": True, "ttl_ms": 8000},
            ))
            first_meta = gate.get_user_composing_meta()
            self.assertTrue(first_meta["active"])
            self.assertEqual(first_meta["last_event"]["send_max_wait_seconds"], USER_COMPOSING_MAX_BLOCK_SECONDS)

            gate._user_composing_started_at = datetime.now() - timedelta(
                seconds=USER_COMPOSING_MAX_BLOCK_SECONDS + 1
            )
            await gate.handle_event(ChatEvent(
                event_id="typing-2",
                platform="qq",
                user_id="550808201",
                event_type=EventType.USER_COMPOSING,
                timestamp=datetime.now(),
                raw={"composing": True, "ttl_ms": 8000},
            ))
            refreshed_meta = gate.get_user_composing_meta()
            self.assertTrue(refreshed_meta["active"])
            self.assertEqual(refreshed_meta["last_event"]["reason"], "input_status")

            await gate.handle_event(ChatEvent(
                event_id="msg",
                platform="qq",
                user_id="550808201",
                event_type=EventType.TEXT,
                text="真发出来了",
                timestamp=datetime.now(),
                raw={"source": "onebot11"},
            ))
            await gate.handle_event(ChatEvent(
                event_id="typing-3",
                platform="qq",
                user_id="550808201",
                event_type=EventType.USER_COMPOSING,
                timestamp=datetime.now(),
                raw={"composing": True, "ttl_ms": 8000},
            ))
            self.assertTrue(gate.get_user_composing_meta()["active"])

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

    def test_connection_manager_can_prefix_reply_segment(self):
        async def scenario():
            manager = OneBotConnectionManager()
            websocket = FakeWebSocket()
            manager._websocket = websocket

            task = asyncio.create_task(
                manager.send_private_text("550808201", "看到了", reply_to_message_id="893164517")
            )
            await asyncio.sleep(0)

            action = websocket.sent[0]
            self.assertEqual(action["action"], "send_private_msg")
            self.assertEqual(action["params"]["message"], [
                {"type": "reply", "data": {"id": 893164517}},
                {"type": "text", "data": {"text": "看到了"}},
            ])

            manager._resolve_action_response({
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 456},
                "echo": action["echo"],
            })
            response = await task
            self.assertEqual(response["data"]["message_id"], 456)

        asyncio.run(scenario())

    def test_media_followup_reply_target_uses_original_image_message_once(self):
        ctx = SimpleNamespace(
            internal_source="media_followup",
            internal_payload={
                "media_ref": {
                    "onebot_message_id": "1411302046",
                    "segment_type": "image",
                },
            },
        )

        self.assertEqual(
            _reply_target_from_context(ctx, send_index=0),
            {"reply_to_message_id": "1411302046"},
        )
        self.assertEqual(_reply_target_from_context(ctx, send_index=1), {})

    def test_connection_manager_sets_input_status(self):
        async def scenario():
            manager = OneBotConnectionManager()
            websocket = FakeWebSocket()
            manager._websocket = websocket

            task = asyncio.create_task(manager.set_input_status("550808201", 1))
            await asyncio.sleep(0)

            self.assertEqual(len(websocket.sent), 1)
            action = websocket.sent[0]
            self.assertEqual(action["action"], "set_input_status")
            self.assertEqual(action["params"], {
                "user_id": 550808201,
                "event_type": 1,
            })

            manager._resolve_action_response({
                "status": "ok",
                "retcode": 0,
                "data": None,
                "echo": action["echo"],
            })
            response = await task
            self.assertEqual(response["retcode"], 0)

        asyncio.run(scenario())

    def test_connection_manager_exposes_download_file_stream_action(self):
        async def scenario():
            manager = OneBotConnectionManager()
            websocket = FakeWebSocket()
            manager._websocket = websocket

            task = asyncio.create_task(manager.download_file_stream("abc-file-id"))
            await asyncio.sleep(0)

            self.assertEqual(len(websocket.sent), 1)
            action = websocket.sent[0]
            self.assertEqual(action["action"], "download_file_stream")
            self.assertEqual(action["params"], {"file_id": "abc-file-id"})

            manager._resolve_action_response({
                "status": "ok",
                "retcode": 0,
                "data": {"path": "x"},
                "echo": action["echo"],
            })
            response = await task
            self.assertEqual(response["retcode"], 0)

        asyncio.run(scenario())

    def test_connection_manager_get_record_action(self):
        async def scenario():
            manager = OneBotConnectionManager()
            websocket = FakeWebSocket()
            manager._websocket = websocket

            task = asyncio.create_task(manager.get_record("voice.amr", out_format="mp3"))
            await asyncio.sleep(0)

            action = websocket.sent[0]
            self.assertEqual(action["action"], "get_record")
            self.assertEqual(action["params"], {"file": "voice.amr", "out_format": "mp3"})

            manager._resolve_action_response({
                "status": "ok",
                "retcode": 0,
                "data": {"path": "voice.mp3"},
                "echo": action["echo"],
            })
            response = await task
            self.assertEqual(response["data"]["path"], "voice.mp3")

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


def _group_text_event(event_id: str, qid: str, message_id: str, text: str) -> ChatEvent:
    return ChatEvent(
        event_id=event_id,
        session_id="qq_group_123456",
        platform="qq",
        user_id=qid,
        event_type=EventType.TEXT,
        text=text,
        timestamp=datetime(2026, 6, 30, 23, 30),
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
