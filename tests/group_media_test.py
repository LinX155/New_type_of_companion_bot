import unittest
from datetime import datetime

from app.core.events import ChatEvent, EventType
from app.core.group_media import GroupImageUnderstandingCache, group_image_media_key


class GroupImageUnderstandingCacheTest(unittest.TestCase):
    def test_remember_event_keeps_plain_images_only(self):
        cache = GroupImageUnderstandingCache()
        event = ChatEvent(
            event_id="evt_group_media",
            session_id="qq_group_123456",
            platform="qq",
            user_id="550808201",
            event_type=EventType.TEXT,
            text="图来了[图片][表情]",
            timestamp=datetime(2026, 6, 30, 23, 58),
            raw={
                "message_type": "group",
                "qq_group_id": "123456",
                "qq_user_id": "550808201",
                "onebot_message_id": "99112240",
                "media_refs": [
                    {
                        "onebot_message_id": "99112240",
                        "segment_index": 0,
                        "is_sticker": False,
                        "file": "photo.png",
                        "url": "https://example.invalid/photo.png?token=secret",
                    },
                    {
                        "onebot_message_id": "99112240",
                        "segment_index": 1,
                        "is_sticker": True,
                        "file": "sticker.webp",
                        "url": "https://example.invalid/sticker.webp?token=secret",
                    },
                ],
            },
        )

        result = cache.remember_event(event)
        media_key = group_image_media_key("qq_group_123456", "99112240", 0)

        self.assertEqual(result["remembered"], 1)
        self.assertEqual(result["media_keys"], [media_key])
        self.assertEqual(cache.get_ref("qq_group_123456", media_key)["file"], "photo.png")
        self.assertIsNone(cache.get_ref("qq_group_123456", group_image_media_key("qq_group_123456", "99112240", 1)))
        status = cache.status("qq_group_123456")
        self.assertEqual(status["ref_count"], 1)
        self.assertNotIn("example.invalid", str(status))
        self.assertNotIn("photo.png", str(status))

    def test_payload_cache_roundtrip_and_clear(self):
        cache = GroupImageUnderstandingCache()
        key = group_image_media_key("qq_group_123456", "99112241", 0)
        cache.set_payload("qq_group_123456", key, {"status": "completed", "result": {"visible_summary": "一只猫"}})

        self.assertEqual(cache.get_payload("qq_group_123456", key)["result"]["visible_summary"], "一只猫")
        self.assertEqual(cache.status("qq_group_123456")["understanding_count"], 1)

        cache.clear("qq_group_123456")

        self.assertIsNone(cache.get_payload("qq_group_123456", key))


if __name__ == "__main__":
    unittest.main()
