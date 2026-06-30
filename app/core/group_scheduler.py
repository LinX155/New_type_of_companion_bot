import asyncio
import inspect
import random
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from .events import ChatEvent
from .sessions import normalize_session_id


# Group roll tuning entrypoint. These values are intentionally declared near
# the top so they can be edited directly while the behavior is tuned.
DEFAULT_GROUP_ROLL_CONFIG = {
    "mention_pending_seconds": 120.0,
    "roll_cooldown_minutes": 30.0,  # X
    "roll_attempt_cooldown_seconds": 60.0,
    "roll_random_min": 0.0,
    "roll_random_max": 10.0,
    "base_threshold": 8.8,  # b
    "message_count_high": 12,  # A: over this, add q1
    "message_count_low": 4,  # B: under this, subtract p1
    "density_q1": 1.25,
    "image_q2": 1.10,  # q2 < q1
    "low_activity_p1": 1.40,
    "meme_ratio_threshold": 0.55,  # o
    "meme_p2": 1.15,  # p2 < p1
    "roll_window_minutes": 20.0,
    "model_window_limit": 50,
    "max_stale_reruns": 1,
}


DecisionCallback = Callable[[str, dict, list[dict]], Awaitable[dict] | dict]
SendCallback = Callable[[str, dict, dict], Awaitable[dict] | dict]
DebugCallback = Callable[[dict], Awaitable[None] | None]
ActivityCallback = Callable[[str, float, datetime], dict]
RandomFunc = Callable[[float, float], float]
NowFunc = Callable[[], datetime]


@dataclass
class GroupReplyTrigger:
    trigger_id: str
    session_id: str
    reason: str
    source_event_id: str
    source_message_id: str
    source_buffer_version: int
    scheduled_at: str
    due_at: str
    reply_to_message_id: Optional[str]
    params_snapshot: dict
    stats: dict
    roll: Optional[dict] = None
    stale_rerun_count: int = 0

    def to_payload(self) -> dict:
        payload = {
            "trigger_id": self.trigger_id,
            "session_id": self.session_id,
            "reason": self.reason,
            "source_event_id": self.source_event_id,
            "source_message_id": self.source_message_id,
            "source_buffer_version": self.source_buffer_version,
            "scheduled_at": self.scheduled_at,
            "due_at": self.due_at,
            "reply_to_message_id": self.reply_to_message_id,
            "params_snapshot": deepcopy(self.params_snapshot),
            "stats": deepcopy(self.stats),
            "stale_rerun_count": self.stale_rerun_count,
        }
        if self.roll is not None:
            payload["roll"] = deepcopy(self.roll)
        return payload


