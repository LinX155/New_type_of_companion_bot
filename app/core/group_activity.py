from copy import deepcopy
from datetime import datetime, timedelta
from typing import Optional

from .sessions import normalize_session_id


DEFAULT_GROUP_ACTIVITY_CONFIG = {
    "enabled": True,
    "update_hour": 6,
    "ema_alpha": 0.35,
    "active_threshold_ratio": 1.35,
    "quiet_threshold_ratio": 0.55,
    "active_cooldown_multiplier": 0.7,
    "quiet_cooldown_multiplier": 1.4,
    "normal_cooldown_multiplier": 1.0,
    "min_roll_cooldown_minutes": 5.0,
    "max_roll_cooldown_minutes": 180.0,
}


DAY_TYPES = ("weekday", "weekend")


class GroupActivityTracker:
    def __init__(self, config: Optional[dict] = None, now_func=None):
        self.config = normalize_group_activity_config(config)
        self.now_func = now_func or datetime.now
        self._profiles: dict[str, dict] = {}

    def maybe_update(self, session_id: str, entries: list[dict], now: Optional[datetime] = None, force: bool = False) -> dict:
        sid = normalize_session_id(session_id)
        current = now or self.now_func()
        if not self.config["enabled"]:
            return {"status": "skipped", "reason": "disabled", "session_id": sid}
        if not force and current.hour < int(self.config["update_hour"]):
            return {"status": "skipped", "reason": "before_update_hour", "session_id": sid}

        profile = self._profile_for(sid)
        today = current.date().isoformat()
        if not force and profile.get("last_update_date") == today:
            return {
                "status": "skipped",
                "reason": "already_updated_today",
                "session_id": sid,
                "last_update_date": today,
            }

        counts = _hour_counts_by_day_type(entries, current)
        alpha = float(self.config["ema_alpha"])
        for day_type in DAY_TYPES:
            old_scores = profile["hourly_ema"][day_type]
            day_counts = counts[day_type]
            profile["hourly_ema"][day_type] = [
                round((1.0 - alpha) * float(old_scores[hour]) + alpha * float(day_counts[hour]), 6)
                for hour in range(24)
            ]
            profile["labels"][day_type] = _labels_for_scores(
                profile["hourly_ema"][day_type],
                active_ratio=float(self.config["active_threshold_ratio"]),
                quiet_ratio=float(self.config["quiet_threshold_ratio"]),
            )

        current_type = day_type_for_datetime(current)
        profile["labels"]["active_hours"] = list(profile["labels"][current_type]["active_hours"])
        profile["labels"]["quiet_hours"] = list(profile["labels"][current_type]["quiet_hours"])
        profile["labels"]["normal_hours"] = list(profile["labels"][current_type]["normal_hours"])
        profile["last_updated_at"] = current.isoformat()
        profile["last_update_date"] = today
        profile["last_source_event_count"] = sum(sum(hours) for hours in counts.values())
        return {
            "status": "updated",
            "session_id": sid,
            "updated_at": profile["last_updated_at"],
            "day_type": current_type,
            "source_event_count": profile["last_source_event_count"],
            "labels": deepcopy(profile["labels"]),
        }

    def roll_cooldown_adjustment(
        self,
        session_id: str,
        base_minutes: float,
        now: Optional[datetime] = None,
    ) -> dict:
        sid = normalize_session_id(session_id)
        current = now or self.now_func()
        base = max(0.0, float(base_minutes))
        profile = self._profiles.get(sid)
        if not profile or not self.config["enabled"]:
            return {
                "enabled": self.config["enabled"],
                "session_id": sid,
                "label": "normal",
                "day_type": day_type_for_datetime(current),
                "hour": current.hour,
                "base_roll_cooldown_minutes": base,
                "roll_cooldown_minutes": base,
                "multiplier": 1.0,
                "reason": "no_activity_profile" if not profile else "disabled",
            }

        day_type = day_type_for_datetime(current)
        labels = profile["labels"].get(day_type) or _empty_labels()
        label = "normal"
        if current.hour in labels.get("active_hours", []):
            label = "active"
        elif current.hour in labels.get("quiet_hours", []):
            label = "quiet"

        multiplier = float(self.config[f"{label}_cooldown_multiplier"])
        adjusted = base * multiplier
        adjusted = max(float(self.config["min_roll_cooldown_minutes"]), adjusted)
        adjusted = min(float(self.config["max_roll_cooldown_minutes"]), adjusted)
        return {
            "enabled": True,
            "session_id": sid,
            "label": label,
            "day_type": day_type,
            "hour": current.hour,
            "base_roll_cooldown_minutes": base,
            "roll_cooldown_minutes": adjusted,
            "multiplier": multiplier,
            "last_updated_at": profile.get("last_updated_at"),
        }

    def status(self, session_id: Optional[str] = None) -> dict:
        if session_id:
            sid = normalize_session_id(session_id)
            profile = self._profiles.get(sid)
            return {
                "enabled": self.config["enabled"],
                "phase": 9,
                "session_id": sid,
                "profile": deepcopy(profile),
                "config": deepcopy(self.config),
            }
        return {
            "enabled": self.config["enabled"],
            "phase": 9,
            "sessions": sorted(self._profiles.keys()),
            "config": deepcopy(self.config),
        }

    def clear(self, session_id: Optional[str] = None):
        if session_id:
            self._profiles.pop(normalize_session_id(session_id), None)
            return
        self._profiles.clear()

    def _profile_for(self, session_id: str) -> dict:
        sid = normalize_session_id(session_id)
        if sid not in self._profiles:
            self._profiles[sid] = {
                "session_id": sid,
                "hourly_ema": {day_type: [0.0] * 24 for day_type in DAY_TYPES},
                "labels": {day_type: _empty_labels() for day_type in DAY_TYPES},
                "last_updated_at": None,
                "last_update_date": None,
                "last_source_event_count": 0,
            }
        return self._profiles[sid]


