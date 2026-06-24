import asyncio
import json
import unittest
from datetime import datetime, timedelta

from app.active.settings import (
    fallback_active_message_setting_from_text,
    format_active_message_setting_response,
    parse_active_message_setting_output,
)
from app.api import routes
from app.core.events import ChatEvent, EventType
from app.llm.prompts import build_active_message_setting_messages
from app.scheduler.jobs import ACTIVE_DAILY_JOB_PREFIX, ACTIVE_NEXT_JOB_PREFIX, SchedulerManager


class ActiveMessageSettingsTest(unittest.TestCase):
    def test_setting_prompt_uses_minimal_schema_and_blocks_reminder_body(self):
        messages = build_active_message_setting_messages(
            content="明天10点提醒我吃药",
            current_time="2026-06-24 20:00",
        )
        system_prompt = messages[0]["content"]

        self.assertIn('{"type":"none","time":null}', system_prompt)
        self.assertIn('{"type":"next","time":"YYYY-MM-DD HH:mm"}', system_prompt)
        self.assertIn('{"type":"daily","time":"HH:mm"}', system_prompt)
        self.assertIn("不要输出“吃药”", system_prompt)
        self.assertIn("主动消息设定时间", system_prompt)

    def test_parser_accepts_only_next_daily_or_none(self):
        now = datetime(2026, 6, 24, 20, 0)

        self.assertEqual(
            parse_active_message_setting_output('{"type":"next","time":"2026-06-25 10:00"}', now),
            {"type": "next", "time": "2026-06-25 10:00"},
        )
        self.assertEqual(
            parse_active_message_setting_output('{"type":"daily","time":"7:45"}', now),
            {"type": "daily", "time": "07:45"},
        )
        self.assertEqual(
            parse_active_message_setting_output('{"type":"clear_daily_time","time":"07:45"}', now),
            {"type": "none", "time": None},
        )

    def test_local_fallback_treats_mem_active_message_time_as_daily(self):
        now = datetime(2026, 6, 24, 20, 0)

        self.assertEqual(
            fallback_active_message_setting_from_text("/mem 主动消息设定时间 7:45", now),
            {"type": "daily", "time": "07:45"},
        )
        self.assertEqual(
            fallback_active_message_setting_from_text("明天 10点提醒我吃药", now),
            {"type": "next", "time": "2026-06-25 10:00"},
        )

    def test_due_priority_is_next_then_user_daily_then_global(self):
        config = routes._normalize_active_message_config({
            "enabled": True,
            "hour": 10,
            "minute": 0,
            "daily_limit": 1,
            "sessions": {
                "qq_private_1": {
                    "daily_time": "07:45",
                    "next_active_at": "2026-06-25 09:30",
                },
                "qq_private_2": {"daily_time": "08:15"},
            },
            "quiet_start_hour": 0,
            "quiet_end_hour": 9,
        })

        self.assertNotIn("quiet_start_hour", config)
        self.assertEqual(
            routes._active_message_due_status(config, "qq_private_1", datetime(2026, 6, 25, 9, 30)),
            {"due": True, "source": "next", "time": "2026-06-25 09:30"},
        )
        self.assertEqual(
            routes._active_message_due_status(config, "qq_private_2", datetime(2026, 6, 25, 8, 15)),
            {"due": True, "source": "daily", "time": "08:15"},
        )
        self.assertEqual(
            routes._active_message_due_status(config, "qq_private_3", datetime(2026, 6, 25, 10, 0)),
            {"due": True, "source": "global", "time": "10:00"},
        )

    def test_scheduled_status_enforces_priority_and_stale_jobs(self):
        config = routes._normalize_active_message_config({
            "enabled": True,
            "hour": 10,
            "minute": 0,
            "daily_limit": 1,
            "sessions": {
                "qq_private_1": {
                    "daily_time": "07:45",
                    "next_active_at": "2026-06-25 09:30",
                },
                "qq_private_2": {"daily_time": "08:15"},
            },
        })

        self.assertEqual(
            routes._active_message_scheduled_status(
                config=config,
                session_id="qq_private_1",
                source="daily",
                scheduled_time="07:45",
            )["reason"],
            "next_active_time_pending",
        )
        self.assertEqual(
            routes._active_message_scheduled_status(
                config=config,
                session_id="qq_private_1",
                source="next",
                scheduled_time="2026-06-25 09:30",
            ),
            {"due": True, "source": "next", "time": "2026-06-25 09:30"},
        )
        self.assertEqual(
            routes._active_message_scheduled_status(
                config=config,
                session_id="qq_private_1",
                source="next",
                scheduled_time="2026-06-25 10:00",
            )["reason"],
            "stale_next_job",
        )
        self.assertEqual(
            routes._active_message_scheduled_status(
                config=config,
                session_id="qq_private_2",
                source="global",
                scheduled_time="10:00",
            )["reason"],
            "user_daily_override",
        )
        self.assertEqual(
            routes._active_message_scheduled_status(
                config=config,
                session_id="qq_private_3",
                source="global",
                scheduled_time="10:00",
            ),
            {"due": True, "source": "global", "time": "10:00"},
        )

    def test_scheduler_registers_dynamic_active_message_jobs_without_minute_scan(self):
        manager = SchedulerManager()
        trigger = manager._trigger_for_job("active_message", {"hour": 10, "minute": 0})
        self.assertIn("hour='10'", str(trigger))
        self.assertIn("minute='0'", str(trigger))
        self.assertNotIn("minute='*'", str(trigger))

        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        config = {
            "enabled": True,
            "hour": 10,
            "minute": 0,
            "daily_limit": 1,
            "sessions": {
                "qq_private_1": {"daily_time": "07:45"},
                "qq_private_2": {"next_active_at": future},
            },
        }
        refreshed, changed = manager.refresh_active_message_jobs(config)
        self.assertFalse(changed)
        self.assertEqual(refreshed["sessions"], config["sessions"])
        job_ids = sorted(job.id for job in manager.scheduler.get_jobs())
        self.assertIn(f"{ACTIVE_DAILY_JOB_PREFIX}qq_private_1", job_ids)
        self.assertIn(f"{ACTIVE_NEXT_JOB_PREFIX}qq_private_2", job_ids)
        self.assertEqual(len(job_ids), len(set(job_ids)))

        manager.refresh_active_message_jobs(config)
        job_ids_after_refresh = sorted(job.id for job in manager.scheduler.get_jobs())
        self.assertEqual(job_ids, job_ids_after_refresh)

    def test_scheduler_clears_expired_next_and_restores_daily_job(self):
        manager = SchedulerManager()
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        config = {
            "enabled": True,
            "hour": 10,
            "minute": 0,
            "daily_limit": 1,
            "sessions": {
                "qq_private_1": {
                    "daily_time": "07:45",
                    "next_active_at": past,
                },
            },
        }

        refreshed, changed = manager.refresh_active_message_jobs(config)
        self.assertTrue(changed)
        self.assertEqual(refreshed["sessions"]["qq_private_1"], {"daily_time": "07:45"})
        job_ids = sorted(job.id for job in manager.scheduler.get_jobs())
        self.assertIn(f"{ACTIVE_DAILY_JOB_PREFIX}qq_private_1", job_ids)
        self.assertNotIn(f"{ACTIVE_NEXT_JOB_PREFIX}qq_private_1", job_ids)

    def test_scheduler_does_not_stack_daily_when_next_is_pending(self):
        manager = SchedulerManager()
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        config = {
            "enabled": True,
            "hour": 10,
            "minute": 0,
            "daily_limit": 1,
            "sessions": {
                "qq_private_1": {
                    "daily_time": "07:45",
                    "next_active_at": future,
                },
            },
        }

        refreshed, changed = manager.refresh_active_message_jobs(config)
        self.assertFalse(changed)
        self.assertEqual(refreshed["sessions"]["qq_private_1"]["daily_time"], "07:45")
        job_ids = sorted(job.id for job in manager.scheduler.get_jobs())
        self.assertIn(f"{ACTIVE_NEXT_JOB_PREFIX}qq_private_1", job_ids)
        self.assertNotIn(f"{ACTIVE_DAILY_JOB_PREFIX}qq_private_1", job_ids)

    def test_memory_analysis_side_channel_applies_active_message_setting_and_removes_topic(self):
        class FakeMemory:
            def __init__(self):
                self.today_memory = "# 每日记忆\n"
                self.tomorrow_topics = routes.TOMORROW_TOPICS_TEMPLATE

            def read_today_memory(self):
                return self.today_memory

            def read_tomorrow_topics(self):
                return self.tomorrow_topics

            def write_today_memory(self, content):
                self.today_memory = content
                return True

            def write_tomorrow_topics(self, content):
                self.tomorrow_topics = content
                return True

        class FakeMemoryManager:
            def __init__(self, memory):
                self.memory = memory

            def for_session(self, _session_id):
                return self.memory

        class FakeLLM:
            def __init__(self, response):
                self.response = response

            async def chat_completion(self, **_kwargs):
                return self.response

        future = (datetime.now() + timedelta(days=1)).replace(second=0, microsecond=0)
        future_text = future.strftime("%Y-%m-%d %H:%M")
        proposed_topics = (
            "# 明日话题\n\n"
            "## 未闭合话题\n"
            "- [pending] [2026-06-24]: 用户请求明早8点发消息。\n"
            "- [pending] [2026-06-24]: 用户还没吃晚饭，后续可自然关心。\n\n"
            "## 昨日记忆\n\n"
            "## 生活感消息备选\n"
        )
        response = json.dumps(
            {
                "today_memory_md": (
                    "# 每日记忆\n"
                    "- 用户请求明早8点发消息。\n"
                    "- 用户今天还在干活。\n"
                ),
                "tomorrow_topics_md": proposed_topics,
                "active_message_setting": {"type": "next", "time": future_text},
            },
            ensure_ascii=False,
        )
        fake_memory = FakeMemory()
        manager = SchedulerManager(
            memory_manager=FakeMemoryManager(fake_memory),
            llm_client=FakeLLM(response),
        )
        manager._start_job = lambda *_args, **_kwargs: 1
        manager._finish_job = lambda *_args, **_kwargs: None
        manager._load_transcript_for_date = lambda *_args, **_kwargs: [
            {
                "created_at": "2026-06-24T18:11:24",
                "role": "user",
                "text": "明天早上八点给我发消息好吗",
            }
        ]
        applied = []
        manager.set_active_message_setting_callback(
            lambda session_id, setting: applied.append((session_id, setting)) or (True, "ok")
        )

        ok, error = asyncio.run(
            manager._run_memory_analysis_for_session("memory_analysis_20990624_190000", "qq_private_1")
        )

        self.assertTrue(ok, error)
        self.assertEqual(applied, [("qq_private_1", {"type": "next", "time": future_text})])
        self.assertNotIn("用户请求明早8点发消息", fake_memory.today_memory)
        self.assertIn("用户今天还在干活", fake_memory.today_memory)
        self.assertNotIn("用户请求明早8点发消息", fake_memory.tomorrow_topics)
        self.assertIn("用户还没吃晚饭", fake_memory.tomorrow_topics)

    def test_active_message_monitor_filters_initial_topics_and_formats_next_time(self):
        config = routes._normalize_active_message_config({
            "enabled": True,
            "hour": 10,
            "minute": 0,
            "daily_limit": 1,
            "sessions": {
                "qq_private_next": {
                    "daily_time": "07:45",
                    "next_active_at": "2026-06-25 09:30",
                },
                "qq_private_daily": {"daily_time": "08:15"},
            },
        })
        topics_by_session = {
            "qq_private_initial": routes.TOMORROW_TOPICS_TEMPLATE,
            "qq_private_next": routes.TOMORROW_TOPICS_TEMPLATE + "- [used] 已处理旧话题\n",
            "qq_private_daily": routes.TOMORROW_TOPICS_TEMPLATE + "- [blocked] 旧候选\n",
            "qq_private_global": routes.TOMORROW_TOPICS_TEMPLATE + "- [expired] 旧候选\n",
        }
        original_list_sessions = routes._list_sessions
        original_read_topics = routes._read_session_tomorrow_topics
        routes._list_sessions = lambda: [
            {"session_id": "qq_private_initial"},
            {"session_id": "qq_private_next"},
            {"session_id": "qq_private_daily"},
            {"session_id": "qq_private_global"},
        ]
        routes._read_session_tomorrow_topics = lambda sid: topics_by_session.get(sid, "")
        try:
            rows = routes._active_message_monitor_rows(config, datetime(2026, 6, 24, 8, 0))
            self.assertEqual(
                rows,
                [
                    {"session_id": "qq_private_daily", "next_time_to_activate": "2026-06-24 08:15"},
                    {"session_id": "qq_private_global", "next_time_to_activate": "2026-06-24 10:00"},
                    {"session_id": "qq_private_next", "next_time_to_activate": "2026-06-25 09:30"},
                ],
            )
        finally:
            routes._list_sessions = original_list_sessions
            routes._read_session_tomorrow_topics = original_read_topics

    def test_active_message_monitor_rolls_daily_to_tomorrow_and_ignores_expired_next(self):
        config = routes._normalize_active_message_config({
            "enabled": True,
            "hour": 10,
            "minute": 0,
            "daily_limit": 1,
            "sessions": {
                "qq_private_1": {
                    "daily_time": "07:45",
                    "next_active_at": "2026-06-23 09:30",
                },
            },
        })

        self.assertEqual(
            routes._active_message_next_activation_time(config, "qq_private_1", datetime(2026, 6, 24, 9, 0)),
            "2026-06-25 07:45",
        )
        self.assertEqual(
            routes._active_message_next_activation_time(config, "qq_private_2", datetime(2026, 6, 24, 11, 0)),
            "2026-06-25 10:00",
        )

    def test_active_message_monitor_disabled_has_no_rows(self):
        config = routes._normalize_active_message_config({
            "enabled": False,
            "hour": 10,
            "minute": 0,
            "daily_limit": 1,
        })
        self.assertEqual(routes._active_message_monitor_rows(config, datetime(2026, 6, 24, 9, 0)), [])

    def test_apply_setting_replaces_session_time_and_clear_next_preserves_daily(self):
        store = {
            "active_message": {
                "enabled": True,
                "hour": 10,
                "minute": 0,
                "daily_limit": 1,
                "sessions": {"qq_private_1": {"daily_time": "07:45"}},
            }
        }
        original_load_settings = routes.load_settings
        original_save_settings = routes.save_settings
        original_refresh_jobs = routes._refresh_active_message_jobs
        routes.load_settings = lambda: dict(store)
        refreshed_configs = []

        def fake_save_settings(updates):
            store.update(updates)
            return True

        routes.save_settings = fake_save_settings
        routes._refresh_active_message_jobs = lambda config=None, persist=True: refreshed_configs.append(config) or config
        try:
            success, response = routes._apply_active_message_setting(
                "qq_private_1",
                {"type": "daily", "time": "08:30"},
            )
            self.assertTrue(success)
            self.assertEqual(response, "好，我以后每天 08:30 左右来找你。")
            self.assertEqual(store["active_message"]["sessions"]["qq_private_1"]["daily_time"], "08:30")

            success, response = routes._apply_active_message_setting(
                "qq_private_1",
                {"type": "next", "time": "2099-06-25 10:00"},
            )
            self.assertTrue(success)
            self.assertIn("2099", store["active_message"]["sessions"]["qq_private_1"]["next_active_at"])
            self.assertNotIn("吃药", str(store["active_message"]))

            self.assertTrue(routes._clear_active_message_next_time("qq_private_1"))
            self.assertEqual(store["active_message"]["sessions"]["qq_private_1"], {"daily_time": "08:30"})
            self.assertGreaterEqual(len(refreshed_configs), 3)
        finally:
            routes.load_settings = original_load_settings
            routes.save_settings = original_save_settings
            routes._refresh_active_message_jobs = original_refresh_jobs

    def test_mem_active_message_setting_does_not_run_memory_write_path(self):
        calls = {"mem_command": 0, "graph_reset": 0}

        class FakeGate:
            def record_command(self, *_args):
                return None

        class FakeGraph:
            def reset_provider_transcript(self, *_args, **_kwargs):
                calls["graph_reset"] += 1

        class FakeRuntime:
            gate = FakeGate()
            graph = FakeGraph()

        async def fake_extract(_text, _session_id):
            return {"type": "daily", "time": "07:45"}

        async def fake_mem_command(*_args, **_kwargs):
            calls["mem_command"] += 1
            raise AssertionError("memory write path should not run")

        async def noop_async(*_args, **_kwargs):
            return None

        original_extract = routes._extract_active_message_setting
        original_apply = routes._apply_active_message_setting
        original_runtime = routes._runtime_for_event
        original_run_mem = routes._run_mem_command
        original_record_response = routes._record_command_response
        original_emit_finished = routes._emit_llm_finished
        original_emit_message = routes._emit_message
        original_emit_changed = routes._emit_conversation_changed
        routes._extract_active_message_setting = fake_extract
        routes._apply_active_message_setting = lambda _session_id, _setting: (
            True,
            "好，我以后每天 07:45 左右来找你。",
        )
        routes._runtime_for_event = lambda _event: FakeRuntime()
        routes._run_mem_command = fake_mem_command
        routes._record_command_response = noop_async
        routes._emit_llm_finished = noop_async
        routes._emit_message = noop_async
        routes._emit_conversation_changed = noop_async
        try:
            event = ChatEvent(
                event_id="evt_test_active_message_setting",
                session_id="qq_private_1",
                platform="webui",
                user_id="user",
                event_type=EventType.COMMAND_MEM,
                text="/mem 主动消息设定时间 7:45",
                timestamp=datetime(2026, 6, 24, 20, 0),
            )
            result = asyncio.run(routes._handle_command_event(event, {}))
            self.assertFalse(result["memory_updated"])
            self.assertTrue(result["active_message_setting_updated"])
            self.assertEqual(calls["mem_command"], 0)
            self.assertEqual(calls["graph_reset"], 0)
        finally:
            routes._extract_active_message_setting = original_extract
            routes._apply_active_message_setting = original_apply
            routes._runtime_for_event = original_runtime
            routes._run_mem_command = original_run_mem
            routes._record_command_response = original_record_response
            routes._emit_llm_finished = original_emit_finished
            routes._emit_message = original_emit_message
            routes._emit_conversation_changed = original_emit_changed

    def test_response_never_promises_the_reminder_content(self):
        response = format_active_message_setting_response(
            {"type": "next", "time": "2026-06-25 10:00"},
            now=datetime(2026, 6, 24, 20, 0),
        )
        self.assertEqual(response, "好，那我明天 10:00 左右来找你。")
        self.assertNotIn("提醒", response)


if __name__ == "__main__":
    unittest.main()
