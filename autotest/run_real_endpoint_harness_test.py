# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import configparser
import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRODUCT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_CASES_PATH = REPO_ROOT / "autotest" / "测试用例.md"
RAW_HISTORY_SEPARATOR = "\n------------------------------\n"
TERMINAL_RESULTS = {"sent", "dropped", "error", "stale_dropped"}

PROVIDER_DEFAULTS = {
    "minimax": {
        "base_url": "https://api.minimax.io/v1",
        "model": "MiniMax-M3",
        "thinking_enabled": True,
        "mimo_web_search_mode": "off",
    },
    "mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5",
        "thinking_enabled": True,
        "mimo_web_search_mode": "off",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "thinking_enabled": True,
        "mimo_web_search_mode": "off",
    },
}

MARKER_LEAK_RE = re.compile(r"(?:&{1,2}|\|{2})[A-Za-z_][A-Za-z0-9_]{1,32}:[^&|\r\n]{0,120}(?:&{1,2}|\|{2})")


@dataclass
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str
    thinking_enabled: bool = True
    temperature: float | None = None
    mimo_web_search_mode: str = "off"
    provider_key: str | None = None
    variant: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "provider_key": self.provider_key or self.name,
            "variant": self.variant,
            "base_url": self.base_url,
            "model": self.model,
            "thinking_enabled": self.thinking_enabled,
            "temperature": self.temperature,
            "mimo_web_search_mode": self.mimo_web_search_mode,
        }


