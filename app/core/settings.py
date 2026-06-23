"""用户配置持久化。

前端保存的配置（API 配置、热聊时长、记忆调度）会写入 data/user_settings.json，
下次重启时自动加载，避免每次都要重新填写。
当前阶段 API key 明文存储（文件已被 .gitignore 排除，不会进版本库）。
"""
import json
import os
from threading import Lock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SETTINGS_PATH = os.path.join(ROOT_DIR, "data", "user_settings.json")
_lock = Lock()

_DEFAULTS = {
    "api_key": "",
    "base_url": "",
    "model": "",
    "thinking_enabled": True,
    "hot_duration_minutes": 30,
    "memory_analysis_day": {"hour": 13, "minute": 0},
    "memory_analysis_night": {"hour": 19, "minute": 0},
    "midnight_cleanup": {"hour": 3, "minute": 30},
    "active_message": {
        "enabled": False,
        "hour": 10,
        "minute": 0,
        "daily_limit": 1,
        "quiet_start_hour": 0,
        "quiet_end_hour": 9,
    },
}


def load_settings() -> dict:
    """读取持久化配置，缺失字段用默认值补齐。"""
    merged = {k: (v.copy() if isinstance(v, dict) else v) for k, v in _DEFAULTS.items()}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            merged.update(data)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    except Exception as e:
        print(f"[settings] load failed: {e}")
    return merged


def save_settings(updates: dict) -> bool:
    """合并写入部分字段。"""
    with _lock:
        current = load_settings()
        current.update(updates)
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(current, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"[settings] save failed: {e}")
            return False
