import asyncio
import unittest
from datetime import datetime, timedelta

from app.core.events import ChatEvent, EventType
from app.core.group_buffer import GroupChatBuffer
from app.core.group_scheduler import GroupReplyScheduler, group_event_mentions_bot, normalize_group_roll_config


class GroupReplySchedulerTest(unittest.TestCase):
    def test_default_roll_config_uses_zero_to_ten_and_factors_above_one(self):
        config = normalize_group_roll_config()

        self.assertEqual(config["roll_random_min"], 0.0)
        self.assertEqual(config["roll_random_max"], 10.0)
        self.assertTrue(config["roll_cooldown_random_enabled"])
        self.assertGreaterEqual(config["roll_cooldown_random_min_minutes"], 0.0)
        self.assertLessEqual(config["roll_cooldown_random_max_minutes"], 30.0)
        self.assertTrue(config["roll_hot_enabled"])
        self.assertGreater(config["roll_hot_x_step_minutes"], 0)
        self.assertGreaterEqual(config["roll_hot_x_min_minutes"], 0)
        for key in ("density_q1", "image_q2", "low_activity_p1", "meme_p2"):
            self.assertGreater(config[key], 1.0)
        self.assertLess(config["image_q2"], config["density_q1"])
        self.assertLess(config["meme_p2"], config["low_activity_p1"])

    def test_roll_q_adds_and_p_subtracts_adjusted_a(self):
        q_scheduler = GroupReplyScheduler(
            group_buffer=GroupChatBuffer(),
            decision_callback=lambda *_args: {"status": "wait", "should_send_candidate": False},
            config={
                "roll_cooldown_minutes": 0,
                "roll_attempt_cooldown_seconds": 0,
                "base_threshold": 8.0,
                "message_count_high": 3,
                "message_count_low": 2,
                "density_q1": 2.0,
                "image_q2": 1.5,
            },
            random_func=lambda _left, _right: 5.0,
            create_tasks=False,
        )
        q_roll = q_scheduler.evaluate_roll(
            "qq_group_123456",
            {"event_count": 4, "image_count": 1, "meme_ratio": 0.0},
        )

        self.assertEqual(q_roll["raw_roll_value"], 5.0)
        self.assertEqual(q_roll["adjusted_roll_value"], 8.5)
        self.assertTrue(q_roll["should_schedule"])
        self.assertEqual([op["op"] for op in q_roll["operations"]], ["add", "add"])

        p_scheduler = GroupReplyScheduler(
            group_buffer=GroupChatBuffer(),
            decision_callback=lambda *_args: {"status": "wait", "should_send_candidate": False},
            config={
                "roll_cooldown_minutes": 0,
                "roll_attempt_cooldown_seconds": 0,
                "base_threshold": 8.0,
                "message_count_high": 3,
                "message_count_low": 2,
                "low_activity_p1": 2.0,
                "meme_ratio_threshold": 0.5,
                "meme_p2": 1.25,
            },
            random_func=lambda _left, _right: 10.0,
            create_tasks=False,
        )
        p_roll = p_scheduler.evaluate_roll(
            "qq_group_123456",
            {"event_count": 1, "image_count": 0, "meme_ratio": 0.6},
        )

        self.assertEqual(p_roll["raw_roll_value"], 10.0)
        self.assertEqual(p_roll["adjusted_roll_value"], 6.75)
        self.assertFalse(p_roll["should_schedule"])
        self.assertEqual([op["op"] for op in p_roll["operations"]], ["subtract", "subtract"])

    def test_mention_event_schedules_pending_decision_with_latest_window(self):
        async def scenario():
            buffer = GroupChatBuffer()
            decisions = []
            debug_events = []
            now = datetime(2026, 6, 30, 23, 40)

            async def decide(session_id, trigger, model_window):
                decisions.append((session_id, trigger, model_window))
                return {"status": "wait", "should_send_candidate": False, "send_enabled": False}

            async def debug(payload):
                debug_events.append(payload)

            scheduler = GroupReplyScheduler(
                group_buffer=buffer,
                decision_callback=decide,
                debug_callback=debug,
                config={"mention_pending_seconds": 0},
                now_func=lambda: now,
                create_tasks=False,
            )
            mention = _group_event(
                event_id="evt_mention",
                message_id="99112233",
                text="[提及] 这个怎么看",
                mentions_bot=True,
            )
            append_result = await buffer.append(mention)
            await buffer.append(_group_event("evt_after", "99112234", "后面又补一句"))

            schedule = await scheduler.on_group_event(mention, append_result)
            run_result = await scheduler.run_pending_now("qq_group_123456")

            self.assertEqual(schedule["status"], "scheduled")
            self.assertEqual(schedule["trigger"]["reason"], "mention")
            self.assertEqual(schedule["trigger"]["reply_to_message_id"], "99112233")
            self.assertEqual(run_result["status"], "wait")
            self.assertEqual(decisions[0][0], "qq_group_123456")
            self.assertEqual(decisions[0][1]["reason"], "mention")
            self.assertEqual(len(decisions[0][2]), 2)
            self.assertEqual(decisions[0][2][-1]["text"], "后面又补一句")
            self.assertTrue(any(item["event_type"] == "group_reply_trigger_scheduled" for item in debug_events))

        asyncio.run(scenario())

    def test_roll_trigger_uses_config_snapshot_and_records_candidate_without_send(self):
        async def scenario():
            buffer = GroupChatBuffer()
            debug_events = []
            now = datetime(2026, 6, 30, 23, 45)

            async def decide(_session_id, _trigger, _model_window):
                return {
                    "status": "candidate_ready_not_sent",
                    "should_send_candidate": True,
                    "send_enabled": False,
                    "final_text": "这句可以插一下",
                }

            async def debug(payload):
                debug_events.append(payload)

            scheduler = GroupReplyScheduler(
                group_buffer=buffer,
                decision_callback=decide,
                debug_callback=debug,
                config={
                    "roll_cooldown_minutes": 0,
                    "roll_attempt_cooldown_seconds": 0,
                    "base_threshold": 8.0,
                    "low_activity_penalty": 0,
                },
                random_func=lambda _left, _right: 10.0,
                now_func=lambda: now,
                create_tasks=False,
            )
            event = _group_event("evt_roll", "99112235", "普通闲聊")
            append_result = await buffer.append(event)

            schedule = await scheduler.on_group_event(event, append_result)
            run_result = await scheduler.run_pending_now("qq_group_123456")
            status = scheduler.status("qq_group_123456")

            self.assertEqual(schedule["trigger"]["reason"], "roll")
            self.assertEqual(schedule["trigger"]["roll"]["raw_roll_value"], 10.0)
            self.assertGreater(schedule["trigger"]["roll"]["adjusted_roll_value"], 8.0)
            self.assertEqual(schedule["trigger"]["roll"]["comparison"], "adjusted_roll_value > base_threshold")
            self.assertFalse(run_result["decision"]["send_enabled"])
            self.assertEqual(run_result["status"], "candidate_ready_not_sent")
            self.assertIsNotNone(status["last_candidate_at"])
            self.assertTrue(any(item["event_type"] == "group_reply_decision_finished" for item in debug_events))

        asyncio.run(scenario())

    def test_non_stale_candidate_calls_send_callback(self):
        async def scenario():
            buffer = GroupChatBuffer()
            sent = []
            now = datetime(2026, 6, 30, 23, 47)

            async def decide(_session_id, _trigger, _model_window):
                return {
                    "status": "candidate_ready",
                    "should_send_candidate": True,
                    "final_text": "这句会发",
                }

            async def send(session_id, trigger, decision):
                sent.append((session_id, trigger, decision))
                return {"status": "sent", "send_key": "group_test"}

            scheduler = GroupReplyScheduler(
                group_buffer=buffer,
                decision_callback=decide,
                send_callback=send,
                config={
                    "roll_cooldown_minutes": 0,
                    "roll_attempt_cooldown_seconds": 0,
                    "base_threshold": 8.0,
                    "low_activity_penalty": 0,
                },
                random_func=lambda _left, _right: 10.0,
                now_func=lambda: now,
                create_tasks=False,
            )
            event = _group_event("evt_roll", "99112239", "普通闲聊")
            append_result = await buffer.append(event)

            await scheduler.on_group_event(event, append_result)
            result = await scheduler.run_pending_now("qq_group_123456")

            self.assertEqual(result["status"], "sent")
            self.assertEqual(result["send_result"]["status"], "sent")
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "qq_group_123456")

        asyncio.run(scenario())

    def test_stale_candidate_does_not_call_send_callback(self):
        async def scenario():
            buffer = GroupChatBuffer()
            sent = []
            now = datetime(2026, 6, 30, 23, 48)

            async def decide(_session_id, _trigger, _model_window):
                await buffer.append(_group_event("evt_new", "99112241", "新消息"))
                return {
                    "status": "candidate_ready",
                    "should_send_candidate": True,
                    "final_text": "旧窗口不能发",
                }

            async def send(session_id, trigger, decision):
                sent.append((session_id, trigger, decision))
                return {"status": "sent"}

            scheduler = GroupReplyScheduler(
                group_buffer=buffer,
                decision_callback=decide,
                send_callback=send,
                config={"mention_pending_seconds": 0, "max_stale_reruns": 0},
                now_func=lambda: now,
                create_tasks=False,
            )
            mention = _group_event("evt_mention", "99112240", "[提及] 看这个", mentions_bot=True)
            append_result = await buffer.append(mention)

            await scheduler.on_group_event(mention, append_result)
            result = await scheduler.run_pending_now("qq_group_123456")

            self.assertEqual(result["status"], "stale_dropped")
            self.assertEqual(sent, [])

        asyncio.run(scenario())

    def test_stale_decision_is_dropped_and_not_marked_as_candidate(self):
        async def scenario():
            buffer = GroupChatBuffer()
            now = datetime(2026, 6, 30, 23, 50)

            async def decide(_session_id, _trigger, _model_window):
                await buffer.append(_group_event("evt_new", "99112237", "决策中又来了新消息"))
                return {
                    "status": "candidate_ready_not_sent",
                    "should_send_candidate": True,
                    "send_enabled": False,
                    "final_text": "旧判断不能插队",
                }

            scheduler = GroupReplyScheduler(
                group_buffer=buffer,
                decision_callback=decide,
                config={
                    "mention_pending_seconds": 0,
                    "max_stale_reruns": 0,
                },
                now_func=lambda: now,
                create_tasks=False,
            )
            mention = _group_event(
                event_id="evt_mention",
                message_id="99112236",
                text="[提及] 看这个",
                mentions_bot=True,
            )
            append_result = await buffer.append(mention)

            await scheduler.on_group_event(mention, append_result)
            result = await scheduler.run_pending_now("qq_group_123456")
            status = scheduler.status("qq_group_123456")

            self.assertEqual(result["status"], "stale_dropped")
            self.assertIsNone(status["last_candidate_at"])
            self.assertEqual(status["last_decision"]["status"], "stale_dropped")

        asyncio.run(scenario())

    def test_roll_skips_when_candidate_cooldown_is_active(self):
        async def scenario():
            buffer = GroupChatBuffer()
            now = datetime(2026, 6, 30, 23, 55)
            scheduler = GroupReplyScheduler(
                group_buffer=buffer,
                decision_callback=lambda *_args: {"status": "wait", "should_send_candidate": False},
                config={"roll_cooldown_minutes": 30, "roll_cooldown_random_enabled": False},
                random_func=lambda _left, _right: 0.0,
                now_func=lambda: now,
                create_tasks=False,
            )
            scheduler._last_candidate_at["qq_group_123456"] = now - timedelta(minutes=5)
            event = _group_event("evt_roll", "99112238", "普通闲聊")
            append_result = await buffer.append(event)

            result = await scheduler.on_group_event(event, append_result)

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["roll"]["skip_reason"], "candidate_cooldown")

        asyncio.run(scenario())

    def test_roll_candidate_cooldown_uses_activity_adjusted_x(self):
        async def scenario():
            buffer = GroupChatBuffer()
            now = datetime(2026, 6, 30, 21, 0)
            scheduler = GroupReplyScheduler(
                group_buffer=buffer,
                decision_callback=lambda *_args: {"status": "wait", "should_send_candidate": False},
                config={"roll_cooldown_minutes": 30},
                activity_callback=lambda _sid, base, _now: {
                    "enabled": True,
                    "label": "quiet",
                    "base_roll_cooldown_minutes": base,
                    "roll_cooldown_minutes": 60.0,
                    "multiplier": 2.0,
                },
                random_func=lambda _left, _right: 10.0,
                now_func=lambda: now,
                create_tasks=False,
            )
            scheduler._last_candidate_at["qq_group_123456"] = now - timedelta(minutes=45)
            event = _group_event("evt_roll", "99112242", "普通闲聊")
            append_result = await buffer.append(event)

            result = await scheduler.on_group_event(event, append_result)

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["roll"]["skip_reason"], "candidate_cooldown")
            self.assertEqual(result["roll"]["activity"]["label"], "quiet")
            self.assertEqual(result["roll"]["activity"]["roll_cooldown_minutes"], 60.0)

        asyncio.run(scenario())

    def test_roll_hot_lowers_effective_x_until_next_real_miss(self):
        async def scenario():
            buffer = GroupChatBuffer()
            now = datetime(2026, 6, 30, 23, 30)
            rolls = iter([10.0, 0.0])

            async def decide(_session_id, _trigger, _model_window):
                return {
                    "status": "candidate_ready",
                    "should_send_candidate": True,
                    "final_text": "命中后进入热态",
                }

            scheduler = GroupReplyScheduler(
                group_buffer=buffer,
                decision_callback=decide,
                config={
                    "roll_cooldown_minutes": 30,
                    "roll_cooldown_random_enabled": False,
                    "roll_attempt_cooldown_seconds": 0,
                    "base_threshold": 6.0,
                    "message_count_high": 6,
                    "message_count_low": 2,
                    "density_q1": 2.0,
                    "roll_hot_x_step_minutes": 10,
                    "roll_hot_x_min_minutes": 5,
                },
                random_func=lambda _left, _right: next(rolls),
                now_func=lambda: now,
                create_tasks=False,
            )
            event = _group_event("evt_roll_hot", "99112243", "普通群聊")
            append_result = await buffer.append(event)

            schedule = await scheduler.on_group_event(event, append_result)
            self.assertEqual(schedule["status"], "scheduled")
            self.assertEqual(schedule["trigger"]["roll"]["roll_hot"]["effective_X_minutes"], 30)
            self.assertEqual(schedule["trigger"]["roll"]["roll_hot_update"]["after"]["effective_X_minutes"], 20)
            await scheduler.run_pending_now("qq_group_123456")
            self.assertEqual(scheduler.status("qq_group_123456")["roll_hot"]["level"], 1)

            now = datetime(2026, 6, 30, 23, 45)
            cooldown = scheduler.evaluate_roll(
                "qq_group_123456",
                {"event_count": 5, "image_count": 0, "meme_ratio": 0.0},
            )
            self.assertFalse(cooldown["should_schedule"])
            self.assertEqual(cooldown["skip_reason"], "candidate_cooldown")
            self.assertEqual(cooldown["cooldown"]["effective_x_minutes"], 20)
            self.assertEqual(cooldown["activity"]["roll_cooldown_minutes"], 20)

            now = datetime(2026, 6, 30, 23, 51)
            miss = scheduler.evaluate_roll(
                "qq_group_123456",
                {"event_count": 0, "image_count": 0, "meme_ratio": 0.0},
            )
            self.assertFalse(miss["should_schedule"])
            self.assertEqual(miss["roll_hot_update"]["event"], "roll_miss_reset")
            self.assertEqual(scheduler.status("qq_group_123456")["roll_hot"]["level"], 0)
            self.assertEqual(scheduler.status("qq_group_123456")["roll_hot"]["effective_X_minutes"], 30)

        asyncio.run(scenario())

    def test_random_x_is_clamped_before_hot_subtraction(self):
        now = datetime(2026, 6, 30, 23, 55)
        scheduler = GroupReplyScheduler(
            group_buffer=GroupChatBuffer(),
            decision_callback=lambda *_args: {"status": "wait", "should_send_candidate": False},
            config={
                "roll_cooldown_random_enabled": True,
                "roll_cooldown_random_min_minutes": 5,
                "roll_cooldown_random_max_minutes": 30,
                "roll_hot_x_step_minutes": 50,
                "roll_hot_x_min_minutes": 2,
            },
            random_func=lambda _left, _right: -100.0,
            now_func=lambda: now,
            create_tasks=False,
        )
        scheduler._last_candidate_at["qq_group_123456"] = now - timedelta(minutes=1)
        scheduler._roll_hot_level_by_session["qq_group_123456"] = 10

        result = scheduler.evaluate_roll(
            "qq_group_123456",
            {"event_count": 1, "image_count": 0, "meme_ratio": 0.0},
        )

        self.assertEqual(result["skip_reason"], "candidate_cooldown")
        self.assertEqual(result["cooldown"]["base_x_minutes"], 5)
        self.assertEqual(result["cooldown"]["effective_x_minutes"], 2)
        self.assertEqual(result["cooldown_seconds"], 120.0)

    def test_mentions_bot_reads_engineering_flag(self):
        self.assertTrue(group_event_mentions_bot(_group_event("evt", "1", "hi", mentions_bot=True)))
        self.assertFalse(group_event_mentions_bot(_group_event("evt", "1", "hi", mentions_bot=False)))


def _group_event(
    event_id: str,
    message_id: str,
    text: str,
    mentions_bot: bool = False,
) -> ChatEvent:
    return ChatEvent(
        event_id=event_id,
        session_id="qq_group_123456",
        platform="qq",
        user_id="550808201",
        event_type=EventType.TEXT,
        text=text,
        timestamp=datetime(2026, 6, 30, 23, 30),
        raw={
            "message_type": "group",
            "qq_group_id": "123456",
            "qq_user_id": "550808201",
            "qq_self_id": "999001",
            "onebot_message_id": message_id,
            "mentions_bot": mentions_bot,
            "at_user_ids": ["999001"] if mentions_bot else [],
        },
    )


if __name__ == "__main__":
    unittest.main()