class ProductClient:
    def __init__(self, base_url: str, session_id: str):
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {body}") from exc
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body

    def get_status(self) -> dict[str, Any]:
        return self.request("GET", self.with_session_query("/api/status"), timeout=30)

    def update_config(self, provider: ProviderConfig) -> dict[str, Any]:
        payload = {
            "api_key": provider.api_key,
            "base_url": provider.base_url,
            "model": provider.model,
            "thinking_enabled": provider.thinking_enabled,
            "mimo_web_search_mode": provider.mimo_web_search_mode,
        }
        if provider.temperature is not None:
            payload["temperature"] = provider.temperature
        return self.request(
            "POST",
            "/api/config",
            payload,
            timeout=30,
        )

    def clear_conversation(self) -> dict[str, Any]:
        return self.request("POST", self.with_session_query("/api/conversation/clear"), {}, timeout=30)

    def chat(self, text: str) -> dict[str, Any]:
        return self.request("POST", "/api/chat", {"text": text, "session_id": self.session_id}, timeout=30)

    def with_session_query(self, path: str) -> str:
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}{urllib.parse.urlencode({'session_id': self.session_id})}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real endpoint harness continuous test.")
    parser.add_argument("--providers", default="minimax", help="Comma separated provider names.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH), help="Markdown case file.")
    parser.add_argument("--api-key-file", default=str(REPO_ROOT / "api_key.txt"), help="Provider config file.")
    parser.add_argument("--base-url", default=DEFAULT_PRODUCT_BASE_URL, help="Local product API base URL.")
    parser.add_argument("--output-dir", default=None, help="Output directory under autotest/runs by default.")
    parser.add_argument("--start-server", action="store_true", help="Start uvicorn when the product API is not reachable.")
    parser.add_argument("--no-start-server", action="store_true", help="Never start uvicorn.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="8000")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--session-id", default=None, help="Isolated product session id for this run.")
    parser.add_argument("--keep-last-conversation", action="store_true", help="Do not clear after the final provider.")
    parser.add_argument(
        "--mimo-temperature-sweep",
        default="",
        help="Run Mimo repeatedly with thinking disabled and comma-separated temperatures, e.g. 0.3,1.5.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = normalize_autotest_session_id(args.session_id or f"autotest_{run_id}")
    output_dir = Path(args.output_dir) if args.output_dir else REPO_ROOT / "autotest" / "runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    provider_names = [name.strip().lower() for name in args.providers.split(",") if name.strip()]
    cases_path = Path(args.cases)
    cases = load_cases(cases_path)
    providers = load_provider_configs(provider_names, Path(args.api_key_file))
    if args.mimo_temperature_sweep.strip():
        providers = build_mimo_temperature_sweep(
            providers,
            args.mimo_temperature_sweep,
            Path(args.api_key_file),
        )
    client = ProductClient(args.base_url, session_id=session_id)

    server_proc = None
    if not is_api_reachable(client):
        if args.no_start_server or not args.start_server:
            raise RuntimeError(
                f"Product API is not reachable at {args.base_url}. "
                "Start the backend or rerun with --start-server."
            )
        server_proc = start_server(args.host, args.port, output_dir)
        client = ProductClient(f"http://{args.host}:{args.port}", session_id=session_id)
        wait_for_api(client, timeout=45)

    trace = {
        "run_id": run_id,
        "created_at": dt.datetime.now().isoformat(),
        "product_base_url": client.base_url,
        "session_id": session_id,
        "cases_path": str(cases_path),
        "case_count": len(cases),
        "providers": [],
    }

    try:
        for index, provider in enumerate(providers):
            provider_trace = run_provider(client, provider, cases, args.timeout_seconds)
            trace["providers"].append(provider_trace)
            write_json(output_dir / f"provider_{provider.name}_trace.json", provider_trace)
            if index < len(providers) - 1 or not args.keep_last_conversation:
                with contextlib.suppress(Exception):
                    client.clear_conversation()
        write_json(output_dir / "trace.json", trace)
        write_json(output_dir / "cases_snapshot.json", cases)
        write_summary(output_dir / "summary.md", trace)
        print(f"[autotest] wrote {output_dir}")
        return 0
    finally:
        if server_proc is not None:
            stop_server(server_proc)


def load_cases(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"<!--\s*AUTOTEST_CASES_JSON_START\s*-->(.*?)<!--\s*AUTOTEST_CASES_JSON_END\s*-->",
        text,
        flags=re.S,
    )
    if not match:
        raise ValueError(f"Cannot find AUTOTEST_CASES_JSON block in {path}")
    cases = json.loads(match.group(1).strip())
    if not isinstance(cases, list) or not cases:
        raise ValueError("Cases JSON must be a non-empty array.")
    for idx, case in enumerate(cases, start=1):
        if str(case.get("user", "")).strip() == "":
            raise ValueError(f"Case {idx} is missing user text.")
        case.setdefault("turn", idx)
        case.setdefault("expected", "")
    return cases


def load_provider_configs(names: list[str], path: Path) -> list[ProviderConfig]:
    file_values = read_provider_file(path)
    providers: list[ProviderConfig] = []
    for name in names:
        provider_key = provider_key_from_name(name)
        if provider_key not in PROVIDER_DEFAULTS:
            raise ValueError(f"Unknown provider: {name}")
        merged = dict(PROVIDER_DEFAULTS[provider_key])
        if len(names) == 1:
            merged.update(file_values.get("_default", {}))
        merged.update(file_values.get(provider_key, {}))
        merged.update(file_values.get(name, {}))
        env_prefix = provider_key.upper()
        for field in ("api_key", "base_url", "model", "thinking_enabled", "temperature", "mimo_web_search_mode"):
            env_names = [f"{env_prefix}_{field.upper()}"]
            if provider_key == "mimo" and field == "mimo_web_search_mode":
                env_names.insert(0, "MIMO_WEB_SEARCH_MODE")
            env_value = next((os.getenv(env_name) for env_name in env_names if os.getenv(env_name)), None)
            if env_value:
                merged[field] = env_value
        api_key = str(merged.get("api_key") or "").strip()
        if not api_key:
            raise ValueError(f"Missing API key for provider {provider_key}.")
        providers.append(
            ProviderConfig(
                name=name,
                api_key=api_key,
                base_url=str(merged.get("base_url") or PROVIDER_DEFAULTS[provider_key]["base_url"]).strip(),
                model=str(merged.get("model") or PROVIDER_DEFAULTS[provider_key]["model"]).strip(),
                thinking_enabled=parse_bool(merged.get("thinking_enabled", False)),
                temperature=parse_optional_float(merged.get("temperature")),
                mimo_web_search_mode=parse_mimo_web_search_mode(merged.get("mimo_web_search_mode")),
                provider_key=provider_key,
                variant=str(merged.get("variant") or "").strip() or None,
            )
        )
    return providers


def provider_key_from_name(name: str) -> str:
    lowered = (name or "").strip().lower()
    if lowered.startswith("minimax") or lowered.startswith("minimaxi"):
        return "minimax"
    if lowered.startswith("mimo"):
        return "mimo"
    if lowered.startswith("deepseek"):
        return "deepseek"
    return lowered


def build_mimo_temperature_sweep(
    providers: list[ProviderConfig],
    raw_temperatures: str,
    api_key_file: Path,
) -> list[ProviderConfig]:
    temperatures = parse_temperature_list(raw_temperatures)
    base = next((provider for provider in providers if (provider.provider_key or provider.name) == "mimo"), None)
    if base is None:
        base = load_provider_configs(["mimo"], api_key_file)[0]
    sweep: list[ProviderConfig] = []
    for temperature in temperatures:
        label = temperature_label(temperature)
        sweep.append(
            ProviderConfig(
                name=f"mimo_t{label}",
                api_key=base.api_key,
                base_url=base.base_url,
                model=base.model,
                thinking_enabled=False,
                temperature=temperature,
                mimo_web_search_mode=base.mimo_web_search_mode,
                provider_key="mimo",
                variant=f"thinking_off_temperature_{temperature:g}",
            )
        )
    return sweep


def parse_temperature_list(raw: str) -> list[float]:
    values: list[float] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        value = float(part)
        if value < 0:
            raise ValueError(f"Temperature must be non-negative: {value}")
        values.append(value)
    if not values:
        raise ValueError("--mimo-temperature-sweep requires at least one temperature.")
    return values


def temperature_label(value: float) -> str:
    return str(value).replace(".", "_").replace("-", "minus_")


def normalize_autotest_session_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
    normalized = normalized.strip("._:-")
    if not normalized:
        normalized = dt.datetime.now().strftime("autotest_%Y%m%d_%H%M%S")
    if not normalized.startswith("autotest_"):
        normalized = f"autotest_{normalized}"
    return normalized[:96]


def parse_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_provider_file(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return normalize_provider_json(json.loads(stripped))
        except json.JSONDecodeError:
            pass

    parser = configparser.ConfigParser()
    try:
        parser.read_string(stripped)
    except configparser.Error:
        parser = None
    if parser and parser.sections():
        result: dict[str, dict[str, Any]] = {}
        for section in parser.sections():
            key = section.strip().lower()
            result[key] = {k.strip().lower(): v.strip() for k, v in parser.items(section)}
        return result

    return parse_flat_provider_keys(stripped)


def normalize_provider_json(data: Any) -> dict[str, dict[str, Any]]:
    if isinstance(data, list):
        result = {}
        for item in data:
            if isinstance(item, dict) and item.get("provider"):
                result[str(item["provider"]).lower()] = dict(item)
        return result
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                result[str(key).lower()] = dict(value)
        return result
    return {}


def parse_flat_provider_keys(text: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    generic_block: dict[str, Any] = {}
    generic_blocks: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+)\s*=\s*(.*)$", line)
        if not match:
            continue
        key = match.group(1).strip().lower().replace(".", "_").replace("-", "_")
        value = strip_quotes(match.group(2).strip())
        provider_name = "_default"
        field = key
        for name in PROVIDER_DEFAULTS:
            if key.startswith(f"{name}_"):
                provider_name = name
                field = key[len(name) + 1 :]
                if provider_name == "mimo" and field == "web_search_mode":
                    field = "mimo_web_search_mode"
                break
            if key.endswith(f"_{name}"):
                provider_name = name
                field = key[: -(len(name) + 1)]
                if provider_name == "mimo" and field == "web_search_mode":
                    field = "mimo_web_search_mode"
                break
        if field in {"api_key", "base_url", "model", "thinking_enabled", "temperature", "mimo_web_search_mode"}:
            if provider_name == "_default":
                if field == "api_key" and generic_block:
                    generic_blocks.append(generic_block)
                    generic_block = {}
                generic_block[field] = value
            else:
                result.setdefault(provider_name, {})[field] = value
    if generic_block:
        generic_blocks.append(generic_block)
    if len(generic_blocks) == 1:
        result.setdefault("_default", {}).update(generic_blocks[0])
    else:
        for block in generic_blocks:
            base_url = str(block.get("base_url") or "").lower()
            if "xiaomimimo" in base_url:
                result.setdefault("mimo", {}).update(block)
            elif "deepseek" in base_url:
                result.setdefault("deepseek", {}).update(block)
    return result


def strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def parse_mimo_web_search_mode(value: Any) -> str:
    mode = str(value or "off").strip().lower()
    if mode in {"0", "false", "no", "disabled", "disable"}:
        return "off"
    if mode in {"1", "true", "yes", "enabled", "enable"}:
        return "adaptive"
    if mode not in {"off", "adaptive", "force"}:
        raise ValueError(f"Unsupported mimo_web_search_mode: {value}")
    return mode


def is_api_reachable(client: ProductClient) -> bool:
    try:
        client.get_status()
        return True
    except Exception:
        return False


def start_server(host: str, port: str, output_dir: Path) -> subprocess.Popen:
    log = open(output_dir / "server.log", "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )


def wait_for_api(client: ProductClient, timeout: int) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            client.get_status()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"Product API did not become ready: {last_error}")


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def run_provider(
    client: ProductClient,
    provider: ProviderConfig,
    cases: list[dict[str, Any]],
    timeout_seconds: int,
) -> dict[str, Any]:
    applied_config = client.update_config(provider)
    client.clear_conversation()
    baseline = client.get_status()
    assert_clean_baseline(baseline, provider.name)

    provider_trace = {
        **provider.public_dict(),
        "applied_config": applied_config,
        "baseline_status": baseline,
        "turns": [],
    }

    for case in cases:
        before_status = client.get_status()
        before_raw_history = split_raw_history(get_nested(before_status, "llm", "last_llm_raw"))
        before_max_raw_id = max_table_id("raw_chat_logs", session_id=client.session_id)

        client.chat(case["user"])
        after_status = wait_for_turn_done(
            client=client,
            before_raw_count=len(before_raw_history),
            before_max_raw_id=before_max_raw_id,
            session_id=client.session_id,
            timeout_seconds=timeout_seconds,
        )
        rows = read_raw_logs_since(before_max_raw_id, session_id=client.session_id)
        after_raw_history = split_raw_history(get_nested(after_status, "llm", "last_llm_raw"))
        raw_outputs = raw_history_delta(before_raw_history, after_raw_history)
        if not raw_outputs:
            raw_outputs = raw_outputs_from_rows(rows)

        turn = build_turn_trace(case, before_status, after_status, raw_outputs, rows)
        provider_trace["turns"].append(turn)

    provider_trace["summary"] = summarize_provider(provider_trace["turns"])
    return provider_trace


def assert_clean_baseline(status: dict[str, Any], provider: str) -> None:
    pending = get_nested(status, "gate", "pending_job_id")
    if status.get("status") != "COLD" or status.get("msg_index_today") not in (0, None) or pending is not None:
        raise RuntimeError(f"{provider} baseline is not isolated COLD state: {status}")


def wait_for_turn_done(
    client: ProductClient,
    before_raw_count: int,
    before_max_raw_id: int,
    session_id: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        status = client.get_status()
        last_status = status
        pending = get_nested(status, "gate", "pending_job_id")
        result = status.get("last_snapshot_result")
        raw_count = len(split_raw_history(get_nested(status, "llm", "last_llm_raw")))
        db_advanced = max_table_id("raw_chat_logs", session_id=session_id) > before_max_raw_id
        raw_advanced = raw_count > before_raw_count
        if pending is None and result in TERMINAL_RESULTS and (db_advanced or raw_advanced):
            return status
        time.sleep(0.5)
    raise TimeoutError(f"Turn timed out after {timeout_seconds}s. Last status: {last_status}")


def build_turn_trace(
    case: dict[str, Any],
    before_status: dict[str, Any],
    after_status: dict[str, Any],
    raw_outputs: list[str],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    initial_raw = raw_outputs[0] if raw_outputs else ""
    is_cold_context = before_status.get("status") == "COLD"
    initial_parse = parse_raw_output(initial_raw, is_cold_context=is_cold_context)
    secondary = [
        {
            "raw_output": raw,
            **parse_raw_output(raw, is_cold_context=is_cold_context),
        }
        for raw in raw_outputs[1:]
    ]
    final_outputs = final_visible_outputs(rows)
    final_action = after_status.get("last_action") or last_non_empty([row.get("action") for row in rows])
    final_parse_status = get_nested(after_status, "llm", "parse_status")
    cache = extract_cache(rows)
    cot = extract_cot(rows)
    output_safety = extract_output_safety(rows)
    quality = build_quality(raw_outputs, rows, final_outputs, output_safety)
    quality["deterministic_downgrade"] = is_deterministic_downgrade(final_parse_status, final_outputs)

    return {
        "turn": case.get("turn"),
        "user": case.get("user"),
        "expected": case.get("expected", ""),
        "before_status": compact_status(before_status),
        "after_status": compact_status(after_status),
        "raw_outputs": raw_outputs,
        "initial_raw_output": initial_raw,
        "initial_parse_status": initial_parse["parse_status"],
        "initial_parse_errors": initial_parse["parse_errors"],
        "initial_protocol_family": initial_parse["protocol_family"],
        "initial_action": initial_parse["action"],
        "initial_direct_ok": initial_parse["direct_ok"],
        "secondary_raw_outputs": secondary,
        "final_parse_status": final_parse_status,
        "final_action": final_action,
        "final_visible_outputs": final_outputs,
        "db_rows": rows,
        "cache": cache,
        "cot": cot,
        "output_safety": output_safety,
        "llm_call_debug": output_safety.get("llm_call_debug") or {},
        "quality": quality,
        "observability_source": {
            "initial_raw_output": "status_raw_history_delta" if initial_raw else "missing",
            "initial_parse_errors": "script_recomputed_current_lightweight_protocol" if initial_raw else "not_available",
            "secondary_raw_outputs": "status_raw_history_delta" if secondary else "not_applicable_or_missing",
            "final_decision": "product_status_and_db",
            "cache": "prompt_cache_debug" if cache["reported"] else "not_reported",
            "cot": "prompt_cache_debug" if cot["reported"] else "not_reported",
            "output_safety": "prompt_cache_debug" if output_safety.get("reported") else "not_reported",
        },
    }


def parse_raw_output(raw: str, is_cold_context: bool = False) -> dict[str, Any]:
    if not raw:
        return {
            "parse_status": "missing",
            "parse_errors": ["no raw output captured"],
            "protocol_family": "missing",
            "action": None,
            "direct_ok": False,
        }
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from app.core.protocol import parse_and_validate_main_output

        result = parse_and_validate_main_output(raw, is_cold_context=is_cold_context)
        return {
            "parse_status": result.status,
            "parse_errors": list(result.errors or []),
            "protocol_family": classify_protocol_family(raw, result.status, result.raw_data),
            "action": result.decision.action.value if result.decision else None,
            "direct_ok": result.ok,
        }
    except Exception as exc:
        return {
            "parse_status": "script_parse_error",
            "parse_errors": [str(exc)],
            "protocol_family": "script_error",
            "action": None,
            "direct_ok": False,
        }


def classify_protocol_family(raw: str, status: str, raw_data: Any) -> str:
    if status == "ok":
        return "lightweight_text"
    if status == "legacy_json_protocol_error":
        return "legacy_action_items_json"
    stripped = (raw or "").lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return "json_like_error"
    if isinstance(raw_data, dict) and raw_data.get("protocol") == "lightweight_text":
        return "lightweight_text_error"
    return "non_json_error"


def split_raw_history(raw_history: Any) -> list[str]:
    if not raw_history:
        return []
    return [part for part in str(raw_history).split(RAW_HISTORY_SEPARATOR)]


def raw_history_delta(before: list[str], after: list[str]) -> list[str]:
    if len(after) >= len(before) and after[: len(before)] == before:
        return after[len(before) :]
    return after


def raw_outputs_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    outputs: list[str] = []
    seen: set[str] = set()
    for row in rows:
        raw = row.get("llm_raw_output")
        if raw is None:
            continue
        key = str(raw)
        if key in seen:
            continue
        seen.add(key)
        outputs.append(key)
    return outputs


def read_raw_logs_since(last_id: int, session_id: str | None = None) -> list[dict[str, Any]]:
    db_path = sqlite_db_path()
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        if session_id:
            rows = conn.execute(
                "select * from raw_chat_logs where id > ? and session_id = ? order by id asc",
                (last_id, session_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "select * from raw_chat_logs where id > ? order by id asc",
                (last_id,),
            ).fetchall()
    return [sanitize_row(dict(row)) for row in rows]


def max_table_id(table: str, session_id: str | None = None) -> int:
    db_path = sqlite_db_path()
    if not db_path.exists():
        return 0
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        try:
            if session_id and table == "raw_chat_logs":
                row = conn.execute(
                    "select max(id) from raw_chat_logs where session_id = ?",
                    (session_id,),
                ).fetchone()
            else:
                row = conn.execute(f"select max(id) from {table}").fetchone()
        except sqlite3.Error:
            return 0
    return int(row[0] or 0)


def sqlite_db_path() -> Path:
    url = os.getenv("DATABASE_URL", "sqlite:///./data/companion_bot.db")
    if not url.startswith("sqlite:///"):
        return REPO_ROOT / "data" / "companion_bot.db"
    raw = url[len("sqlite:///") :]
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / raw.replace("./", "", 1)
    return path


def sanitize_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if "raw_payload" in result:
        result["raw_payload_json"] = parse_json_maybe(result.get("raw_payload"))
    if "parsed_payload" in result:
        result["parsed_payload_json"] = parse_json_maybe(result.get("parsed_payload"))
    return result


def parse_json_maybe(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def final_visible_outputs(rows: list[dict[str, Any]]) -> list[str]:
    sent_rows = [
        row
        for row in rows
        if row.get("event_type") == "llm_decision" and str(row.get("status") or "").startswith("sent")
    ]
    sent_rows.sort(key=lambda row: (row.get("send_index") is None, row.get("send_index") or 0, row.get("id") or 0))
    return [str(row.get("final_text") or "") for row in sent_rows if str(row.get("final_text") or "").strip()]


def extract_cache(rows: list[dict[str, Any]]) -> dict[str, Any]:
    payload = latest_prompt_debug_payload(rows)
    if not payload:
        return {"reported": False, "prompt_tokens": None, "cached_tokens": None, "hit_rate": None, "raw_payload": None}
    usage = payload.get("llm_usage") or {}
    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
    cached_tokens = payload.get("cached_tokens")
    if cached_tokens is None:
        cached_tokens = extract_cached_tokens_from_usage(usage)
    hit_rate = None
    if isinstance(prompt_tokens, int) and isinstance(cached_tokens, int) and prompt_tokens > 0:
        hit_rate = cached_tokens / prompt_tokens
    return {
        "reported": isinstance(prompt_tokens, int) and isinstance(cached_tokens, int),
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "hit_rate": hit_rate,
        "raw_payload": payload,
    }


def extract_cached_tokens_from_usage(usage: dict[str, Any]) -> int | None:
    details = usage.get("prompt_tokens_details") or usage.get("input_token_details") or {}
    cached = details.get("cached_tokens") or details.get("cache_read_input_tokens")
    return cached if isinstance(cached, int) else None


def extract_cot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    payload = latest_prompt_debug_payload(rows)
    if not payload:
        return {
            "reported": False,
            "reasoning_content": None,
            "assistant_content": None,
            "assistant_message": None,
            "reasoning_tokens": None,
        }
    assistant_message = payload.get("llm_assistant_message") or {}
    usage = payload.get("llm_usage") or {}
    details = usage.get("completion_tokens_details") or usage.get("output_token_details") or {}
    reasoning_content = payload.get("llm_reasoning_content")
    assistant_content = assistant_message.get("content") if isinstance(assistant_message, dict) else None
    reasoning_tokens = details.get("reasoning_tokens")
    reported = bool(reasoning_content) or isinstance(reasoning_tokens, int)
    return {
        "reported": reported,
        "reasoning_content": reasoning_content,
        "assistant_content": assistant_content,
        "assistant_message": assistant_message or None,
        "reasoning_tokens": reasoning_tokens if isinstance(reasoning_tokens, int) else None,
    }


def latest_prompt_debug_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    debug_rows = [row for row in rows if row.get("event_type") == "prompt_cache_debug"]
    if not debug_rows:
        return {}
    payload = debug_rows[-1].get("raw_payload_json") or {}
    return payload if isinstance(payload, dict) else {}


def extract_output_safety(rows: list[dict[str, Any]]) -> dict[str, Any]:
    payload = latest_prompt_debug_payload(rows)
    if not payload:
        return {
            "reported": False,
            "normalized_raw_source": None,
            "visible_safety_reason": None,
            "provider_finish_reason": None,
            "assistant_has_reasoning_content": False,
            "assistant_has_tool_calls": False,
            "llm_call_debug": {},
        }
    llm_call_debug = payload.get("llm_call_debug") or {}
    if not isinstance(llm_call_debug, dict):
        llm_call_debug = {}
    return {
        "reported": True,
        "normalized_raw_source": payload.get("normalized_raw_source") or payload.get("raw_output_source"),
        "visible_safety_reason": payload.get("visible_safety_reason"),
        "provider_finish_reason": payload.get("provider_finish_reason"),
        "assistant_has_reasoning_content": bool(payload.get("assistant_has_reasoning_content")),
        "assistant_has_tool_calls": bool(payload.get("assistant_has_tool_calls")),
        "llm_call_debug": llm_call_debug,
    }


def build_quality(
    raw_outputs: list[str],
    rows: list[dict[str, Any]],
    final_outputs: list[str],
    output_safety: dict[str, Any],
) -> dict[str, Any]:
    raw_joined = "\n".join(raw_outputs)
    visible_joined = "\n".join(final_outputs)
    item_types = {str(row.get("item_type") or "") for row in rows}
    parsed_payloads = [row.get("parsed_payload_json") for row in rows if row.get("parsed_payload_json")]
    marker_used = bool(MARKER_LEAK_RE.search(raw_joined))
    visible_meme = "meme" in item_types or any(text.startswith("meme:") for text in final_outputs)
    bubble_safety = visible_bubble_safety(visible_joined)
    emoji_counts, meme_counts = visible_reaction_counts(final_outputs)
    provider_finish_reason = str(output_safety.get("provider_finish_reason") or "").lower()
    marker_or_visible_meme_used = (
        visible_meme
        or marker_used
        or "search_meme" in raw_joined
        or any("search_meme" in json.dumps(payload, ensure_ascii=False) for payload in parsed_payloads)
    )
    return {
        "multi_bubble": len(final_outputs) > 1,
        "visible_meme": visible_meme,
        "marker_used": marker_used,
        "marker_or_visible_meme_used": marker_or_visible_meme_used,
        "meme_or_search_meme_used": marker_or_visible_meme_used,
        "protocol_leak_visible": visible_protocol_leak(visible_joined),
        "json_array_draft_visible": any(text.strip().startswith("[{") for text in final_outputs),
        "old_type_content_residual": '"type"' in raw_joined and '"content"' in raw_joined,
        "visible_question": "？" in visible_joined or "?" in visible_joined,
        "overlong_visible": visible_text_length(final_outputs) > 180,
        "duplicate_visible_segment": has_duplicate_visible_segment(final_outputs),
        "visible_safety_violation": not bubble_safety["ok"],
        "visible_safety_reason": bubble_safety["reason"],
        "raw_marked_unsafe": bool(output_safety.get("visible_safety_reason")),
        "assistant_tool_call_seen": bool(output_safety.get("assistant_has_tool_calls")),
        "assistant_reasoning_seen": bool(output_safety.get("assistant_has_reasoning_content")),
        "provider_finish_reason_risky": bool(provider_finish_reason and provider_finish_reason not in {"stop", "end_turn", "complete", "completed"}),
        "emoji_repeat_visible": any(count > 1 for count in emoji_counts.values()),
        "duplicate_meme_stem": any(count > 1 for count in meme_counts.values()),
        "meme_overuse_visible": sum(meme_counts.values()) > 1,
    }


def is_deterministic_downgrade(final_parse_status: Any, final_outputs: list[str]) -> bool:
    status = str(final_parse_status or "")
    if status in {"repair_failed", "context_repair_failed"}:
        return True
    if status.startswith("repair_failed") or status.endswith("_repair_failed"):
        return True
    visible_texts = [str(output or "").strip() for output in final_outputs if not str(output or "").startswith("meme:")]
    return visible_texts == ["嗯"]


def visible_protocol_leak(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("[{"):
        return True
    if bool(MARKER_LEAK_RE.search(stripped)) or any(
        marker in stripped for marker in ('"action"', '"items"', '"search_meme"', '"meme"', '"type"', '"content"')
    ):
        return True
    return not visible_bubble_safety(stripped)["ok"]


def visible_bubble_safety(text: str) -> dict[str, Any]:
    if not str(text or "").strip():
        return {"ok": True, "reason": None}
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from app.core.protocol import classify_visible_text

        result = classify_visible_text(str(text or ""), mode="bubble")
        return {"ok": bool(result.ok), "reason": result.reason}
    except Exception as exc:
        return {"ok": False, "reason": f"classifier_unavailable:{exc}"}


def visible_text_length(outputs: list[str]) -> int:
    joined = "".join(text for text in outputs if not str(text).startswith("meme:"))
    return len(re.sub(r"\s+", "", joined))


def has_duplicate_visible_segment(outputs: list[str]) -> bool:
    normalized: list[str] = []
    for output in outputs:
        text = re.sub(r"\s+", "", str(output or ""))
        if not text or text.startswith("meme:"):
            continue
        normalized.append(text)
    return len(normalized) != len(set(normalized))


def visible_reaction_counts(outputs: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    emoji_counts: dict[str, int] = {}
    meme_counts: dict[str, int] = {}
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from app.core.repetition_guard import reaction_signals_from_visible_text
    except Exception:
        reaction_signals_from_visible_text = None

    for output in outputs:
        text = str(output or "")
        stripped = text.strip()
        if stripped.startswith("meme:"):
            stem = stripped[len("meme:"):].strip()
            if stem:
                meme_counts[stem] = meme_counts.get(stem, 0) + 1
            continue
        if reaction_signals_from_visible_text:
            emojis, memes = reaction_signals_from_visible_text(text)
            for emoji in emojis:
                emoji_counts[emoji] = emoji_counts.get(emoji, 0) + 1
            for meme in memes:
                meme_counts[meme] = meme_counts.get(meme, 0) + 1
    return emoji_counts, meme_counts


def summarize_provider(turns: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "total_turns": len(turns),
        "initial_lightweight_ok": count(turns, lambda t: t.get("initial_direct_ok") is True),
        "initial_blocked": count(
            turns,
            lambda t: t.get("initial_parse_status") not in ("ok", "missing"),
        ),
        "initial_missing": count(turns, lambda t: t.get("initial_parse_status") == "missing"),
        "initial_legacy_json_blocked": count(
            turns,
            lambda t: t.get("initial_parse_status") == "legacy_json_protocol_error",
        ),
        "initial_json_like_error": count(
            turns,
            lambda t: t.get("initial_protocol_family") == "json_like_error",
        ),
        "initial_lightweight_error": count(
            turns,
            lambda t: t.get("initial_protocol_family") == "lightweight_text_error",
        ),
        "format_ok_final": count(turns, lambda t: t.get("final_parse_status") == "ok"),
        "harness_intercepted": count(
            turns,
            lambda t: t.get("initial_parse_status") not in ("ok", "missing"),
        ),
        "final_non_ok": count(turns, lambda t: t.get("final_parse_status") != "ok"),
        "natural_text_coerced": count(turns, lambda t: t.get("final_parse_status") == "natural_text_coerced"),
        "repair_ok": count(turns, lambda t: t.get("final_parse_status") == "repair_ok"),
        "legacy_json_repair_ok": count(turns, lambda t: t.get("final_parse_status") == "json_repair_ok"),
        "context_repair_ok": count(turns, lambda t: t.get("final_parse_status") == "context_repair_ok"),
        "relaxed_released": count(turns, lambda t: t.get("final_parse_status") == "natural_text_coerced"),
        "failed_or_error": count(
            turns,
            lambda t: str(t.get("final_parse_status") or "").startswith("repair_failed")
            or str(t.get("final_parse_status") or "").endswith("error"),
        ),
        "marker_or_visible_meme_used": count(turns, lambda t: t["quality"].get("marker_or_visible_meme_used")),
        "meme_or_search_meme_used": count(turns, lambda t: t["quality"].get("meme_or_search_meme_used")),
        "marker_used_turns": count(turns, lambda t: t["quality"].get("marker_used")),
        "multi_bubble_turns": count(turns, lambda t: t["quality"]["multi_bubble"]),
        "visible_meme_turns": count(turns, lambda t: t["quality"]["visible_meme"]),
        "protocol_leak_turns": count(turns, lambda t: t["quality"]["protocol_leak_visible"]),
        "json_array_draft_visible_turns": count(turns, lambda t: t["quality"]["json_array_draft_visible"]),
        "old_type_content_residual_turns": count(turns, lambda t: t["quality"]["old_type_content_residual"]),
        "visible_question_turns": count(turns, lambda t: t["quality"].get("visible_question")),
        "overlong_visible_turns": count(turns, lambda t: t["quality"].get("overlong_visible")),
        "duplicate_visible_segment_turns": count(turns, lambda t: t["quality"].get("duplicate_visible_segment")),
        "visible_safety_violation_turns": count(turns, lambda t: t["quality"].get("visible_safety_violation")),
        "raw_marked_unsafe_calls": count(turns, lambda t: t["quality"].get("raw_marked_unsafe")),
        "assistant_tool_call_seen_calls": count(turns, lambda t: t["quality"].get("assistant_tool_call_seen")),
        "provider_finish_reason_risky_calls": count(turns, lambda t: t["quality"].get("provider_finish_reason_risky")),
        "emoji_repeat_visible_turns": count(turns, lambda t: t["quality"].get("emoji_repeat_visible")),
        "duplicate_meme_stem_turns": count(turns, lambda t: t["quality"].get("duplicate_meme_stem")),
        "meme_overuse_visible_turns": count(turns, lambda t: t["quality"].get("meme_overuse_visible")),
        "deterministic_downgrade_turns": count(turns, lambda t: t["quality"].get("deterministic_downgrade")),
        "llm_retry_calls": count(
            turns,
            lambda t: safe_int((t.get("llm_call_debug") or {}).get("retry_attempts")) > 0,
        ),
        "cot_reported_calls": count(turns, lambda t: (t.get("cot") or {}).get("reported")),
    }
    prompt_sum = 0
    cached_sum = 0
    reported = 0
    for turn in turns:
        cache = turn.get("cache") or {}
        if cache.get("reported"):
            prompt_sum += int(cache.get("prompt_tokens") or 0)
            cached_sum += int(cache.get("cached_tokens") or 0)
            reported += 1
    summary["cache_reported_calls"] = reported
    summary["cache_hit_rate_weighted"] = cached_sum / prompt_sum if prompt_sum else None
    return summary


def count(items: list[dict[str, Any]], predicate) -> int:
    return sum(1 for item in items if predicate(item))


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def compact_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status.get("status"),
        "msg_index_today": status.get("msg_index_today"),
        "last_action": status.get("last_action"),
        "last_snapshot_result": status.get("last_snapshot_result"),
        "pending_job_id": get_nested(status, "gate", "pending_job_id"),
        "parse_status": get_nested(status, "llm", "parse_status"),
        "last_send": get_nested(status, "llm", "last_send"),
        "meme": status.get("meme"),
    }


def write_summary(path: Path, trace: dict[str, Any]) -> None:
    refresh_current_protocol_metrics(trace)
    lines = [
        "# Harness 真实端点自动测试报告",
        "",
        f"- run_id: `{trace['run_id']}`",
        f"- 生成时间: `{trace['created_at']}`",
        f"- 产品接口: `{trace['product_base_url']}`",
        f"- 测试 session: `{trace.get('session_id')}`",
        f"- 测试轮数: `{trace['case_count']}`",
        "",
        "## 指标说明",
        "",
        "- `首轮轻量通过`：模型第一次原始输出按当前轻量协议可直接执行，不需要 repair 或兜底。",
        "- `首轮被拦截`：模型第一次原始输出不能按当前轻量协议直接执行，harness 需要拦截、修复或走兜底。",
        "- `最终成功`：产品最终发出的 decision 通过执行层校验；这个指标只说明兜底链路成功，不代表模型首轮听话。",
        "- `旧JSON`：模型仍输出旧 `action/items` JSON，属于协议残留。",
        "- `可见泄漏`：用户最终可见内容里出现 JSON、协议词、表情占位符、provider 残片或内部工具痕迹。",
        "- `raw unsafe`：归一化层把 provider raw content 标记为不可直接进入 visible_text。",
        "- `finish异常/tool/retry`：来自当前 `prompt_cache_debug.llm_call_debug` 与归一化观测字段。",
        "",
        "## Provider 汇总",
        "",
        "| Provider | 模型 | thinking | temperature | 轮次 | 首轮轻量通过 | 首轮被拦截 | 旧JSON | 最终成功 | 最终非OK | repair | 失败 | 确定性降级 | 表情使用 | 多气泡 | 提问 | 长回复 | 重复片段 | 可见泄漏 | raw unsafe | finish异常 | tool | retry | emoji重复 | meme重复 | 缓存命中 | CoT |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for provider in trace["providers"]:
        summary = provider["summary"]
        cache = format_rate(summary.get("cache_hit_rate_weighted"))
        lines.append(
            "| {provider} | {model} | {thinking} | {temperature} | {total_turns} | "
            "{initial_lightweight_ok} | {initial_blocked} | {initial_legacy_json_blocked} | "
            "{format_ok_final} | {final_non_ok} | {repair_ok} | {failed_or_error} | "
            "{deterministic_downgrade_turns} | "
            "{marker_or_visible_meme_used} | {multi_bubble_turns} | {visible_question_turns} | "
            "{overlong_visible_turns} | {duplicate_visible_segment_turns} | {protocol_leak_turns} | "
            "{raw_marked_unsafe_calls} | {provider_finish_reason_risky_calls} | "
            "{assistant_tool_call_seen_calls} | {llm_retry_calls} | "
            "{emoji_repeat_visible_turns} | {duplicate_meme_stem_turns} | "
            "{cache} ({cache_reported_calls}) | {cot_reported_calls} |".format(
                provider=provider["provider"],
                model=provider["model"],
                thinking=str(provider.get("thinking_enabled")).lower(),
                temperature=format_temperature(provider),
                cache=cache,
                **summary,
            )
        )
    lines.extend(["", "## 语义质量辅助指标", ""])
    lines.extend(semantic_quality_notes(trace))
    lines.extend(["", "## 本轮 P0 发现", ""])
    lines.extend(current_p0_findings(trace))
    lines.extend(["", "## 逐轮明细", ""])
    for provider in trace["providers"]:
        lines.extend([
            f"### {provider['provider']} / {provider['model']} / thinking={str(provider.get('thinking_enabled')).lower()} / temperature={format_temperature(provider)}",
            "",
        ])
        lines.append("| 轮次 | 用户输入 | 首轮状态 | 最终状态 | action | 最终可见回复 | cache | 标记 |")
        lines.append("|---:|---|---|---|---|---|---|---|")
        for turn in provider["turns"]:
            cache = turn.get("cache") or {}
            cache_text = (
                f"{cache.get('cached_tokens')}/{cache.get('prompt_tokens')}"
                if cache.get("reported")
                else "未上报"
            )
            flags = []
            quality = turn["quality"]
            if quality["multi_bubble"]:
                flags.append("多气泡")
            if quality["visible_meme"]:
                flags.append("可见表情")
            if quality.get("marker_used"):
                flags.append("首轮占位符")
            if quality["protocol_leak_visible"]:
                flags.append("可见泄漏")
            if quality.get("visible_safety_violation"):
                flags.append(f"bubble安全:{quality.get('visible_safety_reason')}")
            if quality.get("raw_marked_unsafe"):
                reason = (turn.get("output_safety") or {}).get("visible_safety_reason")
                flags.append(f"raw unsafe:{reason}")
            if quality.get("provider_finish_reason_risky"):
                reason = (turn.get("output_safety") or {}).get("provider_finish_reason")
                flags.append(f"finish:{reason}")
            if quality.get("assistant_tool_call_seen"):
                flags.append("tool_calls")
            retry_attempts = safe_int((turn.get("llm_call_debug") or {}).get("retry_attempts"))
            if retry_attempts > 0:
                flags.append(f"retry:{retry_attempts}")
            if quality["old_type_content_residual"]:
                flags.append("旧type/content")
            if quality.get("visible_question"):
                flags.append("提问")
            if quality.get("overlong_visible"):
                flags.append("长回复")
            if quality.get("duplicate_visible_segment"):
                flags.append("重复片段")
            if quality.get("deterministic_downgrade"):
                flags.append("确定性降级")
            if quality.get("emoji_repeat_visible"):
                flags.append("emoji重复")
            if quality.get("duplicate_meme_stem"):
                flags.append("meme重复")
            if quality.get("meme_overuse_visible"):
                flags.append("meme过度")
            user = str(turn.get("user") or "")[:80].replace("\n", " ")
            visible = " / ".join(turn["final_visible_outputs"])[:260].replace("\n", " ")
            lines.append(
                f"| {turn['turn']} | {escape_md(user)} | {escape_md(chinese_status(turn['initial_parse_status']))} | "
                f"{escape_md(chinese_status(turn['final_parse_status']))} | {turn.get('final_action') or ''} | "
                f"{escape_md(visible)} | {cache_text} | {', '.join(flags)} |"
            )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_current_protocol_metrics(trace: dict[str, Any]) -> None:
    for provider in trace.get("providers", []):
        for turn in provider.get("turns", []):
            before_status = turn.get("before_status") or {}
            initial_parse = parse_raw_output(
                turn.get("initial_raw_output") or "",
                is_cold_context=before_status.get("status") == "COLD",
            )
            turn["initial_parse_status"] = initial_parse["parse_status"]
            turn["initial_parse_errors"] = initial_parse["parse_errors"]
            turn["initial_protocol_family"] = initial_parse["protocol_family"]
            turn["initial_action"] = initial_parse["action"]
            turn["initial_direct_ok"] = initial_parse["direct_ok"]
            for secondary in turn.get("secondary_raw_outputs") or []:
                if isinstance(secondary, dict):
                    secondary_parse = parse_raw_output(
                        secondary.get("raw_output") or "",
                        is_cold_context=before_status.get("status") == "COLD",
                    )
                    secondary.update(secondary_parse)
        provider["summary"] = summarize_provider(provider.get("turns", []))


def current_p0_findings(trace: dict[str, Any]) -> list[str]:
    json_array_count = sum(
        turn["quality"]["json_array_draft_visible"]
        for provider in trace["providers"]
        for turn in provider["turns"]
    )
    context_repair_count = sum(
        1
        for provider in trace["providers"]
        for turn in provider["turns"]
        if turn.get("final_parse_status") == "context_repair_ok"
    )
    protocol_leak_count = sum(
        1
        for provider in trace["providers"]
        for turn in provider["turns"]
        if turn["quality"]["protocol_leak_visible"]
    )
    raw_unsafe_count = sum(
        1
        for provider in trace["providers"]
        for turn in provider["turns"]
        if turn["quality"].get("raw_marked_unsafe")
    )
    visible_safety_count = sum(
        1
        for provider in trace["providers"]
        for turn in provider["turns"]
        if turn["quality"].get("visible_safety_violation")
    )
    finish_risky_count = sum(
        1
        for provider in trace["providers"]
        for turn in provider["turns"]
        if turn["quality"].get("provider_finish_reason_risky")
    )
    tool_call_count = sum(
        1
        for provider in trace["providers"]
        for turn in provider["turns"]
        if turn["quality"].get("assistant_tool_call_seen")
    )
    deterministic_downgrade_count = sum(
        1
        for provider in trace["providers"]
        for turn in provider["turns"]
        if turn["quality"].get("deterministic_downgrade")
    )
    missing_initial_count = sum(
        1
        for provider in trace["providers"]
        for turn in provider["turns"]
        if turn["observability_source"]["initial_raw_output"] == "missing"
    )
    findings = []
    if json_array_count:
        findings.append(
            f"- P0-A：{json_array_count} 轮把 JSON 数组草稿泄漏到了用户可见回复。最小修复：所有 `[` 开头的模型草稿都按 JSON-like 协议输出处理，不能当自然文本放行。"
        )
    if protocol_leak_count:
        findings.append(
            f"- P0-B：{protocol_leak_count} 轮用户可见内容泄漏了协议词、JSON、provider 残片或表情占位符。最小修复：最终可见输出前必须经过 `classify_visible_text(mode=\"bubble\")`。"
        )
    if visible_safety_count and not protocol_leak_count:
        findings.append(
            f"- P0-B：{visible_safety_count} 轮最终可见文本被 bubble 安全分类器判为异常。需要核对发送前 guard 是否被绕过。"
        )
    if raw_unsafe_count:
        findings.append(
            f"- P0-C：{raw_unsafe_count} 轮 provider raw content 被归一化层标记为 unsafe。若最终仍发送，说明 repair/WAIT/drop 链路有问题；若未发送，说明拦截生效。"
        )
    if finish_risky_count:
        findings.append(
            f"- P1：{finish_risky_count} 轮 provider_finish_reason 不是 stop/completed，需核对是否 length/error/tool_calls 等未完成回复被当作普通回复发送。"
        )
    if tool_call_count:
        findings.append(
            f"- P1：{tool_call_count} 轮 assistant message 带 tool_calls/function_call。当前主聊天不是 provider-native agent loop，这类字段必须保持隐藏且不得进入 visible_text。"
        )
    if deterministic_downgrade_count:
        findings.append(
            f"- P1：{deterministic_downgrade_count} 轮触发确定性降级。需要人工核对是合理兜底，还是模型首轮失控导致体验退化。"
        )
    if context_repair_count:
        findings.append(
            f"- P0 候选：{context_repair_count} 轮触发 contextual repair。需要人工核对是否造成产品可见兜底；如果造成，需要修 COLD/HOT prompt 或确定性改写。"
        )
    if missing_initial_count:
        findings.append(
            f"- 可观测性缺口：{missing_initial_count} 轮没有捕获首轮 raw output delta。"
        )
    if not findings:
        findings.append("- 自动检测未发现本轮 P0。仍需人工检查回复质量、显性表情包请求和人设漂移。")
    return findings


def semantic_quality_notes(trace: dict[str, Any]) -> list[str]:
    lines = [
        "- 这部分是自动化辅助判断，不能代替人工语义评审；低温 / 高温对比时重点看最终可见回复表。",
    ]
    for provider in trace.get("providers", []):
        summary = provider.get("summary") or {}
        lines.append(
            "- `{provider}`：多气泡 {multi}/{total}，表情使用 {meme}/{total}，提问 {questions}/{total}，长回复 {long}/{total}，重复片段 {dup}/{total}，emoji重复 {emoji_dup}/{total}，meme重复 {meme_dup}/{total}，确定性降级 {downgrade}/{total}。".format(
                provider=provider.get("provider"),
                multi=summary.get("multi_bubble_turns", 0),
                meme=summary.get("marker_or_visible_meme_used", 0),
                questions=summary.get("visible_question_turns", 0),
                long=summary.get("overlong_visible_turns", 0),
                dup=summary.get("duplicate_visible_segment_turns", 0),
                emoji_dup=summary.get("emoji_repeat_visible_turns", 0),
                meme_dup=summary.get("duplicate_meme_stem_turns", 0),
                downgrade=summary.get("deterministic_downgrade_turns", 0),
                total=summary.get("total_turns", 0),
            )
        )
    return lines


def format_temperature(provider: dict[str, Any]) -> str:
    applied = provider.get("applied_config") or {}
    if isinstance(applied, dict) and applied.get("temperature") is not None:
        return str(applied.get("temperature"))
    if provider.get("temperature") is not None:
        return str(provider.get("temperature"))
    return "默认"


def chinese_status(status: Any) -> str:
    mapping = {
        "ok": "通过",
        "missing": "未捕获",
        "strict_parse_error": "严格解析失败",
        "legacy_json_protocol_error": "旧JSON拦截",
        "internal_protocol_leak": "内部协议泄漏拦截",
        "protocol_error": "协议错误",
        "text_protocol_error": "轻量协议错误",
        "script_parse_error": "脚本解析错误",
        "natural_text_coerced": "自然文本放行",
        "repair_ok": "repair成功",
        "json_repair_ok": "旧JSON repair成功",
        "context_repair_ok": "context repair成功",
        "repair_failed": "repair失败",
    }
    return mapping.get(str(status), str(status or ""))


def format_rate(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.2%}"
    return "not_reported"


def escape_md(value: str) -> str:
    return value.replace("|", "\\|")


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def get_nested(data: dict[str, Any] | None, *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def last_non_empty(values: list[Any]) -> Any:
    for value in reversed(values):
        if value not in (None, ""):
            return value
    return None


if __name__ == "__main__":
    raise SystemExit(main())
