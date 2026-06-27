import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace

from app.api import routes
from app.core.decisions import Action, ActionDecision, SendItem, SendItemType
from app.core.events import ChatEvent, EventType


class FakeGraph:
    def __init__(self, decision: ActionDecision):
        self.decision = decision
        self.committed = []
        self.reset_reasons = []

    async def run(self, ctx):
        return self.decision

    async def resolve_search_meme_item(self, ctx, item):
        return SendItem(type=SendItemType.MEME, content="amused_laugh")

    def get_prompt_observability(self):
        return {}

    def append_internal_system_reminder(self, message):
        pass

    def commit_sent_items(self, ctx, items):
        self.committed.append(("turn", list(items)))

    def commit_assistant_items(self, items):
        self.committed.append(("assistant", list(items)))

    def reset_provider_transcript(self, reason="provider_transcript_reset"):
        self.reset_reasons.append(reason)


class FakeGate:
    def __init__(self):
        self.sent = False
        self.stale = False
        self.send_keys = set()

    def is_job_stale(self, job_id):
        return self.stale

    def mark_job_stale_dropped(self, job_id):
        self.stale = True

    async def dispatch_latest_after_stale(self, job_id):
        pass

    async def is_snapshot_current(self, ctx):
        return True

    def record_decision(self, decision):
        pass

    async def clear_buffer_after_visible_send(self, expected_version):
        return True, expected_version + 1

    async def is_send_group_current(self, ctx, expected_buffer_version):
        return True

    def build_send_key(self, ctx, send_index):
        return f"{ctx.job_id}:{send_index}"

    def is_send_key_sent(self, send_key):
        return send_key in self.send_keys

    def record_send_meta(self, ctx, send_index, send_count, item_type=None):
        pass

    def mark_send_key_sent(self, send_key):
        self.send_keys.add(send_key)

    def mark_job_sent(self, job_id):
        self.sent = True

    def mark_job_dropped(self, job_id):
        self.sent = False

    async def _enter_hot(self):
        pass

    async def _exit_hot(self):
        pass


class FakeOneBotManager:
    def __init__(self):
        self.input_status_calls = []

    async def set_input_status(self, user_id: str, event_type: int):
        self.input_status_calls.append((user_id, event_type))
        return {"status": "ok", "retcode": 0}


