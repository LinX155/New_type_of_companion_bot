import json
import os
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Optional

from .sessions import SESSION_ROOT_DIR, normalize_session_id


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
GROUP_ACTIVITY_PROFILE_FILENAME = "GROUP_ACTIVITY.json"
GROUP_ACTIVITY_SCHEMA_VERSION = 1


class GroupActivityTracker:
    def __init__(self, config: Optional[dict] = None, now_func=None, base_dir: Optional[str] = None):
        self.config = normalize_group_activity_config(config)
        self.now_func = now_func or datetime.now
        self.base_dir = os.path.abspath(
            base_dir or os.path.join(os.path.dirname(__file__), "..", "..")
        )
        self.sessions_root = os.path.abspath(os.path.join(self.base_dir, SESSION_ROOT_DIR))
        self._profiles: dict[str, dict] = {}

    def profile_path(self, session_id: str) -> str:
        sid = normalize_session_id(session_id)
        return os.path.join(self.sessions_root, sid, GROUP_ACTIVITY_PROFILE_FILENAME)

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
        profile["config_snapshot"] = deepcopy(self.config)
        persist = self._write_profile(sid, profile)
        return {
            "status": "updated",
            "session_id": sid,
            "updated_at": profile["last_updated_at"],
            "day_type": current_type,
            "source_event_count": profile["last_source_event_count"],
            "labels": deepcopy(profile["labels"]),
            "profile_path": persist["path"],
            "profile_persisted": persist["ok"],
            "profile_persist_error": persist.get("error"),
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
        profile = self._profiles.get(sid) or self._load_profile(sid)
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
                "profile_path": self.profile_path(sid),
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
            "profile_path": self.profile_path(sid),
        }

    def status(self, session_id: Optional[str] = None) -> dict:
        if session_id:
            sid = normalize_session_id(session_id)
            profile = self._profiles.get(sid) or self._load_profile(sid)
            path = self.profile_path(sid)
            return {
                "enabled": self.config["enabled"],
                "phase": 9,
                "session_id": sid,
                "profile": deepcopy(profile),
                "profile_path": path,
                "profile_persisted": os.path.exists(path),
                "config": deepcopy(self.config),
            }
        return {
            "enabled": self.config["enabled"],
            "phase": 9,
            "sessions": sorted(set(self._profiles.keys()) | set(self._persisted_session_ids())),
            "config": deepcopy(self.config),
        }

    def clear(self, session_id: Optional[str] = None):
        if session_id:
            sid = normalize_session_id(session_id)
            self._profiles.pop(sid, None)
            self._remove_profile_file(sid)
            return
        self._profiles.clear()
        for sid in self._persisted_session_ids():
            self._remove_profile_file(sid)

    def _profile_for(self, session_id: str) -> dict:
        sid = normalize_session_id(session_id)
        if sid not in self._profiles:
            loaded = self._load_profile(sid)
            self._profiles[sid] = loaded or _empty_profile(sid, self.config)
        return self._profiles[sid]

    def _load_profile(self, session_id: str) -> Optional[dict]:
        sid = normalize_session_id(session_id)
        path = self.profile_path(sid)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        profile = _normalize_activity_profile(raw, sid, self.config)
        self._profiles[sid] = profile
        return profile

    def _write_profile(self, session_id: str, profile: dict) -> dict:
        sid = normalize_session_id(session_id)
        path = self.profile_path(sid)
        payload = _normalize_activity_profile(profile, sid, self.config)
        payload["config_snapshot"] = deepcopy(self.config)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, path)
            self._profiles[sid] = payload
            return {"ok": True, "path": path}
        except OSError as exc:
            return {"ok": False, "path": path, "error": str(exc)}

    def _persisted_session_ids(self) -> list[str]:
        try:
            names = os.listdir(self.sessions_root)
        except OSError:
            return []
        result = []
        for name in names:
            sid = normalize_session_id(name)
            if os.path.exists(self.profile_path(sid)):
                result.append(sid)
        return sorted(set(result))

    def _remove_profile_file(self, session_id: str):
        path = os.path.abspath(self.profile_path(session_id))
        try:
            if os.path.commonpath([self.sessions_root, path]) != self.sessions_root:
                return
        except ValueError:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError:
            return


def _empty_profile(session_id: str, config: dict) -> dict:
    sid = normalize_session_id(session_id)
    labels = {day_type: _empty_labels() for day_type in DAY_TYPES}
    labels["active_hours"] = []
    labels["quiet_hours"] = []
    labels["normal_hours"] = list(range(24))
    return {
        "schema_version": GROUP_ACTIVITY_SCHEMA_VERSION,
        "session_id": sid,
        "hourly_ema": {day_type: [0.0] * 24 for day_type in DAY_TYPES},
        "labels": labels,
        "last_updated_at": None,
        "last_update_date": None,
        "last_source_event_count": 0,
        "config_snapshot": deepcopy(config),
    }


def _normalize_activity_profile(raw, session_id: str, config: dict) -> dict:
    base = _empty_profile(session_id, config)
    if not isinstance(raw, dict):
        return base

    hourly_ema = raw.get("hourly_ema")
    if isinstance(hourly_ema, dict):
        for day_type in DAY_TYPES:
            base["hourly_ema"][day_type] = _normalized_scores(hourly_ema.get(day_type))

    labels = raw.get("labels")
    if isinstance(labels, dict):
        for day_type in DAY_TYPES:
            base["labels"][day_type] = _normalized_labels(labels.get(day_type))
        for key in ("active_hours", "quiet_hours", "normal_hours"):
            fallback = base["labels"]["weekday"].get(key, [])
            base["labels"][key] = _normalized_hour_list(labels.get(key), fallback=fallback)

    for key in ("last_updated_at", "last_update_date"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            base[key] = value

    try:
        base["last_source_event_count"] = max(0, int(raw.get("last_source_event_count", 0)))
    except (TypeError, ValueError):
        base["last_source_event_count"] = 0

    if isinstance(raw.get("config_snapshot"), dict):
        base["config_snapshot"] = deepcopy(raw["config_snapshot"])
    return base


def _normalized_scores(values) -> list[float]:
    scores = []
    if isinstance(values, list):
        for item in values[:24]:
            try:
                scores.append(max(0.0, float(item)))
            except (TypeError, ValueError):
                scores.append(0.0)
    return (scores + [0.0] * 24)[:24]


def _normalized_labels(value) -> dict:
    if not isinstance(value, dict):
        return _empty_labels()
    active = _normalized_hour_list(value.get("active_hours"))
    quiet = _normalized_hour_list(value.get("quiet_hours"))
    occupied = set(active) | set(quiet)
    normal_default = [hour for hour in range(24) if hour not in occupied]
    normal = _normalized_hour_list(value.get("normal_hours"), fallback=normal_default)
    return {
        "active_hours": active,
        "quiet_hours": quiet,
        "normal_hours": normal,
    }


def _normalized_hour_list(value, fallback: Optional[list[int]] = None) -> list[int]:
    result = []
    seen = set()
    if isinstance(value, list):
        for item in value:
            try:
                hour = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23 and hour not in seen:
                seen.add(hour)
                result.append(hour)
    if not result and fallback is not None:
        return list(fallback)
    return result


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