class GroupReplyScheduler:
    """Group trigger scheduler with phase 9 activity-aware cooldowns.

    It runs LLM trigger decisions first, then optionally hands a non-stale
    candidate to the group send layer.
    """

    def __init__(
        self,
        *,
        group_buffer,
        decision_callback: DecisionCallback,
        send_callback: Optional[SendCallback] = None,
        debug_callback: Optional[DebugCallback] = None,
        activity_callback: Optional[ActivityCallback] = None,
        config: Optional[dict] = None,
        random_func: Optional[RandomFunc] = None,
        now_func: Optional[NowFunc] = None,
        create_tasks: bool = True,
    ):
        self.group_buffer = group_buffer
        self.decision_callback = decision_callback
        self.send_callback = send_callback
        self.debug_callback = debug_callback
        self.activity_callback = activity_callback
        self.config = normalize_group_roll_config(config)
        self.random_func = random_func or random.uniform
        self.now_func = now_func or datetime.now
        self.create_tasks = create_tasks
        self._pending_by_session: dict[str, GroupReplyTrigger] = {}
        self._tasks_by_session: dict[str, asyncio.Task] = {}
        self._last_candidate_at: dict[str, datetime] = {}
        self._last_roll_attempt_at: dict[str, datetime] = {}
        self._last_decision_by_session: dict[str, dict] = {}

    async def on_group_event(self, event: ChatEvent, group_window: dict) -> dict:
        session_id = normalize_session_id(event.session_id)
        if _group_event_from_bot(event):
            result = {"status": "skipped", "reason": "self_message", "session_id": session_id}
            await self._debug("group_reply_trigger_skipped", result)
            return result

        stats = self._window_stats(session_id)
        if group_event_mentions_bot(event):
            return await self._schedule_from_event(
                event,
                group_window,
                reason="mention",
                delay_seconds=self.config["mention_pending_seconds"],
                stats=stats,
                roll=None,
            )

        pending = self._pending_by_session.get(session_id)
        if pending and pending.reason == "mention":
            result = {
                "status": "skipped",
                "reason": "mention_pending",
                "session_id": session_id,
                "pending_trigger_id": pending.trigger_id,
            }
            await self._debug("group_reply_trigger_skipped", result)
            return result

        roll = self.evaluate_roll(session_id, stats)
        if not roll["should_schedule"]:
            result = {
                "status": "skipped",
                "reason": "roll_not_selected",
                "session_id": session_id,
                "roll": roll,
            }
            await self._debug("group_reply_trigger_skipped", result)
            return result

        self._last_roll_attempt_at[session_id] = self.now_func()
        return await self._schedule_from_event(
            event,
            group_window,
            reason="roll",
            delay_seconds=0.0,
            stats=stats,
            roll=roll,
        )

    def evaluate_roll(self, session_id: str, stats: dict) -> dict:
        now = self.now_func()
        last_candidate_at = self._last_candidate_at.get(session_id)
        activity = self._activity_adjustment(session_id, now)
        cooldown_seconds = float(activity["roll_cooldown_minutes"]) * 60.0
        if last_candidate_at and (now - last_candidate_at).total_seconds() < cooldown_seconds:
            return {
                "eligible": False,
                "should_schedule": False,
                "skip_reason": "candidate_cooldown",
                "last_candidate_at": last_candidate_at.isoformat(),
                "cooldown_seconds": cooldown_seconds,
                "activity": activity,
            }

        last_roll_at = self._last_roll_attempt_at.get(session_id)
        attempt_cooldown = float(self.config["roll_attempt_cooldown_seconds"])
        if last_roll_at and (now - last_roll_at).total_seconds() < attempt_cooldown:
            return {
                "eligible": False,
                "should_schedule": False,
                "skip_reason": "roll_attempt_cooldown",
                "last_roll_attempt_at": last_roll_at.isoformat(),
                "cooldown_seconds": attempt_cooldown,
            }

        roll_min = float(self.config["roll_random_min"])
        roll_max = float(self.config["roll_random_max"])
        if roll_max < roll_min:
            roll_min, roll_max = roll_max, roll_min
        raw_roll_value = float(self.random_func(roll_min, roll_max))
        adjusted_roll_value, operations = self._adjust_roll_value(raw_roll_value, stats)
        threshold_value = float(self.config["base_threshold"])
        return {
            "eligible": True,
            "should_schedule": adjusted_roll_value > threshold_value,
            "roll_value": adjusted_roll_value,
            "raw_roll_value": raw_roll_value,
            "adjusted_roll_value": adjusted_roll_value,
            "threshold_value": threshold_value,
            "comparison": "adjusted_roll_value > base_threshold",
            "adjustment_mode": "add_q_subtract_p",
            "operations": operations,
            "params_snapshot": deepcopy(self.config),
            "activity": activity,
            "stats": deepcopy(stats),
        }

    def status(self, session_id: Optional[str] = None) -> dict:
        if session_id:
            sid = normalize_session_id(session_id)
            pending = self._pending_by_session.get(sid)
            return {
                "enabled": True,
                "phase": 9 if self.send_callback else 5,
                "send_layer_enabled": self.send_callback is not None,
                "activity_dynamic_cooldown_enabled": self.activity_callback is not None,
                "session_id": sid,
                "pending": pending.to_payload() if pending else None,
                "last_decision": deepcopy(self._last_decision_by_session.get(sid)),
                "last_candidate_at": _dt_to_text(self._last_candidate_at.get(sid)),
                "last_roll_attempt_at": _dt_to_text(self._last_roll_attempt_at.get(sid)),
                "config": deepcopy(self.config),
            }
        return {
            "enabled": True,
            "phase": 9 if self.send_callback else 5,
            "send_layer_enabled": self.send_callback is not None,
            "activity_dynamic_cooldown_enabled": self.activity_callback is not None,
            "sessions": {
                sid: {
                    "pending": pending.to_payload(),
                    "last_decision": deepcopy(self._last_decision_by_session.get(sid)),
                    "last_candidate_at": _dt_to_text(self._last_candidate_at.get(sid)),
                    "last_roll_attempt_at": _dt_to_text(self._last_roll_attempt_at.get(sid)),
                }
                for sid, pending in sorted(self._pending_by_session.items())
            },
            "config": deepcopy(self.config),
        }

    async def run_pending_now(self, session_id: str) -> Optional[dict]:
        sid = normalize_session_id(session_id)
        trigger = self._pending_by_session.get(sid)
        if not trigger:
            return None
        task = self._tasks_by_session.pop(sid, None)
        if task:
            task.cancel()
        return await self._run_trigger(trigger)

    async def clear(self, session_id: Optional[str] = None):
        if session_id:
            sid = normalize_session_id(session_id)
            task = self._tasks_by_session.pop(sid, None)
            if task:
                task.cancel()
            self._pending_by_session.pop(sid, None)
            self._last_candidate_at.pop(sid, None)
            self._last_roll_attempt_at.pop(sid, None)
            self._last_decision_by_session.pop(sid, None)
            return
        for task in self._tasks_by_session.values():
            task.cancel()
        self._tasks_by_session.clear()
        self._pending_by_session.clear()
        self._last_candidate_at.clear()
        self._last_roll_attempt_at.clear()
        self._last_decision_by_session.clear()

    async def _schedule_from_event(
        self,
        event: ChatEvent,
        group_window: dict,
        *,
        reason: str,
        delay_seconds: float,
        stats: dict,
        roll: Optional[dict],
        stale_rerun_count: int = 0,
    ) -> dict:
        now = self.now_func()
        due_at = now + timedelta(seconds=max(0.0, float(delay_seconds or 0.0)))
        session_id = normalize_session_id(event.session_id)
        raw = event.raw if isinstance(event.raw, dict) else {}
        trigger = GroupReplyTrigger(
            trigger_id=f"group_{reason}_{uuid.uuid4().hex[:10]}",
            session_id=session_id,
            reason=reason,
            source_event_id=event.event_id,
            source_message_id=str(raw.get("onebot_message_id") or ""),
            source_buffer_version=int(group_window.get("buffer_version") or 0),
            scheduled_at=now.isoformat(),
            due_at=due_at.isoformat(),
            reply_to_message_id=str(raw.get("onebot_message_id") or "") if reason == "mention" else None,
            params_snapshot=deepcopy(self.config),
            stats=deepcopy(stats),
            roll=deepcopy(roll),
            stale_rerun_count=stale_rerun_count,
        )
        return await self._schedule_trigger(trigger, delay_seconds=max(0.0, float(delay_seconds or 0.0)))

    async def _schedule_trigger(self, trigger: GroupReplyTrigger, delay_seconds: float) -> dict:
        previous = self._tasks_by_session.pop(trigger.session_id, None)
        if previous:
            previous.cancel()
        self._pending_by_session[trigger.session_id] = trigger
        if self.create_tasks:
            self._tasks_by_session[trigger.session_id] = asyncio.create_task(
                self._sleep_and_run(trigger, delay_seconds)
            )
        result = {
            "status": "scheduled",
            "session_id": trigger.session_id,
            "trigger": trigger.to_payload(),
            "delay_seconds": delay_seconds,
        }
        await self._debug("group_reply_trigger_scheduled", result)
        return result

    async def _sleep_and_run(self, trigger: GroupReplyTrigger, delay_seconds: float):
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await self._run_trigger(trigger)
        except asyncio.CancelledError:
            await self._debug(
                "group_reply_trigger_cancelled",
                {"session_id": trigger.session_id, "trigger": trigger.to_payload()},
            )

    async def _run_trigger(self, trigger: GroupReplyTrigger) -> dict:
        current = self._pending_by_session.get(trigger.session_id)
        if current and current.trigger_id != trigger.trigger_id:
            return {"status": "skipped", "reason": "superseded", "trigger_id": trigger.trigger_id}

        self._pending_by_session.pop(trigger.session_id, None)
        self._tasks_by_session.pop(trigger.session_id, None)

        start_panel = self.group_buffer.status(trigger.session_id)
        start_version = int(start_panel.get("buffer_version") or 0)
        model_window = self.group_buffer.get_model_window(
            trigger.session_id,
            limit=int(self.config["model_window_limit"]),
        )
        decision_trigger = trigger.to_payload()
        decision_trigger["request_buffer_version"] = start_version
        await self._debug(
            "group_reply_decision_started",
            {
                "session_id": trigger.session_id,
                "trigger": decision_trigger,
                "model_window_count": len(model_window),
            },
        )

        try:
            decision = await _maybe_await(self.decision_callback(trigger.session_id, decision_trigger, model_window))
        except Exception as exc:
            result = {
                "status": "error",
                "session_id": trigger.session_id,
                "trigger": decision_trigger,
                "error_message": str(exc),
            }
            self._last_decision_by_session[trigger.session_id] = deepcopy(result)
            await self._debug("group_reply_decision_error", result)
            return result
        if not isinstance(decision, dict):
            decision = {
                "status": "invalid_decision_result",
                "should_send_candidate": False,
                "send_enabled": False,
                "errors": ["decision_callback did not return a dict"],
            }

        end_panel = self.group_buffer.status(trigger.session_id)
        end_version = int(end_panel.get("buffer_version") or 0)
        if end_version != start_version:
            result = {
                "status": "stale_dropped",
                "session_id": trigger.session_id,
                "trigger": decision_trigger,
                "start_buffer_version": start_version,
                "end_buffer_version": end_version,
                "decision": deepcopy(decision),
            }
            self._last_decision_by_session[trigger.session_id] = deepcopy(result)
            await self._debug("group_reply_decision_stale", result)
            if (
                trigger.reason == "mention"
                and trigger.stale_rerun_count < int(self.config["max_stale_reruns"])
            ):
                rerun = GroupReplyTrigger(
                    **{
                        **trigger.__dict__,
                        "trigger_id": f"group_{trigger.reason}_{uuid.uuid4().hex[:10]}",
                        "source_buffer_version": end_version,
                        "scheduled_at": self.now_func().isoformat(),
                        "due_at": self.now_func().isoformat(),
                        "stats": self._window_stats(trigger.session_id),
                        "stale_rerun_count": trigger.stale_rerun_count + 1,
                    }
                )
                await self._schedule_trigger(rerun, delay_seconds=0.0)
            return result

        send_result = None
        if decision.get("should_send_candidate") and self.send_callback:
            try:
                send_result = await _maybe_await(self.send_callback(trigger.session_id, decision_trigger, decision))
            except Exception as exc:
                send_result = {
                    "status": "error",
                    "reason": "group_send_callback_error",
                    "error_message": str(exc),
                }

        if decision.get("should_send_candidate"):
            if send_result:
                if send_result.get("status") == "sent":
                    status = "sent"
                elif send_result.get("status") == "error":
                    status = "send_error"
                else:
                    status = "send_skipped"
            else:
                status = "candidate_ready_not_sent"
        else:
            status = "wait"
        result = {
            "status": status,
            "session_id": trigger.session_id,
            "trigger": decision_trigger,
            "decision": deepcopy(decision),
        }
        if send_result is not None:
            result["send_result"] = deepcopy(send_result)
        if decision.get("should_send_candidate"):
            self._last_candidate_at[trigger.session_id] = self.now_func()
        self._last_decision_by_session[trigger.session_id] = deepcopy(result)
        await self._debug("group_reply_decision_finished", result)
        return result

    def _adjust_roll_value(self, roll_value: float, stats: dict) -> tuple[float, list[dict]]:
        adjusted = float(roll_value)
        operations: list[dict] = []
        count = int(stats.get("event_count") or 0)
        low = int(self.config["message_count_low"])
        high = max(low, int(self.config["message_count_high"]))

        if count > high:
            delta = float(self.config["density_q1"])
            adjusted += delta
            operations.append({
                "reason": "message_count_above_A",
                "op": "add",
                "value": delta,
                "value_after": adjusted,
            })

        if count < low:
            delta = float(self.config["low_activity_p1"])
            adjusted -= delta
            operations.append({
                "reason": "message_count_below_B",
                "op": "subtract",
                "value": delta,
                "value_after": adjusted,
            })

        if int(stats.get("image_count") or 0) > 0:
            delta = float(self.config["image_q2"])
            adjusted += delta
            operations.append({
                "reason": "image_event_present",
                "op": "add",
                "value": delta,
                "value_after": adjusted,
            })

        if float(stats.get("meme_ratio") or 0.0) > float(self.config["meme_ratio_threshold"]):
            delta = float(self.config["meme_p2"])
            adjusted -= delta
            operations.append({
                "reason": "meme_ratio_above_o",
                "op": "subtract",
                "value": delta,
                "value_after": adjusted,
            })

        return adjusted, operations

    def _window_stats(self, session_id: str) -> dict:
        now = self.now_func()
        all_entries = self.group_buffer.get_window(session_id)
        since = now - timedelta(minutes=float(self.config["roll_window_minutes"]))
        recent = [
            entry for entry in all_entries
            if _entry_timestamp(entry) is None or _entry_timestamp(entry) >= since
        ]
        media_items = [
            media
            for entry in recent
            for media in (entry.get("media") or [])
            if isinstance(media, dict)
        ]
        event_count = len(recent)
        meme_count = sum(1 for entry in recent if entry.get("meme"))
        image_count = sum(1 for media in media_items if media.get("kind") == "image")
        return {
            "event_count": event_count,
            "text_count": sum(1 for entry in recent if entry.get("text")),
            "image_count": image_count,
            "meme_count": meme_count,
            "meme_ratio": (meme_count / event_count) if event_count else 0.0,
            "window_minutes": float(self.config["roll_window_minutes"]),
            "buffered_events_total": len(all_entries),
        }

    def _activity_adjustment(self, session_id: str, now: datetime) -> dict:
        base_minutes = float(self.config["roll_cooldown_minutes"])
        if not self.activity_callback:
            return {
                "enabled": False,
                "session_id": normalize_session_id(session_id),
                "label": "normal",
                "base_roll_cooldown_minutes": base_minutes,
                "roll_cooldown_minutes": base_minutes,
                "multiplier": 1.0,
                "reason": "no_activity_callback",
            }
        try:
            result = self.activity_callback(session_id, base_minutes, now) or {}
        except Exception as exc:
            return {
                "enabled": False,
                "session_id": normalize_session_id(session_id),
                "label": "normal",
                "base_roll_cooldown_minutes": base_minutes,
                "roll_cooldown_minutes": base_minutes,
                "multiplier": 1.0,
                "reason": "activity_callback_error",
                "error_message": str(exc),
            }
        try:
            result["roll_cooldown_minutes"] = max(0.0, float(result.get("roll_cooldown_minutes")))
        except (TypeError, ValueError):
            result["roll_cooldown_minutes"] = base_minutes
        result.setdefault("enabled", True)
        result.setdefault("session_id", normalize_session_id(session_id))
        result.setdefault("label", "normal")
        result.setdefault("base_roll_cooldown_minutes", base_minutes)
        result.setdefault("multiplier", 1.0)
        return result

    async def _debug(self, event_type: str, payload: dict):
        if not self.debug_callback:
            return
        data = {"event_type": event_type, **deepcopy(payload)}
        await _maybe_await(self.debug_callback(data))