class SendGateTest(unittest.TestCase):
    def test_successful_mem_command_resets_provider_transcript(self):
        async def scenario():
            reset_reasons = []
            recorded_commands = []
            runtime = SimpleNamespace(
                graph=SimpleNamespace(
                    reset_provider_transcript=lambda reason: reset_reasons.append(reason),
                ),
                gate=SimpleNamespace(
                    record_command=lambda command, status: recorded_commands.append((command, status)),
                ),
            )
            event = ChatEvent(
                event_id="mem1",
                session_id="qq_private_10001",
                platform="qq",
                user_id="10001",
                event_type=EventType.COMMAND_MEM,
                text="/mem 记住不要发表情包",
                timestamp=datetime.now(),
            )
            emitted_messages = []

            async def fake_run_mem_command(text, session_id, target):
                return True, "已通过记忆线程写入 CORE。"

            async def noop_async(*args, **kwargs):
                return None

            async def fake_emit_message(data):
                emitted_messages.append(data)

            originals = {
                "_runtime_for_event": routes._runtime_for_event,
                "_run_mem_command": routes._run_mem_command,
                "_record_command_response": routes._record_command_response,
                "_emit_llm_finished": routes._emit_llm_finished,
                "_emit_message": routes._emit_message,
                "_emit_conversation_changed": routes._emit_conversation_changed,
            }

            try:
                routes._runtime_for_event = lambda _event: runtime
                routes._run_mem_command = fake_run_mem_command
                routes._record_command_response = noop_async
                routes._emit_llm_finished = noop_async
                routes._emit_message = fake_emit_message
                routes._emit_conversation_changed = noop_async

                result = await routes._handle_command_event(event)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            self.assertTrue(result["memory_updated"])
            self.assertEqual(recorded_commands, [("/mem", "success")])
            self.assertEqual(reset_reasons, ["command.mem_memory_updated"])
            self.assertEqual(emitted_messages[0]["content"], "已通过记忆线程写入 CORE。")

        asyncio.run(scenario())

    def test_text_display_split_breaks_after_long_chinese_clause_comma(self):
        self.assertEqual(
            routes._split_text_for_display("刚在阳台给花浇水呢，晚霞好漂亮，想叫你来看"),
            ["刚在阳台给花浇水呢，", "晚霞好漂亮，想叫你来看"],
        )
        self.assertEqual(
            routes._split_text_for_display("晚霞好漂亮，想叫你来看"),
            ["晚霞好漂亮，想叫你来看"],
        )
        self.assertEqual(
            routes._split_text_for_display("今天真的好烦，想睡觉"),
            ["今天真的好烦，想睡觉"],
        )
        self.assertEqual(
            routes._split_text_for_display("刚在阳台给花浇水呢，晚霞好漂亮，今天风也很温柔，想叫你来看"),
            ["刚在阳台给花浇水呢，", "晚霞好漂亮，今天风也很温柔，想叫你来看"],
        )
        self.assertEqual(
            routes._split_text_for_display("这件事情真的离谱...但是我先缓一下"),
            ["这件事情真的离谱...", "但是我先缓一下"],
        )
        self.assertEqual(
            routes._split_text_for_display("我现在其实有点想笑——但还是先忍住"),
            ["我现在其实有点想笑——", "但还是先忍住"],
        )
        self.assertEqual(
            routes._split_text_for_display("你今天怎么这么可爱？我有点受不了"),
            ["你今天怎么这么可爱？", "我有点受不了"],
        )

    def test_text_display_split_is_disabled_when_model_already_outputs_four_items(self):
        long_text = "刚在阳台给花浇水呢，晚霞好漂亮，想叫你来看"

        three_items = [
            SendItem(type=SendItemType.TEXT, content=long_text),
            SendItem(type=SendItemType.TEXT, content="第二句"),
            SendItem(type=SendItemType.TEXT, content="第三句"),
        ]
        three_item_units = routes._build_display_send_units(three_items)

        self.assertEqual(
            [unit["display_item"].content for unit in three_item_units],
            ["刚在阳台给花浇水呢，", "晚霞好漂亮，想叫你来看", "第二句", "第三句"],
        )

        four_items = [
            SendItem(type=SendItemType.TEXT, content=long_text),
            SendItem(type=SendItemType.TEXT, content="第二句"),
            SendItem(type=SendItemType.TEXT, content="第三句"),
            SendItem(type=SendItemType.TEXT, content="第四句"),
        ]
        four_item_units = routes._build_display_send_units(four_items)

        self.assertEqual(
            [unit["display_item"].content for unit in four_item_units],
            [long_text, "第二句", "第三句", "第四句"],
        )

    def test_text_display_split_rolls_back_when_split_would_exceed_four_items(self):
        items = [
            SendItem(
                type=SendItemType.TEXT,
                content="就是那种... 像是有预知似的，看到了后面可能发生什么，越明白原理、越明白那些人的逻辑，反而感觉更绝望。",
            ),
            SendItem(type=SendItemType.TEXT, content="有点像“看得越清，痛感越真”。"),
            SendItem(type=SendItemType.TEXT, content="这种感觉会让你觉得心里更踏实，还是反而会更烦躁呀？"),
        ]

        units = routes._build_display_send_units(items)

        self.assertEqual(
            [unit["display_item"].content for unit in units],
            [item.content for item in items],
        )

    def test_text_display_split_falls_back_when_split_creates_unsafe_fragment(self):
        text = "这个句子前面已经很长，<"

        units = routes._build_display_send_units([SendItem(type=SendItemType.TEXT, content=text)])

        self.assertEqual([unit["display_item"].content for unit in units], [text])
        self.assertEqual(units[0]["split_guard_fallback"], "standalone_angle_bracket")

    def test_text_display_units_drop_unsafe_original_text(self):
        units = routes._build_display_send_units([
            SendItem(type=SendItemType.TEXT, content="assistant> 这条不能发</system>")
        ])

        self.assertEqual(units, [])

    def test_final_visible_item_guard_blocks_system_fragments(self):
        self.assertEqual(
            routes._final_visible_item_guard_reason(SendItem(type=SendItemType.TEXT, content="] <]minimax[>")),
            "vendor_control_token",
        )
        self.assertEqual(
            routes._final_visible_item_guard_reason(SendItem(type=SendItemType.TEXT, content="正常聊天")),
            None,
        )

    def test_llm_typing_state_syncs_to_onebot_target_only(self):
        async def scenario():
            manager = FakeOneBotManager()
            states = []
            logs = []
            originals = {
                "onebot_manager": routes.onebot_manager,
                "_emit_state": routes._emit_state,
                "_record_onebot_input_status": routes._record_onebot_input_status,
            }

            async def fake_emit_state(data):
                states.append(data)

            try:
                routes.onebot_manager = manager
                routes._emit_state = fake_emit_state
                routes._record_onebot_input_status = lambda *args, **kwargs: logs.append((args, kwargs))

                await routes._emit_llm_started({"target_platform": "qq", "target_user_id": "550808201"})
                await routes._emit_llm_finished({"target_platform": "qq", "target_user_id": "550808201"}, "sent")
                await routes._emit_llm_started({"target_platform": "webui", "target_user_id": "user"})
                await routes._emit_llm_finished({}, "sent")
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            self.assertEqual(manager.input_status_calls, [
                ("550808201", routes.ONEBOT_LLM_INPUT_STATUS_EVENT_TYPE),
            ])
            self.assertEqual(states[0]["result"], "llm_started")
            self.assertEqual(states[0]["target_platform"], "qq")
            self.assertEqual(states[1]["result"], "llm_finished")
            self.assertEqual(states[1]["target_platform"], "qq")
            self.assertEqual(states[1]["reason"], "sent")
            self.assertEqual(states[2], {"result": "llm_started", "target_platform": "webui", "target_user_id": "user"})
            self.assertEqual(states[3], {"result": "llm_finished", "reason": "sent"})
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0][0][1], routes.ONEBOT_LLM_INPUT_STATUS_EVENT_TYPE)

        asyncio.run(scenario())

    def test_llm_typing_state_refreshes_until_finished(self):
        async def scenario():
            manager = FakeOneBotManager()
            logs = []
            target = {"target_platform": "qq", "target_user_id": "550808201"}
            originals = {
                "onebot_manager": routes.onebot_manager,
                "_emit_state": routes._emit_state,
                "_record_onebot_input_status": routes._record_onebot_input_status,
                "ONEBOT_LLM_INPUT_STATUS_REFRESH_SECONDS": routes.ONEBOT_LLM_INPUT_STATUS_REFRESH_SECONDS,
                "ONEBOT_LLM_INPUT_STATUS_MAX_SECONDS": routes.ONEBOT_LLM_INPUT_STATUS_MAX_SECONDS,
            }

            async def fake_emit_state(data):
                pass

            try:
                routes.onebot_manager = manager
                routes._emit_state = fake_emit_state
                routes._record_onebot_input_status = lambda *args, **kwargs: logs.append((args, kwargs))
                routes.ONEBOT_LLM_INPUT_STATUS_REFRESH_SECONDS = 0.01
                routes.ONEBOT_LLM_INPUT_STATUS_MAX_SECONDS = 0.05

                await routes._emit_llm_started(target)
                await asyncio.sleep(0.025)
                await routes._emit_llm_finished(target, "sent")
                count_after_finish = len(manager.input_status_calls)
                await asyncio.sleep(0.025)
            finally:
                await routes._stop_onebot_input_status_refresh(target)
                for name, value in originals.items():
                    setattr(routes, name, value)

            self.assertGreaterEqual(count_after_finish, 2)
            self.assertEqual(len(manager.input_status_calls), count_after_finish)
            self.assertTrue(all(
                call == ("550808201", routes.ONEBOT_LLM_INPUT_STATUS_EVENT_TYPE)
                for call in manager.input_status_calls
            ))
            reasons = [entry[0][4] for entry in logs]
            self.assertIn("llm_started", reasons)
            self.assertIn("llm_refresh", reasons)

        asyncio.run(scenario())

    def test_composing_gate_only_applies_to_text_items(self):
        self.assertFalse(routes._requires_user_composing_gate([
            SendItem(type=SendItemType.MEME, content="affection_hug"),
        ]))
        self.assertFalse(routes._requires_user_composing_gate([
            SendItem(type=SendItemType.SEARCH_MEME, content="amused:laugh"),
        ]))
        self.assertTrue(routes._requires_user_composing_gate([
            SendItem(type=SendItemType.TEXT, content="等我一下"),
        ]))

    def test_user_composing_wait_deadline_starts_when_send_gate_defers(self):
        async def scenario():
            class ComposingGate:
                def __init__(self):
                    self.composing = True
                    self.cleared_reason = None
                    self.deferred = []
                    self.current_checks = []

                def is_user_composing(self):
                    return self.composing

                def mark_job_deferred(self, job_id):
                    self.deferred.append(job_id)

                def get_user_composing_meta(self):
                    return {"active": self.composing, "until": "raw-input-status-ttl"}

                def is_job_stale(self, job_id):
                    return False

                def mark_job_stale_dropped(self, job_id):
                    pass

                async def dispatch_latest_after_stale(self, job_id):
                    pass

                async def is_send_group_current(self, ctx, expected_buffer_version):
                    self.current_checks.append(expected_buffer_version)
                    return expected_buffer_version == 42

                def clear_user_composing(self, event=None, reason="cleared"):
                    self.composing = False
                    self.cleared_reason = reason

            gate = ComposingGate()
            states = []
            originals = {
                "_emit_state": routes._emit_state,
                "USER_COMPOSING_MAX_BLOCK_SECONDS": routes.USER_COMPOSING_MAX_BLOCK_SECONDS,
            }

            async def fake_emit_state(data):
                states.append(data)

            try:
                routes._emit_state = fake_emit_state
                routes.USER_COMPOSING_MAX_BLOCK_SECONDS = 0.01
                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job_wait",
                    snapshot=SimpleNamespace(
                        session_id="default",
                        snapshot_id=1,
                        buffer_version=10,
                    ),
                )

                ok = await routes._wait_until_user_not_composing(ctx, expected_buffer_version=42)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            self.assertTrue(ok)
            self.assertEqual(gate.deferred, ["job_wait"])
            self.assertEqual(gate.cleared_reason, "send_gate_max_wait_elapsed")
            self.assertEqual(gate.current_checks, [42])
            self.assertEqual(states[0]["result"], "deferred_user_composing")
            self.assertEqual(states[0]["max_wait_seconds"], 0.01)
            self.assertEqual(states[-1]["result"], "resumed_after_user_composing")

        asyncio.run(scenario())

    def test_text_composing_wait_happens_before_buffer_clear(self):
        async def scenario():
            decision = ActionDecision(
                action=Action.REPLY,
                items=[SendItem(type=SendItemType.TEXT, content="我在呢")],
            )
            graph = FakeGraph(decision)
            gate = FakeGate()
            order = []

            async def fake_clear(expected_version):
                order.append(("clear", expected_version))
                return True, expected_version + 1

            async def fake_wait(ctx, expected_buffer_version=None):
                order.append(("wait", expected_buffer_version))
                return True

            gate.clear_buffer_after_visible_send = fake_clear

            originals = {
                "companion_graph": routes.companion_graph,
                "event_gate": routes.event_gate,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_message": routes._emit_message,
                "_emit_state": routes._emit_state,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_wait_until_user_not_composing": routes._wait_until_user_not_composing,
                "_apply_recent_repetition_guard": routes._apply_recent_repetition_guard,
                "_record_prompt_cache_debug": routes._record_prompt_cache_debug,
                "_record_assistant_send": routes._record_assistant_send,
                "_record_job_state": routes._record_job_state,
            }

            async def noop_async(*args, **kwargs):
                pass

            try:
                routes.companion_graph = graph
                routes.event_gate = gate
                routes._emit_llm_started = noop_async
                routes._emit_message = noop_async
                routes._emit_state = noop_async
                routes._emit_conversation_changed = noop_async
                routes._wait_until_user_not_composing = fake_wait
                routes._apply_recent_repetition_guard = lambda ctx, decision: (decision, [])
                routes._record_prompt_cache_debug = lambda *args, **kwargs: None
                routes._record_assistant_send = lambda *args, **kwargs: None
                routes._record_job_state = lambda *args, **kwargs: None

                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job_text_wait",
                    snapshot=SimpleNamespace(
                        session_id="default",
                        snapshot_id=1,
                        buffer_version=10,
                        events=[{"text": "在吗"}],
                    ),
                )
                await routes.on_decision(ctx)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            self.assertEqual(order, [("wait", 10), ("clear", 10)])
            self.assertTrue(gate.sent)

        asyncio.run(scenario())

    def test_active_schedule_side_effect_applies_after_visible_send(self):
        async def scenario():
            decision = ActionDecision(
                action=Action.REPLY,
                items=[SendItem(type=SendItemType.TEXT, content="好，那我明天十点左右来找你。")],
                side_effects={
                    "active_message_setting": {
                        "type": "next",
                        "time": "2099-06-28 10:00",
                    }
                },
            )
            graph = FakeGraph(decision)
            gate = FakeGate()
            emitted = []
            apply_calls = []
            side_effect_logs = []
            order = []

            original_commit = graph.commit_sent_items

            def fake_commit(ctx, items):
                order.append("commit")
                original_commit(ctx, items)

            graph.commit_sent_items = fake_commit

            originals = {
                "companion_graph": routes.companion_graph,
                "event_gate": routes.event_gate,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_message": routes._emit_message,
                "_emit_state": routes._emit_state,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_wait_until_user_not_composing": routes._wait_until_user_not_composing,
                "_apply_recent_repetition_guard": routes._apply_recent_repetition_guard,
                "_record_prompt_cache_debug": routes._record_prompt_cache_debug,
                "_record_assistant_send": routes._record_assistant_send,
                "_record_job_state": routes._record_job_state,
                "_apply_active_message_setting": routes._apply_active_message_setting,
                "_record_active_message_setting_side_effect": routes._record_active_message_setting_side_effect,
            }

            async def noop_async(*args, **kwargs):
                pass

            async def fake_emit_message(data):
                order.append("emit")
                emitted.append(data)

            async def fake_wait(ctx, expected_buffer_version=None):
                return True

            def fake_record_send(*args, **kwargs):
                order.append("record_send")

            def fake_apply(session_id, setting):
                order.append("apply")
                apply_calls.append((session_id, setting))
                return True, "ok"

            def fake_record_side_effect(ctx, decision, status, payload):
                side_effect_logs.append((status, payload))

            try:
                routes.companion_graph = graph
                routes.event_gate = gate
                routes._emit_llm_started = noop_async
                routes._emit_message = fake_emit_message
                routes._emit_state = noop_async
                routes._emit_conversation_changed = noop_async
                routes._wait_until_user_not_composing = fake_wait
                routes._apply_recent_repetition_guard = lambda ctx, decision: (decision, [])
                routes._record_prompt_cache_debug = lambda *args, **kwargs: None
                routes._record_assistant_send = fake_record_send
                routes._record_job_state = lambda *args, **kwargs: None
                routes._apply_active_message_setting = fake_apply
                routes._record_active_message_setting_side_effect = fake_record_side_effect

                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job_active_marker",
                    snapshot=SimpleNamespace(
                        session_id="qq_private_1",
                        snapshot_id=1,
                        buffer_version=10,
                        events=[{"text": "明天 10点提醒我吃药"}],
                    ),
                )
                await routes.on_decision(ctx)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            assistant_messages = [item for item in emitted if item.get("type") == "assistant_message"]
            self.assertEqual(len(assistant_messages), 1)
            self.assertEqual(assistant_messages[0]["content"], "好，那我明天十点左右来找你。")
            self.assertNotIn("next:", assistant_messages[0]["content"])
            self.assertEqual(
                apply_calls,
                [("qq_private_1", {"type": "next", "time": "2099-06-28 10:00"})],
            )
            self.assertEqual(side_effect_logs[-1][0], "applied")
            self.assertEqual(order[-1], "apply")
            self.assertIn("commit", order)
            self.assertLess(order.index("commit"), order.index("apply"))
            self.assertTrue(gate.sent)

        asyncio.run(scenario())

    def test_active_schedule_side_effect_is_skipped_without_user_request(self):
        async def scenario():
            decision = ActionDecision(
                action=Action.REPLY,
                items=[SendItem(type=SendItemType.TEXT, content="晚点再说。")],
                side_effects={
                    "active_message_setting": {
                        "type": "daily",
                        "time": "07:45",
                    }
                },
            )
            graph = FakeGraph(decision)
            gate = FakeGate()
            apply_calls = []
            side_effect_logs = []

            originals = {
                "companion_graph": routes.companion_graph,
                "event_gate": routes.event_gate,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_message": routes._emit_message,
                "_emit_state": routes._emit_state,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_wait_until_user_not_composing": routes._wait_until_user_not_composing,
                "_apply_recent_repetition_guard": routes._apply_recent_repetition_guard,
                "_record_prompt_cache_debug": routes._record_prompt_cache_debug,
                "_record_assistant_send": routes._record_assistant_send,
                "_record_job_state": routes._record_job_state,
                "_apply_active_message_setting": routes._apply_active_message_setting,
                "_record_active_message_setting_side_effect": routes._record_active_message_setting_side_effect,
            }

            async def noop_async(*args, **kwargs):
                pass

            async def fake_wait(ctx, expected_buffer_version=None):
                return True

            def fake_apply(session_id, setting):
                apply_calls.append((session_id, setting))
                return True, "ok"

            def fake_record_side_effect(ctx, decision, status, payload):
                side_effect_logs.append((status, payload))

            try:
                routes.companion_graph = graph
                routes.event_gate = gate
                routes._emit_llm_started = noop_async
                routes._emit_message = noop_async
                routes._emit_state = noop_async
                routes._emit_conversation_changed = noop_async
                routes._wait_until_user_not_composing = fake_wait
                routes._apply_recent_repetition_guard = lambda ctx, decision: (decision, [])
                routes._record_prompt_cache_debug = lambda *args, **kwargs: None
                routes._record_assistant_send = lambda *args, **kwargs: None
                routes._record_job_state = lambda *args, **kwargs: None
                routes._apply_active_message_setting = fake_apply
                routes._record_active_message_setting_side_effect = fake_record_side_effect

                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job_active_marker_skip",
                    snapshot=SimpleNamespace(
                        session_id="qq_private_1",
                        snapshot_id=1,
                        buffer_version=10,
                        events=[{"text": "今天事情好多"}],
                    ),
                )
                await routes.on_decision(ctx)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            self.assertEqual(apply_calls, [])
            self.assertEqual(side_effect_logs[-1][0], "skipped")
            self.assertEqual(
                side_effect_logs[-1][1]["reason"],
                "current_user_text_did_not_request_active_message_setting",
            )
            self.assertTrue(gate.sent)

        asyncio.run(scenario())

    def test_meme_sends_before_following_text_is_deferred_by_composing(self):
        async def scenario():
            decision = ActionDecision(
                action=Action.REACT,
                items=[
                    SendItem(type=SendItemType.MEME, content="affection_hug"),
                    SendItem(type=SendItemType.TEXT, content="先给你这个。"),
                ],
            )
            graph = FakeGraph(decision)
            gate = FakeGate()
            emitted = []
            wait_calls = []

            originals = {
                "companion_graph": routes.companion_graph,
                "event_gate": routes.event_gate,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_message": routes._emit_message,
                "_emit_state": routes._emit_state,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_wait_until_user_not_composing": routes._wait_until_user_not_composing,
                "_apply_recent_repetition_guard": routes._apply_recent_repetition_guard,
                "_record_prompt_cache_debug": routes._record_prompt_cache_debug,
                "_record_assistant_send": routes._record_assistant_send,
                "_record_job_state": routes._record_job_state,
            }

            async def noop_async(*args, **kwargs):
                pass

            async def fake_emit_message(data):
                emitted.append(data)

            async def fake_wait(ctx, expected_buffer_version=None):
                wait_calls.append(ctx.job_id)
                return False

            try:
                routes.companion_graph = graph
                routes.event_gate = gate
                routes._emit_llm_started = noop_async
                routes._emit_message = fake_emit_message
                routes._emit_state = noop_async
                routes._emit_conversation_changed = noop_async
                routes._wait_until_user_not_composing = fake_wait
                routes._apply_recent_repetition_guard = lambda ctx, decision: (decision, [])
                routes._record_prompt_cache_debug = lambda *args, **kwargs: None
                routes._record_assistant_send = lambda *args, **kwargs: None
                routes._record_job_state = lambda *args, **kwargs: None

                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job1",
                    snapshot=SimpleNamespace(
                        session_id="default",
                        snapshot_id=1,
                        buffer_version=10,
                        events=[{"text": "发个表情"}],
                    ),
                )
                await routes.on_decision(ctx)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            assistant_messages = [item for item in emitted if item.get("type") == "assistant_message"]
            self.assertEqual(len(assistant_messages), 1)
            self.assertEqual(assistant_messages[0]["item_type"], "meme")
            self.assertEqual(assistant_messages[0]["content"], "meme:affection_hug")
            self.assertEqual(wait_calls, ["job1"])
            self.assertFalse(gate.sent)
            self.assertEqual(graph.committed[0][0], "turn")
            self.assertEqual(graph.committed[0][1][0].type, SendItemType.MEME)

        asyncio.run(scenario())

    def test_search_meme_marker_sends_prefix_before_second_round_selection(self):
        async def scenario():
            decision = ActionDecision(
                action=Action.REACT,
                items=[
                    SendItem(type=SendItemType.TEXT, content="先笑一下"),
                    SendItem(type=SendItemType.SEARCH_MEME, content="amused:laugh"),
                    SendItem(type=SendItemType.TEXT, content="然后继续说"),
                ],
            )
            graph = FakeGraph(decision)
            gate = FakeGate()
            emitted = []
            resolve_seen_counts = []

            originals = {
                "companion_graph": routes.companion_graph,
                "event_gate": routes.event_gate,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_message": routes._emit_message,
                "_emit_state": routes._emit_state,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_wait_until_user_not_composing": routes._wait_until_user_not_composing,
                "_apply_recent_repetition_guard": routes._apply_recent_repetition_guard,
                "_record_prompt_cache_debug": routes._record_prompt_cache_debug,
                "_record_assistant_send": routes._record_assistant_send,
                "_record_job_state": routes._record_job_state,
            }

            async def noop_async(*args, **kwargs):
                pass

            async def fake_emit_message(data):
                emitted.append(data)

            async def fake_resolve(ctx, item):
                resolve_seen_counts.append(len([
                    event for event in emitted if event.get("type") == "assistant_message"
                ]))
                return SendItem(type=SendItemType.MEME, content="amused_laugh")

            try:
                graph.resolve_search_meme_item = fake_resolve
                routes.companion_graph = graph
                routes.event_gate = gate
                routes._emit_llm_started = noop_async
                routes._emit_message = fake_emit_message
                routes._emit_state = noop_async
                routes._emit_conversation_changed = noop_async
                routes._wait_until_user_not_composing = lambda ctx, expected_buffer_version=None: asyncio.sleep(0, result=True)
                routes._apply_recent_repetition_guard = lambda ctx, decision: (decision, [])
                routes._record_prompt_cache_debug = lambda *args, **kwargs: None
                routes._record_assistant_send = lambda *args, **kwargs: None
                routes._record_job_state = lambda *args, **kwargs: None

                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job_marker",
                    snapshot=SimpleNamespace(
                        session_id="default",
                        snapshot_id=1,
                        buffer_version=10,
                        events=[{"text": "哈哈哈"}],
                    ),
                )
                await routes.on_decision(ctx)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            assistant_messages = [item for item in emitted if item.get("type") == "assistant_message"]
            self.assertEqual(resolve_seen_counts, [1])
            self.assertEqual([item["content"] for item in assistant_messages], [
                "先笑一下",
                "meme:amused_laugh",
                "然后继续说",
            ])
            self.assertEqual([item["item_type"] for item in assistant_messages], ["text", "meme", "text"])
            self.assertTrue(gate.sent)
            committed = [item for _, batch in graph.committed for item in batch]
            self.assertEqual([item.type for item in committed], [
                SendItemType.TEXT,
                SendItemType.MEME,
                SendItemType.TEXT,
            ])

        asyncio.run(scenario())

    def test_search_meme_marker_without_candidate_skips_marker_and_sends_suffix(self):
        async def scenario():
            decision = ActionDecision(
                action=Action.REACT,
                items=[
                    SendItem(type=SendItemType.TEXT, content="先说这句"),
                    SendItem(type=SendItemType.SEARCH_MEME, content="amused:laugh"),
                    SendItem(type=SendItemType.TEXT, content="后面继续"),
                ],
            )
            graph = FakeGraph(decision)
            gate = FakeGate()
            emitted = []

            originals = {
                "companion_graph": routes.companion_graph,
                "event_gate": routes.event_gate,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_message": routes._emit_message,
                "_emit_state": routes._emit_state,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_wait_until_user_not_composing": routes._wait_until_user_not_composing,
                "_apply_recent_repetition_guard": routes._apply_recent_repetition_guard,
                "_record_prompt_cache_debug": routes._record_prompt_cache_debug,
                "_record_assistant_send": routes._record_assistant_send,
                "_record_job_state": routes._record_job_state,
            }

            async def noop_async(*args, **kwargs):
                pass

            async def fake_emit_message(data):
                emitted.append(data)

            async def fake_resolve(ctx, item):
                return None

            try:
                graph.resolve_search_meme_item = fake_resolve
                routes.companion_graph = graph
                routes.event_gate = gate
                routes._emit_llm_started = noop_async
                routes._emit_message = fake_emit_message
                routes._emit_state = noop_async
                routes._emit_conversation_changed = noop_async
                routes._wait_until_user_not_composing = lambda ctx, expected_buffer_version=None: asyncio.sleep(0, result=True)
                routes._apply_recent_repetition_guard = lambda ctx, decision: (decision, [])
                routes._record_prompt_cache_debug = lambda *args, **kwargs: None
                routes._record_assistant_send = lambda *args, **kwargs: None
                routes._record_job_state = lambda *args, **kwargs: None

                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job_marker_skip",
                    snapshot=SimpleNamespace(
                        session_id="default",
                        snapshot_id=1,
                        buffer_version=10,
                        events=[{"text": "哈哈哈"}],
                    ),
                )
                await routes.on_decision(ctx)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            assistant_messages = [item for item in emitted if item.get("type") == "assistant_message"]
            self.assertEqual([item["content"] for item in assistant_messages], ["先说这句", "后面继续"])
            self.assertEqual([item["item_type"] for item in assistant_messages], ["text", "text"])
            self.assertTrue(gate.sent)
            committed = [item for _, batch in graph.committed for item in batch]
            self.assertEqual([item.type for item in committed], [SendItemType.TEXT, SendItemType.TEXT])

        asyncio.run(scenario())

    def test_search_meme_marker_drops_meme_and_suffix_when_stale_after_selection(self):
        async def scenario():
            decision = ActionDecision(
                action=Action.REACT,
                items=[
                    SendItem(type=SendItemType.TEXT, content="先说这句"),
                    SendItem(type=SendItemType.SEARCH_MEME, content="amused:laugh"),
                    SendItem(type=SendItemType.TEXT, content="这句不该发"),
                ],
            )
            graph = FakeGraph(decision)
            gate = FakeGate()
            emitted = []
            states = []
            checks = {"count": 0}

            originals = {
                "companion_graph": routes.companion_graph,
                "event_gate": routes.event_gate,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_message": routes._emit_message,
                "_emit_state": routes._emit_state,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_wait_until_user_not_composing": routes._wait_until_user_not_composing,
                "_apply_recent_repetition_guard": routes._apply_recent_repetition_guard,
                "_record_prompt_cache_debug": routes._record_prompt_cache_debug,
                "_record_assistant_send": routes._record_assistant_send,
                "_record_job_state": routes._record_job_state,
            }

            async def noop_async(*args, **kwargs):
                pass

            async def fake_emit_message(data):
                emitted.append(data)

            async def fake_emit_state(data):
                states.append(data)

            async def fake_is_send_group_current(ctx, expected_buffer_version):
                checks["count"] += 1
                return checks["count"] < 4

            try:
                routes.companion_graph = graph
                routes.event_gate = gate
                routes._emit_llm_started = noop_async
                routes._emit_message = fake_emit_message
                routes._emit_state = fake_emit_state
                routes._emit_conversation_changed = noop_async
                routes._wait_until_user_not_composing = lambda ctx, expected_buffer_version=None: asyncio.sleep(0, result=True)
                routes._apply_recent_repetition_guard = lambda ctx, decision: (decision, [])
                routes._record_prompt_cache_debug = lambda *args, **kwargs: None
                routes._record_assistant_send = lambda *args, **kwargs: None
                routes._record_job_state = lambda *args, **kwargs: None
                gate.is_send_group_current = fake_is_send_group_current

                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job_marker_stale",
                    snapshot=SimpleNamespace(
                        session_id="default",
                        snapshot_id=1,
                        buffer_version=10,
                        events=[{"text": "哈哈哈"}],
                    ),
                )
                await routes.on_decision(ctx)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            assistant_messages = [item for item in emitted if item.get("type") == "assistant_message"]
            self.assertEqual([item["content"] for item in assistant_messages], ["先说这句"])
            self.assertFalse(gate.sent)
            self.assertTrue(gate.stale)
            self.assertEqual(states[-1]["result"], "stale_dropped")
            self.assertEqual(states[-1]["send_index"], 1)
            committed = [item for _, batch in graph.committed for item in batch]
            self.assertEqual([item.type for item in committed], [SendItemType.TEXT])

        asyncio.run(scenario())

    def test_text_split_is_display_only_and_history_keeps_original_item(self):
        async def scenario():
            original_text = "刚在阳台给花浇水呢，晚霞好漂亮，想叫你来看"
            decision = ActionDecision(
                action=Action.REPLY,
                items=[SendItem(type=SendItemType.TEXT, content=original_text)],
            )
            graph = FakeGraph(decision)
            gate = FakeGate()
            emitted = []

            originals = {
                "companion_graph": routes.companion_graph,
                "event_gate": routes.event_gate,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_message": routes._emit_message,
                "_emit_state": routes._emit_state,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_wait_until_user_not_composing": routes._wait_until_user_not_composing,
                "_apply_recent_repetition_guard": routes._apply_recent_repetition_guard,
                "_record_prompt_cache_debug": routes._record_prompt_cache_debug,
                "_record_assistant_send": routes._record_assistant_send,
                "_record_job_state": routes._record_job_state,
            }

            async def noop_async(*args, **kwargs):
                pass

            async def fake_emit_message(data):
                emitted.append(data)

            async def fake_wait(ctx, expected_buffer_version=None):
                return True

            try:
                routes.companion_graph = graph
                routes.event_gate = gate
                routes._emit_llm_started = noop_async
                routes._emit_message = fake_emit_message
                routes._emit_state = noop_async
                routes._emit_conversation_changed = noop_async
                routes._wait_until_user_not_composing = fake_wait
                routes._apply_recent_repetition_guard = lambda ctx, decision: (decision, [])
                routes._record_prompt_cache_debug = lambda *args, **kwargs: None
                routes._record_assistant_send = lambda *args, **kwargs: None
                routes._record_job_state = lambda *args, **kwargs: None

                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job2",
                    snapshot=SimpleNamespace(
                        session_id="default",
                        snapshot_id=1,
                        buffer_version=10,
                        events=[{"text": "刚才干嘛去了"}],
                    ),
                )
                await routes.on_decision(ctx)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            assistant_messages = [item for item in emitted if item.get("type") == "assistant_message"]
            self.assertEqual([item["content"] for item in assistant_messages], [
                "刚在阳台给花浇水呢，",
                "晚霞好漂亮，想叫你来看",
            ])
            self.assertEqual([item["send_index"] for item in assistant_messages], [0, 1])
            self.assertEqual([item["send_count"] for item in assistant_messages], [2, 2])
            self.assertTrue(gate.sent)
            self.assertEqual(graph.committed[0][0], "turn")
            self.assertEqual(len(graph.committed[0][1]), 1)
            self.assertEqual(graph.committed[0][1][0].content, original_text)

        asyncio.run(scenario())

    def test_internal_output_guard_drops_tool_payload_before_send(self):
        async def scenario():
            decision = ActionDecision(
                action=Action.REPLY,
                items=[
                    SendItem(type=SendItemType.TEXT, content="<tool_call>"),
                    SendItem(type=SendItemType.TEXT, content="<tool_name>run_background_process</tool_name>"),
                    SendItem(type=SendItemType.TEXT, content='{"message_type": "STICKER_PROCESSED_USER_INPUT"}'),
                    SendItem(type=SendItemType.TEXT, content="[[quote]]"),
                    SendItem(type=SendItemType.TEXT, content="confused:pout&&"),
                ],
            )
            graph = FakeGraph(decision)
            gate = FakeGate()
            emitted = []
            guard_logs = []

            originals = {
                "companion_graph": routes.companion_graph,
                "event_gate": routes.event_gate,
                "_emit_llm_started": routes._emit_llm_started,
                "_emit_llm_finished": routes._emit_llm_finished,
                "_emit_message": routes._emit_message,
                "_emit_state": routes._emit_state,
                "_emit_conversation_changed": routes._emit_conversation_changed,
                "_wait_until_user_not_composing": routes._wait_until_user_not_composing,
                "_apply_recent_repetition_guard": routes._apply_recent_repetition_guard,
                "_record_prompt_cache_debug": routes._record_prompt_cache_debug,
                "_record_assistant_send": routes._record_assistant_send,
                "_record_decision_drop": routes._record_decision_drop,
                "_record_job_state": routes._record_job_state,
                "_record_internal_output_guard_filter": routes._record_internal_output_guard_filter,
            }

            async def noop_async(*args, **kwargs):
                pass

            async def fake_emit_message(data):
                emitted.append(data)

            async def fake_wait(ctx, expected_buffer_version=None):
                return True

            try:
                routes.companion_graph = graph
                routes.event_gate = gate
                routes._emit_llm_started = noop_async
                routes._emit_llm_finished = noop_async
                routes._emit_message = fake_emit_message
                routes._emit_state = noop_async
                routes._emit_conversation_changed = noop_async
                routes._wait_until_user_not_composing = fake_wait
                routes._apply_recent_repetition_guard = lambda ctx, decision: (decision, [])
                routes._record_prompt_cache_debug = lambda *args, **kwargs: None
                routes._record_assistant_send = lambda *args, **kwargs: None
                routes._record_decision_drop = lambda *args, **kwargs: None
                routes._record_job_state = lambda *args, **kwargs: None
                routes._record_internal_output_guard_filter = (
                    lambda ctx, decision, removed: guard_logs.append((decision, removed))
                )

                ctx = SimpleNamespace(
                    gate=gate,
                    job_id="job_internal_leak",
                    snapshot=SimpleNamespace(
                        session_id="default",
                        snapshot_id=1,
                        buffer_version=10,
                        events=[{"text": "小夏"}],
                    ),
                )
                await routes.on_decision(ctx)
            finally:
                for name, value in originals.items():
                    setattr(routes, name, value)

            assistant_messages = [item for item in emitted if item.get("type") == "assistant_message"]
            self.assertEqual(assistant_messages, [])
            self.assertEqual(graph.committed, [])
            self.assertEqual(graph.reset_reasons, ["internal_output_guard_filtered"])
            self.assertFalse(gate.sent)
            self.assertEqual(guard_logs[0][0].action, Action.WAIT)
            self.assertEqual(len(guard_logs[0][1]), 5)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
