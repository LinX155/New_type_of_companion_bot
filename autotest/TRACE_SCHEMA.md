# Trace schema

The runner writes `trace.json` with this shape:

```json
{
  "run_id": "YYYYMMDD_HHMMSS",
  "created_at": "ISO timestamp",
  "product_base_url": "http://127.0.0.1:8000",
  "session_id": "autotest_YYYYMMDD_HHMMSS",
  "cases_path": "autotest/测试用例.md",
  "providers": [
    {
      "provider": "minimax",
      "model": "MiniMax-M3",
      "base_url": "https://api.minimax.io/v1",
      "thinking_enabled": true,
      "temperature": 1.0,
      "mimo_web_search_mode": "off",
      "variant": null,
      "applied_config": {"status": "ok", "temperature": 1.0, "mimo_web_search_mode": "off"},
      "summary": {},
      "turns": []
    }
  ]
}
```

Provider config intentionally omits API keys.

Each turn contains:

```json
{
  "turn": 1,
  "user": "...",
  "expected": "...",
  "before_status": {},
  "after_status": {},
  "raw_outputs": ["initial", "secondary"],
  "initial_raw_output": "...",
  "initial_parse_status": "ok | legacy_json_protocol_error | strict_parse_error | text_protocol_error | protocol_error | missing",
  "initial_parse_errors": [],
  "initial_protocol_family": "lightweight_text | legacy_action_items_json | json_like_error | lightweight_text_error | non_json_error | missing",
  "initial_action": "internal action inferred by the current lightweight main harness",
  "initial_direct_ok": true,
  "secondary_raw_outputs": [
    {
      "raw_output": "...",
      "parse_status": "...",
      "parse_errors": []
    }
  ],
  "final_parse_status": "ok | natural_text_coerced | repair_ok | context_repair_ok | repair_failed | ...",
  "final_action": "internal action such as REPLY | REACT | ENTER_CHAT | ...",
  "final_visible_outputs": ["..."],
  "db_rows": [],
  "cache": {
    "reported": true,
    "prompt_tokens": 9828,
    "cached_tokens": 9536,
    "hit_rate": 0.9702,
    "raw_payload": {}
  },
  "cot": {
    "reported": true,
    "reasoning_content": "...",
    "assistant_content": "...",
    "assistant_message": {},
    "reasoning_tokens": 128
  },
  "output_safety": {
    "reported": true,
    "normalized_raw_source": "content | reasoning_content_rescue | content_empty | unsafe_content",
    "visible_safety_reason": null,
    "provider_finish_reason": "stop",
    "assistant_has_reasoning_content": true,
    "assistant_has_tool_calls": false,
      "llm_call_debug": {
        "llm_runtime_id": "...",
        "client_scope": "chat_session",
        "provider_profile": "minimax | mimo | kimi | deepseek | openai_compatible",
        "temperature_sent": true,
        "provider_identity_field": "user_id | safety_identifier | none",
        "retry_attempts": 0,
        "retry_delays": [],
        "transient_error_class": null,
        "status_code": null,
        "prompt_cache_key_disabled_reason": null,
        "mimo_web_search_mode": "off | adaptive | force",
        "mimo_web_search_tool_sent": false,
        "mimo_web_search_force_search": null,
        "mimo_web_search_annotations_count": 0,
        "mimo_web_search_error_message": null,
        "mimo_web_search_usage": null
      }
  },
  "llm_call_debug": {},
  "quality": {
    "multi_bubble": true,
    "visible_meme": true,
    "marker_used": true,
    "marker_or_visible_meme_used": true,
    "meme_or_search_meme_used": true,
    "protocol_leak_visible": false,
    "json_array_draft_visible": false,
    "old_type_content_residual": false,
    "visible_question": false,
    "overlong_visible": false,
    "duplicate_visible_segment": false,
    "visible_safety_violation": false,
    "visible_safety_reason": null,
    "raw_marked_unsafe": false,
    "assistant_tool_call_seen": false,
    "assistant_reasoning_seen": true,
    "provider_finish_reason_risky": false,
    "emoji_repeat_visible": false,
    "duplicate_meme_stem": false,
    "meme_overuse_visible": false,
    "deterministic_downgrade": false
  },
  "observability_source": {
    "initial_raw_output": "status_raw_history_delta | missing",
    "initial_parse_errors": "script_recomputed | not_available",
    "final_decision": "product_status_and_db",
    "cache": "prompt_cache_debug | not_reported",
    "cot": "prompt_cache_debug | not_reported",
    "output_safety": "prompt_cache_debug | not_reported"
  }
}
```

Metrics are derived from these fields. `initial_parse_status` is recomputed with the current lightweight main harness (`WAIT`, `ENTER_CHAT: ...`, natural text, recommended `&&category:keywords&&`, compatibility `||category:keywords||`), not the removed JSON `action/items` protocol. Final visible text is rechecked with the same bubble safety classifier used by the product send path. If a provider does not return cached token metadata, mark the turn as `reported=false`; do not treat missing cache metadata as zero cached tokens.

CoT, `output_safety`, and `llm_call_debug` fields are for developer/debug reports only. They must not be sent to the user as chat output or appended to the main conversation history. If `assistant_content` is empty but `reasoning_content` contains parseable lightweight protocol output, report it explicitly as a provider/content-channel issue.
