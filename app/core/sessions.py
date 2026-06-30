import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .provider_identity import (
    is_valid_provider_user_seed,
    new_provider_user_seed,
    provider_user_id_for_seed,
)


WEBUI_DEFAULT_SESSION_ID = "webui_default"
LEGACY_DEFAULT_SESSION_ID = "default"
SESSION_ROOT_DIR = os.path.join("memory", "sessions")
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9_.-]+")
PROVIDER_USER_SEED_KEY = "provider_user_seed"


@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    platform: str
    user_id: str
    label: str


def normalize_session_id(value: Optional[str], default: str = WEBUI_DEFAULT_SESSION_ID) -> str:
    raw = (value or "").strip()
    if not raw or raw == LEGACY_DEFAULT_SESSION_ID:
        return default
    safe = _SAFE_SESSION_RE.sub("_", raw).strip("._-")
    return safe or default


def webui_session_id(value: Optional[str] = None) -> str:
    return normalize_session_id(value, WEBUI_DEFAULT_SESSION_ID)


def qq_private_session_id(user_id: str) -> str:
    safe_user_id = normalize_session_id(str(user_id or "unknown"), "unknown")
    return f"qq_private_{safe_user_id}"


def qq_group_session_id(group_id: str) -> str:
    safe_group_id = normalize_session_id(str(group_id or "unknown"), "unknown")
    return f"qq_group_{safe_group_id}"


def identity_for_webui(session_id: Optional[str] = None) -> SessionIdentity:
    sid = webui_session_id(session_id)
    return SessionIdentity(
        session_id=sid,
        platform="webui",
        user_id="user",
        label=sid,
    )


def identity_for_qq_private(user_id: str) -> SessionIdentity:
    sid = qq_private_session_id(user_id)
    return SessionIdentity(
        session_id=sid,
        platform="qq",
        user_id=str(user_id or "unknown"),
        label=f"QQ {user_id}",
    )


def identity_for_qq_group(group_id: str) -> SessionIdentity:
    sid = qq_group_session_id(group_id)
    return SessionIdentity(
        session_id=sid,
        platform="qq_group",
        user_id=str(group_id or "unknown"),
        label=f"QQ 群 {group_id}",
    )


def infer_identity(session_id: str) -> SessionIdentity:
    sid = normalize_session_id(session_id)
    if sid.startswith("qq_private_"):
        user_id = sid[len("qq_private_"):] or "unknown"
        return SessionIdentity(session_id=sid, platform="qq", user_id=user_id, label=f"QQ {user_id}")
    if sid.startswith("qq_group_"):
        group_id = sid[len("qq_group_"):] or "unknown"
        return SessionIdentity(session_id=sid, platform="qq_group", user_id=group_id, label=f"QQ 群 {group_id}")
    return SessionIdentity(session_id=sid, platform="webui", user_id="user", label=sid)


class SessionRegistry:
    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        self.sessions_root = os.path.join(self.base_dir, SESSION_ROOT_DIR)

    def session_dir(self, session_id: str) -> str:
        sid = normalize_session_id(session_id)
        return os.path.join(self.sessions_root, sid)

    def ensure_session(self, session_id: str, identity: Optional[SessionIdentity] = None) -> str:
        sid = normalize_session_id(session_id)
        identity = identity or infer_identity(sid)
        path = self.session_dir(sid)
        os.makedirs(path, exist_ok=True)
        os.makedirs(os.path.join(path, "dm"), exist_ok=True)
        meta_path = os.path.join(path, "session.json")
        existing = self._read_session_meta(path)
        provider_user_seed = existing.get(PROVIDER_USER_SEED_KEY)
        if not is_valid_provider_user_seed(provider_user_seed):
            provider_user_seed = new_provider_user_seed()
        payload = {
            **existing,
            "session_id": sid,
            "platform": identity.platform,
            "user_id": identity.user_id,
            "label": identity.label,
            PROVIDER_USER_SEED_KEY: provider_user_seed,
            "updated_at": datetime.now().isoformat(),
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            print(f"Error writing session metadata {meta_path}: {exc}")
        return sid

    def provider_user_id(self, session_id: str) -> str:
        sid = self.ensure_session(session_id)
        meta = self._read_session_meta(self.session_dir(sid))
        provider_user_seed = meta.get(PROVIDER_USER_SEED_KEY)
        if not is_valid_provider_user_seed(provider_user_seed):
            sid = self.ensure_session(sid)
            meta = self._read_session_meta(self.session_dir(sid))
            provider_user_seed = meta.get(PROVIDER_USER_SEED_KEY)
        return provider_user_id_for_seed(self.base_dir, str(provider_user_seed or ""))

    def list_file_sessions(self) -> list[dict]:
        sessions: list[dict] = []
        if not os.path.isdir(self.sessions_root):
            return sessions
        for name in sorted(os.listdir(self.sessions_root)):
            sid = normalize_session_id(name)
            path = self.session_dir(sid)
            if not os.path.isdir(path):
                continue
            identity = infer_identity(sid)
            meta = self._read_session_meta(path)
            sessions.append({
                "session_id": sid,
                "platform": meta.get("platform") or identity.platform,
                "user_id": meta.get("user_id") or identity.user_id,
                "label": meta.get("label") or identity.label,
                "has_files": True,
            })
        return sessions

    def delete_private_files(self, session_id: str) -> bool:
        sid = normalize_session_id(session_id)
        path = self.session_dir(sid)
        if not path.startswith(os.path.abspath(self.sessions_root) + os.sep):
            raise ValueError("refusing to delete session outside memory/sessions")
        if os.path.isdir(path):
            shutil.rmtree(path)
            return True
        return False

    def _read_session_meta(self, path: str) -> dict:
        meta_path = os.path.join(path, "session.json")
        if not os.path.exists(meta_path):
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