def normalize_group_roll_config(config: Optional[dict] = None) -> dict:
    raw_config = config or {}
    merged = {**DEFAULT_GROUP_ROLL_CONFIG, **raw_config}
    normalized = {
        "mention_pending_seconds": max(0.0, float(merged["mention_pending_seconds"])),
        "roll_cooldown_minutes": max(0.0, float(merged["roll_cooldown_minutes"])),
        "roll_attempt_cooldown_seconds": max(0.0, float(merged["roll_attempt_cooldown_seconds"])),
        "roll_random_min": float(merged["roll_random_min"]),
        "roll_random_max": float(merged["roll_random_max"]),
        "base_threshold": float(merged["base_threshold"]),
        "message_count_low": max(0, int(merged["message_count_low"])),
        "message_count_high": max(0, int(merged["message_count_high"])),
        "density_q1": _normalize_roll_delta(
            _merged_roll_delta(raw_config, merged, "density_q1", "density_coeff")
        ),
        "image_q2": _normalize_roll_delta(
            _merged_roll_delta(raw_config, merged, "image_q2", "image_coeff")
        ),
        "low_activity_p1": _normalize_roll_delta(
            _merged_roll_delta(raw_config, merged, "low_activity_p1", "low_activity_penalty")
        ),
        "meme_ratio_threshold": max(0.0, min(1.0, float(merged["meme_ratio_threshold"]))),
        "meme_p2": _normalize_roll_delta(
            _merged_roll_delta(raw_config, merged, "meme_p2", "meme_penalty")
        ),
        "roll_window_minutes": max(0.0, float(merged["roll_window_minutes"])),
        "model_window_limit": max(1, int(merged["model_window_limit"])),
        "max_stale_reruns": max(0, int(merged["max_stale_reruns"])),
    }
    return normalized


def _merged_roll_delta(raw_config: dict, merged: dict, primary: str, legacy: str) -> float:
    if primary in raw_config:
        return float(merged[primary])
    if legacy not in raw_config:
        return float(merged[primary])
    legacy_value = float(raw_config.get(legacy, 1.0))
    if 0.0 <= legacy_value < 1.0:
        return 1.0 + legacy_value
    return legacy_value


def _normalize_roll_delta(value: float) -> float:
    return max(1.000001, float(value))


def group_event_mentions_bot(event: ChatEvent) -> bool:
    raw = event.raw if isinstance(event.raw, dict) else {}
    return bool(raw.get("mentions_bot"))


def _group_event_from_bot(event: ChatEvent) -> bool:
    raw = event.raw if isinstance(event.raw, dict) else {}
    self_id = str(raw.get("qq_self_id") or "").strip()
    user_id = str(raw.get("qq_user_id") or event.user_id or "").strip()
    return bool(self_id and user_id and self_id == user_id)


def _entry_timestamp(entry: dict) -> Optional[datetime]:
    value = entry.get("timestamp") if isinstance(entry, dict) else None
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _dt_to_text(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None
