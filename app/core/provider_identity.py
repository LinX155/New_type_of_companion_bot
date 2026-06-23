import hashlib
import hmac
import json
import os
import re
import secrets
from typing import Optional


PROVIDER_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
PROVIDER_USER_ID_PREFIX = "u_"
PROVIDER_IDENTITY_SECRET_PATH = os.path.join("data", "provider_identity_secret.json")
_SECRET_BYTES = 32
_SEED_BYTES = 32


def new_provider_user_seed() -> str:
    return secrets.token_urlsafe(_SEED_BYTES)


def is_valid_provider_user_seed(value: object) -> bool:
    return isinstance(value, str) and 16 <= len(value.strip()) <= 256


def is_valid_provider_user_id(value: object) -> bool:
    return isinstance(value, str) and bool(PROVIDER_USER_ID_RE.fullmatch(value))


def provider_user_id_for_seed(base_dir: str, seed: str) -> str:
    if not is_valid_provider_user_seed(seed):
        raise ValueError("invalid provider user seed")
    secret = _load_or_create_installation_secret(base_dir)
    digest = hmac.new(
        secret.encode("ascii"),
        seed.strip().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    provider_user_id = f"{PROVIDER_USER_ID_PREFIX}{digest[:32]}"
    if not is_valid_provider_user_id(provider_user_id):
        raise ValueError("generated provider user id is invalid")
    return provider_user_id


def provider_user_id_hash(provider_user_id: Optional[str]) -> Optional[str]:
    if not provider_user_id:
        return None
    return hashlib.sha256(provider_user_id.encode("utf-8")).hexdigest()[:16]


def _load_or_create_installation_secret(base_dir: str) -> str:
    path = os.path.join(os.path.abspath(base_dir), PROVIDER_IDENTITY_SECRET_PATH)
    secret = _read_secret(path)
    if secret:
        return secret

    secret = secrets.token_hex(_SECRET_BYTES)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{secrets.token_hex(8)}.tmp"
    payload = {"version": 1, "secret": secret}
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return secret


def _read_secret(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    secret = data.get("secret")
    if not isinstance(secret, str):
        return None
    secret = secret.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", secret):
        return secret.lower()
    return None