def normalize_group_activity_config(config: Optional[dict] = None) -> dict:
    raw = {**DEFAULT_GROUP_ACTIVITY_CONFIG, **(config or {})}
    return {
        "enabled": bool(raw["enabled"]),
        "update_hour": max(0, min(23, int(raw["update_hour"]))),
        "ema_alpha": max(0.0, min(1.0, float(raw["ema_alpha"]))),
        "active_threshold_ratio": max(0.0, float(raw["active_threshold_ratio"])),
        "quiet_threshold_ratio": max(0.0, float(raw["quiet_threshold_ratio"])),
        "active_cooldown_multiplier": max(0.01, float(raw["active_cooldown_multiplier"])),
        "quiet_cooldown_multiplier": max(0.01, float(raw["quiet_cooldown_multiplier"])),
        "normal_cooldown_multiplier": max(0.01, float(raw["normal_cooldown_multiplier"])),
        "min_roll_cooldown_minutes": max(0.0, float(raw["min_roll_cooldown_minutes"])),
        "max_roll_cooldown_minutes": max(0.0, float(raw["max_roll_cooldown_minutes"])),
    }


def day_type_for_datetime(value: datetime) -> str:
    return "weekend" if value.weekday() >= 5 else "weekday"


def _hour_counts_by_day_type(entries: list[dict], now: datetime) -> dict[str, list[int]]:
    since = now - timedelta(hours=24)
    counts = {day_type: [0] * 24 for day_type in DAY_TYPES}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        timestamp = _parse_timestamp(entry.get("timestamp"))
        if not timestamp or timestamp < since or timestamp > now:
            continue
        counts[day_type_for_datetime(timestamp)][timestamp.hour] += 1
    return counts


def _labels_for_scores(scores: list[float], active_ratio: float, quiet_ratio: float) -> dict:
    values = [max(0.0, float(score)) for score in (scores or [0.0] * 24)]
    if len(values) < 24:
        values = (values + [0.0] * 24)[:24]
    if not any(value > 0 for value in values):
        return _empty_labels()

    avg = sum(values) / 24.0
    active_threshold = max(avg * active_ratio, 0.000001)
    quiet_threshold = avg * quiet_ratio
    active = [hour for hour, value in enumerate(values) if value >= active_threshold and value > 0]
    quiet = [hour for hour, value in enumerate(values) if value <= quiet_threshold]
    normal = [hour for hour in range(24) if hour not in set(active) and hour not in set(quiet)]
    return {
        "active_hours": active,
        "quiet_hours": quiet,
        "normal_hours": normal,
    }


def _empty_labels() -> dict:
    return {
        "active_hours": [],
        "quiet_hours": [],
        "normal_hours": list(range(24)),
    }


def _parse_timestamp(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
