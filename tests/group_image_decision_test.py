import asyncio
import json
import unittest
from datetime import datetime

import app.api.routes as routes
from app.core.events import ChatEvent, EventType
from app.core.group_media import GroupImageUnderstandingCache


class FakeGroupVisionQueue:
    def __init__(self):
        self.jobs = []

    async def process_inline_image_for_prompt(self, job):
        self.jobs.append(job)
        payload = {
            "internal_event_harness": "image_understanding_result",
            "media_key": job.media_key,
            "session_id": job.session_id,
            "status": "completed",
            "is_sticker": False,
            "media_ref": {
                "onebot_message_id": job.media_ref.get("onebot_message_id"),
                "segment_index": job.media_ref.get("segment_index"),
            },
            "result": {
                "kind": "photo",
                "visible_summary": "一只猫趴在键盘旁边",
                "relation_to_context": "群友在分享猫打扰工作的瞬间",
                "user_intent": "分享一个好笑的日常画面",
                "desired_response": "轻松接梗",
                "reply_style": "playful",
                "confidence": "high",
            },
            "instruction": "用户发的是普通图片，第一语义是分享。",
        }
        return payload, [payload]


class FakeGroupLLM:
    def __init__(self):
        self.messages = []

    async def chat_completion(self, messages, temperature=0.7, max_tokens=None, stream=False):
        self.messages.append(messages)
        serialized = json.dumps(messages, ensure_ascii=False)
        if "一只猫趴在键盘旁边" in serialized:
            return "这猫看起来已经接管键盘了"
        return "WAIT"

    def get_cache_debug(self):
        return {"client_scope": "internal_session", "cache_affinity_enabled": False}


class GroupImageDecisionTest(unittest.TestCase):
    def test_group_decision_injects_image_understanding_and_keeps_image_reply_target(self):
        async def scenario():
            fake_queue = FakeGroupVisionQueue()
            fake_llm = FakeGroupLLM()
            records = []
            originals = {
                "group_image_cache": routes.group_image_cache,
                "_ensure_media_job_queue": routes._ensure_media_job_queue,
                "_internal_llm_for_session": routes._internal_llm_for_session,
                "_record_media_job_payloads": routes._record_media_job_payloads,
                "_record_group_image_understanding_log": routes._record_group_image_understanding_log,
                "_record_group_reply_decision_log": routes._record_group_reply_decision_log,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_llm_finished": routes._emit_llm_finished,
                "load_settings": routes.load_settings,
            }

            async def noop_async(*_args, **_kwargs):
                return None

            routes.group_image_cache = GroupImageUnderstandingCache()
            routes._ensure_media_job_queue = lambda: fake_queue
            routes._internal_llm_for_session = lambda _sid: fake_llm
            routes._record_media_job_payloads = lambda payloads, job: records.append(("media", payloads, job.media_key))
            routes._record_group_image_understanding_log = lambda sid, payload: records.append(("image", sid, payload))
            routes._record_group_reply_decision_log = lambda sid, **kwargs: records.append(("decision", sid, kwargs))
            routes._emit_llm_started = noop_async
            routes._emit_llm_finished = noop_async
            routes.load_settings = lambda: {
                "group_chat_send": {
                    "enabled": True,
                    "groups": {
                        "123456": {
                            "observe_only": False,
                            "allow_roll_reply": True,
                        }
                    },
                }
            }
            try:
                event = _group_image_event()
                routes.group_image_cache.remember_event(event)
                model_window = [{
                    "qid": "550808201",
                    "media": [{"kind": "image", "message_id": "99112242", "segment_index": 0}],
                }]
                result = await routes._run_group_reply_decision(
                    "qq_group_123456",
                    {
                        "trigger_id": "group_roll_test",
                        "reason": "roll",
                        "source_message_id": "99112242",
                        "source_buffer_version": 1,
                        "request_buffer_version": 1,
                    },
                    model_window,
                )
            finally:
                routes.group_image_cache = originals["group_image_cache"]
                routes._ensure_media_job_queue = originals["_ensure_media_job_queue"]
                routes._internal_llm_for_session = originals["_internal_llm_for_session"]
                routes._record_media_job_payloads = originals["_record_media_job_payloads"]
                routes._record_group_image_understanding_log = originals["_record_group_image_understanding_log"]
                routes._record_group_reply_decision_log = originals["_record_group_reply_decision_log"]
                routes._emit_llm_started = originals["_emit_llm_started"]
                routes._emit_llm_finished = originals["_emit_llm_finished"]
                routes.load_settings = originals["load_settings"]

            self.assertEqual(result["status"], "candidate_ready_not_sent")
            self.assertEqual(result["reply_to_message_id"], "99112242")
            self.assertEqual(result["final_text"], "这猫看起来已经接管键盘了")
            self.assertEqual(len(fake_queue.jobs), 1)
            self.assertIn("一只猫趴在键盘旁边", json.dumps(fake_llm.messages[-1], ensure_ascii=False))
            self.assertTrue(result["image_understanding"][0]["media_cache_write"])
            self.assertTrue(any(item[0] == "media" for item in records))

        asyncio.run(scenario())

    def test_text_latest_does_not_force_image_quote(self):
        result = routes._select_group_reply_target(
            trigger={"reason": "roll"},
            model_window=[
                {
                    "qid": "550808201",
                    "media": [
                        {
                            "kind": "image",
                            "message_id": "99112243",
                            "segment_index": 0,
                            "image": {"status": "completed", "reply_target_candidate": True},
                        }
                    ],
                },
                {"qid": "1057552839", "text": "我觉得重点不是图"},
            ],
            should_send=True,
        )

        self.assertIsNone(result)

    def test_image_with_caption_keeps_image_reply_target(self):
        result = routes._select_group_reply_target(
            trigger={"reason": "roll"},
            model_window=[
                {
                    "qid": "550808201",
                    "text": "[图片] 这个猫是不是已经准备上班了",
                    "media": [
                        {
                            "kind": "image",
                            "message_id": "99112244",
                            "segment_index": 0,
                            "image": {"status": "completed", "reply_target_candidate": True},
                        }
                    ],
                },
            ],
            should_send=True,
        )

        self.assertEqual(result, "99112244")


def _group_image_event() -> ChatEvent:
    return ChatEvent(
        event_id="evt_group_image",
        session_id="qq_group_123456",
        platform="qq",
        user_id="550808201",
        event_type=EventType.IMAGE,
        text="[图片]",
        timestamp=datetime(2026, 6, 30, 23, 59),
        raw={
            "message_type": "group",
            "qq_group_id": "123456",
            "qq_user_id": "550808201",
            "onebot_message_id": "99112242",
            "media_refs": [
                {
                    "onebot_message_id": "99112242",
                    "segment_index": 0,
                    "is_sticker": False,
                    "file": "cat.png",
                    "url": "https://example.invalid/cat.png",
                }
            ],
        },
    )


if __name__ == "__main__":
    unittest.main()
