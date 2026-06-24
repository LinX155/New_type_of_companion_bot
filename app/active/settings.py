"""User-level active message schedule extraction helpers."""
import json
import re
from datetime import datetime, timedelta
from typing import Optional


ACTIVE_SETTING_NONE = {"type": "none", "time": None}
ACTIVE_SETTING_TYPES = {"none", "next", "daily"}

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)
_HHMM_COLON_RE = re.compile(r"(?<!\d)(?P<hour>[01]?\d|2[0-3])\s*[:：]\s*(?P<minute>[0-5]?\d)(?!\d)")
_HHMM_POINT_RE = re.compile(r"(?<!\d)(?P<hour>[01]?\d|2[0-3])\s*点\s*(?P<minute>[0-5]?\d)?\s*分?")

_TIME_HINT_RE = re.compile(
    r"([01]?\d|2[0-3])\s*[:：]\s*[0-5]?\d|"
    r"([01]?\d|2[0-3])\s*点|"
    r"(早上|上午|中午|下午|晚上|今晚|明早|明晚|凌晨)"
)
_SETTING_HINTS = (
    "主动消息",
    "主动找",
    "主动来",
    "提醒我",
    "叫我",
    "喊我",
    "来找我",
    "找我",
    "联系我",
)
_DAILY_HINTS = ("每天", "每日", "固定", "每次", "以后", "今后", "往后", "天天")
_NEXT_HINTS = ("明天", "明早", "明晚", "今晚", "今天", "下次", "等会", "一会", "待会", "稍后", "提醒我")


def looks_like_active_message_setting_request(text: str) -> bool:
    content = _strip_command_prefix(text or "")
    if not content or not _TIME_HINT_RE.search(content):
        return False
    return (
        any(hint in content for hint in _SETTING_HINTS)
        or any(hint in content for hint in _DAILY_HINTS)
        or ("提醒" in content and "我" in content)
    )


def parse_active_message_setting_output(raw_output: str, now: Optional[datetime] = None) -> dict:
    data = _parse_json_object(raw_output)
    return normalize_active_message_setting(data, now=now)


def normalize_active_message_setting(data: dict, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now()
    setting_type = str(data.get("type") or "none").strip().lower()
    value = data.get("time")

    if setting_type not in ACTIVE_SETTING_TYPES:
        return dict(ACTIVE_SETTING_NONE)
    if setting_type == "none":
        return dict(ACTIVE_SETTING_NONE)

    if setting_type == "daily":
        normalized = _normalize_daily_time(value)
        if not normalized:
            return dict(ACTIVE_SETTING_NONE)
        return {"type": "daily", "time": normalized}

    normalized_next = _normalize_next_time(value, now)
    if not normalized_next:
        return dict(ACTIVE_SETTING_NONE)
    return {"type": "next", "time": normalized_next}


def fallback_active_message_setting_from_text(text: str, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now()
    content = _strip_command_prefix(text or "")
    if not looks_like_active_message_setting_request(content):
        return dict(ACTIVE_SETTING_NONE)

    parsed_time = _extract_arabic_time(content)
    if not parsed_time:
        return dict(ACTIVE_SETTING_NONE)

    hour, minute = parsed_time
    if any(hint in content for hint in _DAILY_HINTS) or (
        "主动消息" in content and not any(hint in content for hint in _NEXT_HINTS)
    ):
        return {"type": "daily", "time": f"{hour:02d}:{minute:02d}"}

    if not any(hint in content for hint in _NEXT_HINTS):
        return dict(ACTIVE_SETTING_NONE)

    days = 1 if any(hint in content for hint in ("明天", "明早", "明晚")) else 0
    candidate = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if days == 0 and candidate < now:
        candidate += timedelta(days=1)
    return {"type": "next", "time": candidate.strftime("%Y-%m-%d %H:%M")}


def format_active_message_setting_response(setting: dict, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    setting_type = setting.get("type")
    value = setting.get("time")
    if setting_type == "daily":
        return f"好，我以后每天 {value} 左右来找你。"
    if setting_type == "next":
        dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M")
        return f"好，那我{_format_next_time_for_user(dt, now)}左右来找你。"
    return ""


def _parse_json_object(raw_output: str) -> dict:
    text = (raw_output or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def _normalize_daily_time(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"


def _normalize_next_time(value, now: datetime) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    if parsed < now.replace(second=0, microsecond=0):
        return None
    return parsed.strftime("%Y-%m-%d %H:%M")


def _extract_arabic_time(text: str) -> Optional[tuple[int, int]]:
    match = _HHMM_COLON_RE.search(text)
    if not match:
        match = _HHMM_POINT_RE.search(text)
    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    prefix = text[max(0, match.start() - 4):match.start()]
    if any(word in prefix for word in ("下午", "晚上", "今晚", "明晚")) and hour < 12:
        hour += 12
    if "中午" in prefix and hour < 11:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _format_next_time_for_user(dt: datetime, now: datetime) -> str:
    today = now.date()
    if dt.date() == today:
        prefix = "今天"
    elif dt.date() == (today + timedelta(days=1)):
        prefix = "明天"
    else:
        prefix = dt.strftime("%m-%d")
    return f"{prefix} {dt.strftime('%H:%M')} "


def _strip_command_prefix(text: str) -> str:
    stripped = (text or "").strip()
    lowered = stripped.lower()
    if lowered.startswith("/mem"):
        return stripped[4:].lstrip("：: ").strip()
    return stripped
