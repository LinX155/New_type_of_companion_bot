import asyncio
import unittest
from datetime import datetime

from app.core.events import ChatEvent, EventType
from app.core.group_buffer import GroupChatBuffer, summarize_group_event


class GroupBufferTest(unittest.TestCase):
    def test_group_text_summary_uses_engineering_identity_only(self):
        event = ChatEvent(
            event_id="evt_group_text",
            session_id="qq_group_123456",
            platform="qq",
            user_id="550808201",
            event_type=EventType.TEXT,
            text="群里一句话",
            timestamp=datetime(2026, 6, 30, 23, 1),
            raw={
                "message_type": "group",
                "qq_group_id": "123456",
                "qq_user_id": "550808201",
                "onebot_message_id": "99112233",
                "sender_card": "平台群名片不可信",
                "sender_nickname": "平台昵称不可信",
            },
        )

        summary = summarize_group_event(event)

        self.assertEqual(summary["group_id"], "123456")
        self.assertEqual(summary["sender_qid"], "550808201")
        self.assertEqual(summary["text"], "群里一句话")
        self.assertEqual(summary["media"], [])
        self.assertNotIn("平台群名片不可信", str(summary))
        self.assertNotIn("平台昵称不可信", str(summary))

    def test_group_media_summary_drops_download_urls_and_paths(self):
        event = ChatEvent(
            event_id="evt_group_image",
            session_id="qq_group_123456",
            platform="qq",
            user_id="550808201",
            event_type=EventType.IMAGE,
            text="[图片]",
            timestamp=datetime(2026, 6, 30, 23, 2),
            raw={
                "message_type": "group",
                "qq_group_id": "123456",
                "qq_user_id": "550808201",
                "onebot_message_id": "99112234",
                "media_refs": [
                    {
                        "onebot_message_id": "99112234",
                        "segment_index": 0,
                        "is_sticker": False,
                        "file": "group_image.webp",
                        "url": "https://example.invalid/group.webp?token=secret",
                        "local_path": "D:/qq/cache/group_image.webp",
                        "download_status": "pending",
                    }
                ],
            },
        )

        summary = summarize_group_event(event)

        self.assertEqual(summary["text"], "")
        self.assertEqual(summary["media"], [
            {
                "kind": "image",
                "message_id": "99112234",
                "segment_index": 0,
                "download_status": "pending",
            }
        ])
        self.assertNotIn("group_image.webp", str(summary))
        self.assertNotIn("https://example.invalid", str(summary))
        self.assertNotIn("D:/qq/cache", str(summary))

    def test_group_sticker_summary_is_meme_unknown_without_raw_asset_ref(self):
        event = ChatEvent(
            event_id="evt_group_sticker",
            session_id="qq_group_123456",
            platform="qq",
            user_id="550808201",
            event_type=EventType.STICKER,
            text="[表情]",
            timestamp=datetime(2026, 6, 30, 23, 3),
            raw={
                "message_type": "group",
                "qq_group_id": "123456",
                "qq_user_id": "550808201",
                "onebot_message_id": "99112235",
                "media_refs": [
                    {
                        "onebot_message_id": "99112235",
                        "segment_index": 0,
                        "is_sticker": True,
                        "file": "sticker.webp",
                        "url": "https://example.invalid/sticker.webp",
                    }
                ],
            },
        )

        summary = summarize_group_event(event)

        self.assertEqual(summary["meme"], "unknown")
        self.assertEqual(summary["media"][0]["kind"], "meme")
        self.assertNotIn("sticker.webp", str(summary))
        self.assertNotIn("https://example.invalid", str(summary))

    def test_group_meme_result_updates_internal_entry_but_model_window_stays_compact(self):
        async def scenario():
            buffer = GroupChatBuffer()
            event = ChatEvent(
                event_id="evt_group_sticker",
                session_id="qq_group_123456",
                platform="qq",
                user_id="550808201",
                event_type=EventType.STICKER,
                text="[表情]",
                timestamp=datetime(2026, 6, 30, 23, 3),
                raw={
                    "message_type": "group",
                    "qq_group_id": "123456",
                    "qq_user_id": "550808201",
                    "onebot_message_id": "99112235",
                    "media_refs": [
                        {
                            "onebot_message_id": "99112235",
                            "segment_index": 0,
                            "is_sticker": True,
                            "file": "sticker.webp",
                        }
                    ],
                },
            )
            await buffer.append(event)

            update = await buffer.update_meme_result(
                "qq_group_123456",
                message_id="99112235",
                segment_index=0,
                meme="amused_cat_laugh",
                intake_status="saved",
                media_key="internal-key",
            )

            self.assertTrue(update["updated"])
            self.assertEqual(update["meme"], "amused_cat_laugh")
            latest = buffer.status("qq_group_123456")["latest"]
            self.assertEqual(latest["meme"], "amused_cat_laugh")
            self.assertEqual(latest["media"][0]["media_key"], "internal-key")
            self.assertEqual(latest["media"][0]["meme_intake_status"], "saved")
            model_window = buffer.get_model_window("qq_group_123456")
            self.assertEqual(model_window[0]["meme"], "amused_cat_laugh")
            self.assertEqual(model_window[0]["media"][0]["meme"], "amused_cat_laugh")
            self.assertNotIn("internal-key", str(model_window))
            self.assertNotIn("meme_intake_status", str(model_window))

        asyncio.run(scenario())

    def test_group_buffer_keeps_readonly_window_and_versions(self):
        async def scenario():
            buffer = GroupChatBuffer(max_events=1)
            first = ChatEvent(
                event_id="evt_1",
                session_id="qq_group_123456",
                platform="qq",
                user_id="1",
                event_type=EventType.TEXT,
                text="第一条",
                timestamp=datetime(2026, 6, 30, 23, 4),
                raw={"qq_group_id": "123456", "qq_user_id": "1", "onebot_message_id": "1"},
            )
            second = first.model_copy(update={
                "event_id": "evt_2",
                "user_id": "2",
                "text": "第二条",
                "raw": {"qq_group_id": "123456", "qq_user_id": "2", "onebot_message_id": "2"},
            })

            await buffer.append(first)
            latest = await buffer.append(second)

            self.assertEqual(latest["buffer_version"], 2)
            self.assertEqual(latest["buffered_events"], 1)
            self.assertEqual(buffer.status("qq_group_123456")["buffered_events"], 1)
            self.assertEqual(buffer.get_window("qq_group_123456")[0]["text"], "第二条")

        asyncio.run(scenario())

    def test_model_window_only_uses_confirmed_nickname_mapping(self):
        async def scenario():
            buffer = GroupChatBuffer()
            event = ChatEvent(
                event_id="evt_model_window",
                session_id="qq_group_123456",
                platform="qq",
                user_id="550808201",
                event_type=EventType.TEXT,
                text="这句给模型看",
                timestamp=datetime(2026, 6, 30, 23, 5),
                raw={
                    "message_type": "group",
                    "qq_group_id": "123456",
                    "qq_user_id": "550808201",
                    "onebot_message_id": "99112236",
                    "sender_card": "平台群名片不可信",
                    "sender_nickname": "平台昵称不可信",
                    "reply_to_message_id": "99112200",
                },
            )
            await buffer.append(event)

            anonymous_window = buffer.get_model_window("qq_group_123456")
            named_window = buffer.get_model_window(
                "qq_group_123456",
                qid_to_nickname={"550808201": "记忆确认昵称"},
            )

            self.assertEqual(anonymous_window, [{
                "qid": "550808201",
                "reply_to_message_id": "99112200",
                "text": "这句给模型看",
            }])
            self.assertEqual(named_window[0]["nickname"], "记忆确认昵称")
            self.assertNotIn("平台群名片不可信", str(named_window))
            self.assertNotIn("平台昵称不可信", str(named_window))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
