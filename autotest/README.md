# Autotest SOP: real endpoint harness continuous test

This folder is for agents. It standardizes the continuous QQ-style product test against the real product API. The default provider is the current MiniMax M3 setup; Mimo and DeepSeek remain optional comparison providers.

Do not write API keys into reports, logs, git commits, or screenshots. Provider artifacts may contain raw model outputs and visible chat content, but never the key.

## Scope

Use this SOP when validating the main chat harness through the product path:

- `POST /api/config`
- `POST /api/conversation/clear`
- `POST /api/chat`
- `GET /api/status`
- SQLite `raw_chat_logs` and `conversation_events`

Do not include `/mem`, `/forget`, active-message jobs, nudge, image, or platform adapter tests in this SOP. Those are separate harness surfaces.

## Standard command

From repo root:

```powershell
python autotest/run_real_endpoint_harness_test.py --start-server
```

Useful variants:

```powershell
python autotest/run_real_endpoint_harness_test.py --providers minimax --cases autotest/测试用例.md
python autotest/run_real_endpoint_harness_test.py --providers mimo,deepseek --cases autotest/测试用例.md
python autotest/run_real_endpoint_harness_test.py --base-url http://127.0.0.1:8000 --no-start-server
python autotest/run_real_endpoint_harness_test.py --output-dir autotest/runs/manual_YYYYMMDD_HHMMSS
python autotest/run_real_endpoint_harness_test.py --session-id autotest_manual_YYYYMMDD_HHMMSS --no-start-server
python autotest/run_real_endpoint_harness_test.py --providers mimo --mimo-temperature-sweep 0.3,1.5 --start-server
```

Default output:

```text
autotest/runs/YYYYMMDD_HHMMSS/
  trace.json
  summary.md
  cases_snapshot.json
  provider_minimax_trace.json
  provider_mimo_trace.json
  provider_deepseek_trace.json
  server.log            # only when --start-server starts uvicorn
```

`autotest/runs/` is ignored by git. The existing root report does not need to move.

## Provider config

The runner reads keys from environment variables or `api_key.txt`.
When running multiple providers in one command, use provider-prefixed keys or INI sections. A generic `api_key=...` is accepted only when running a single provider.

Standard harness runs should use `thinking_enabled=true` unless the provider comparison intentionally disables it. MiniMax M3 defaults to `thinking_enabled=true` and `reasoning_split=true` through the product runtime. Standard MiMo runs default `mimo_web_search_mode=off` so endpoint regressions are not mixed with external search variance; only set `adaptive` or `force` for a dedicated web-search run. If a provider rejects thinking mode, stop or mark the run as `thinking_unsupported`; do not silently continue as the standard result.

Temperature comparison runs are a separate mode, not the standard harness regression run. For Mimo temperature comparison, run the same cases twice with `thinking_enabled=false`, once at low temperature `0.3` and once at high temperature `1.5`:

```powershell
python autotest/run_real_endpoint_harness_test.py --providers mimo --mimo-temperature-sweep 0.3,1.5 --start-server
```

This mode must compare semantic quality first: whether replies stay coherent, remember the story, avoid over-roleplay, avoid repetitive questions, avoid overlong/duplicated bubbles, and still use memes naturally.

Preferred environment variables:

```powershell
$env:MIMO_API_KEY="..."
$env:MINIMAX_API_KEY="..."
$env:MINIMAX_BASE_URL="https://api.minimax.io/v1"
$env:MINIMAX_MODEL="MiniMax-M3"
$env:MINIMAX_THINKING_ENABLED="true"
$env:MINIMAX_TEMPERATURE="1.0"
$env:MIMO_BASE_URL="https://api.xiaomimimo.com/v1"
$env:MIMO_MODEL="mimo-v2.5"
$env:MIMO_THINKING_ENABLED="true"
$env:MIMO_TEMPERATURE="1.0"
$env:MIMO_WEB_SEARCH_MODE="off"
$env:DEEPSEEK_API_KEY="..."
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com"
$env:DEEPSEEK_MODEL="deepseek-v4-flash"
$env:DEEPSEEK_THINKING_ENABLED="true"
$env:DEEPSEEK_TEMPERATURE="1.0"
```

Supported `api_key.txt` shapes:

```ini
[minimax]
api_key=...
base_url=https://api.minimax.io/v1
model=MiniMax-M3
thinking_enabled=true
temperature=1.0

[mimo]
api_key=...
base_url=https://api.xiaomimimo.com/v1
model=mimo-v2.5
thinking_enabled=true
temperature=1.0
mimo_web_search_mode=off

[deepseek]
api_key=...
base_url=https://api.deepseek.com
model=deepseek-v4-flash
thinking_enabled=true
temperature=1.0
```

or flat keys:

```text
minimax_api_key=...
minimax_base_url=https://api.minimax.io/v1
minimax_model=MiniMax-M3
minimax_thinking_enabled=true
minimax_temperature=1.0
mimo_api_key=...
mimo_base_url=https://api.xiaomimimo.com/v1
mimo_model=mimo-v2.5
mimo_thinking_enabled=true
mimo_temperature=1.0
mimo_web_search_mode=off
deepseek_api_key=...
deepseek_base_url=https://api.deepseek.com
deepseek_model=deepseek-v4-flash
deepseek_thinking_enabled=true
deepseek_temperature=1.0
```

## Isolation rules

For each provider:

1. Set `/api/config`.
2. Use an isolated product session. By default the runner creates `autotest_<run_id>` and passes it to `/api/status`, `/api/conversation/clear`, and `/api/chat`.
3. Call `/api/conversation/clear` for that isolated session.
4. Confirm `/api/status` is `COLD`, `msg_index_today=0`, no pending job.
5. Send all cases sequentially. Wait for each turn to reach terminal state before sending the next.
6. Export provider trace before switching provider.
7. Call `/api/conversation/clear` again before the next provider.

Provider outputs must never share one conversation history. Cache metrics are provider-local.

## Required metrics

Per provider summary must include:

- `total_turns`
- `initial_lightweight_ok`
- `initial_blocked`
- `initial_missing`
- `initial_legacy_json_blocked`
- `initial_json_like_error`
- `initial_lightweight_error`
- `format_ok_final`
- `final_non_ok`
- `harness_intercepted`，含义等同于首轮被拦截，不再按最终 `parse_status != ok` 计算
- `natural_text_coerced`
- `repair_ok`
- `legacy_json_repair_ok`
- `context_repair_ok`
- `relaxed_released`
- `failed_or_error`
- `marker_or_visible_meme_used`
- `multi_bubble_turns`
- `visible_meme_turns`
- `protocol_leak_turns`
- `old_type_content_residual_turns`
- `cache_hit_rate_weighted`
- `cache_reported_calls`
- `visible_question_turns`
- `overlong_visible_turns`
- `duplicate_visible_segment_turns`
- `visible_safety_violation_turns`
- `raw_marked_unsafe_calls`
- `assistant_tool_call_seen_calls`
- `provider_finish_reason_risky_calls`
- `llm_retry_calls`
- `emoji_repeat_visible_turns`
- `duplicate_meme_stem_turns`
- `meme_overuse_visible_turns`
- `deterministic_downgrade_turns`

Per turn detail must include:

- turn index
- user input
- provider/model/base_url/thinking_enabled
- raw output list, especially `initial_raw_output`
- local recomputed `initial_parse_status` and `initial_parse_errors` using the current lightweight main harness, not the removed JSON `action/items` protocol
- `initial_protocol_family`
- `initial_action`
- `initial_direct_ok`
- secondary raw outputs, if repair/context repair/meme selection was called
- final product `parse_status`
- final action
- final visible outputs
- DB rows added in this turn
- prompt tokens, cached tokens, and whether cache metadata was reported
- normalized raw source, visible safety reason, provider finish reason, tool-call flag, and LLM retry debug when reported
- whether the turn used the recommended `&&category:keywords&&` marker, the compatibility `||category:keywords||` marker, or sent a visible meme
- whether multiple visible bubbles were sent
- whether visible output leaked JSON/protocol text
- whether the turn asked a visible question
- whether the final visible output was overlong
- whether the final visible output repeated the same visible segment
- whether emoji / meme stem repetition or deterministic downgrade happened

## Output quality checklist

Evaluate model behavior, not only protocol validity:

- Can it send multiple bubbles in one turn when natural?
- Does it actively use valid meme intent marker or visible meme in QQ-like emotional contexts? The recommended marker remains `&&category:keywords&&`; `||category:keywords||` is counted only as provider drift compatibility.
- Is the reply vivid, cute, and low-service?
- Does it remember the continuous day-long story?
- Does it avoid inventing heavy persona facts not grounded in prompt or history?
- Does it avoid exposing JSON, action, items, protocol, repair, or internal rules?

## Harness analysis checklist

For each provider, explain:

- Why first outputs were intercepted.
- Which cases both providers fail or get intercepted on.
- Whether natural language was correctly counted as `natural_text_coerced` instead of repair.
- Whether JSON-like broken output entered repair instead of becoming visible text.
- Whether COLD/HOT state rules caused contextual repair.
- Whether product logs alone can prove initial raw output, parse errors, repair raw output, repair errors, relaxed reason, contextual errors, and final decision.

## Human-readable report requirements

`summary.md` must be human-readable. It is not enough to leave only the script-generated metrics table.
`summary.md` must be generated in Chinese by default. English-only summary output is a regression.

After each run, rewrite or extend `summary.md` into a product/debug report that a product manager can read without opening `trace.json`.

The report must include:

- A short executive conclusion: what worked, what broke, and whether the result is acceptable.
- Test scope: endpoints used, providers tested, thinking mode, isolation/clear-history behavior, and what was intentionally excluded.
- Thinking-mode evidence: state whether `thinking_enabled=true` was only configured locally or also confirmed by provider usage fields such as `completion_tokens_details.reasoning_tokens`. If exact request payloads are not logged, say so explicitly.
- CoT analysis when available:
  - report `cot_reported_calls`;
  - for empty visible/assistant content, check whether `reasoning_content` contains parseable lightweight protocol output;
  - for DeepSeek-style protocol failures, compare CoT intent with final `assistant.content`;
  - explain whether the failure is prompt understanding, provider content-channel mismatch, relaxed release, or product-side guard weakness.
- Metric definitions in plain language: 首轮轻量协议通过、首轮被拦截、旧 JSON 残留、最终成功、repair、兜底失败、marker/visible-meme usage、多气泡、协议泄漏、raw unsafe、finish异常、tool_calls、retry、cache hit rate。
- Provider 汇总表。
- Full per-turn reply tables for each provider. These tables are mandatory and must show every user turn and the final visible product reply. Use the table style:
  - `turn`
  - `user input`
  - `initial -> final parse status`
  - `action`
  - `final visible output`
  - `cached/prompt tokens`
  - `flags`
  Use `/` to separate multiple chat bubbles inside one reply.
- Model output quality analysis:
  - whether the model can send multiple chat bubbles naturally;
  - whether it actively uses real visible meme or valid meme marker;
- whether replies are vivid, cute, low-service, and QQ-like;
- whether it remembers the continuous story;
- whether it overuses fake tags, repeated expressions, forced questions, or roleplay.
- for temperature comparison, whether low temperature becomes too bland/repetitive and whether high temperature becomes too loose/verbose/over-roleplay.
- Harness analysis:
  - why first outputs were intercepted;
  - what was repaired, relaxed, normalized, or failed;
  - which failures are product-visible;
  - whether visible output leaked JSON, protocol text, fake meme tags, provider control tokens, quote tags, tool calls, or other internal/tool-like text.
- Cache analysis:
  - reported calls count;
  - weighted cache hit rate;
  - append-only check result;
  - whether missing cache metadata was treated as `not_reported`, never as zero.
- Concrete evidence for serious issues:
  - turn number;
  - user input;
  - raw output or captured absence of raw output;
  - final visible output;
  - usage/cache evidence when relevant.
- A run-scoped P0 section following the rules below.
- A minimal fix order: list the smallest safe fixes to try before the next autotest run.

Do not expose API keys or full secret-bearing config in the report.

If the script-generated `summary.md` is too machine-like, rewrite it manually from `trace.json` and provider traces before reporting the result to the user.

## Run-scoped P0 analysis rules

P0 findings must be re-derived from the current run. Do not carry over old P0 numbering or old conclusions unless the same issue is proven again by the current trace.

For each P0, include:

- Product impact in plain language.
- Evidence from this exact run.
- Why it is P0 instead of P1.
- The smallest practical fix.
- The next-run validation criterion.

Use fresh labels such as `P0-A`, `P0-B`, `P0-C` for each report unless the user explicitly asks to preserve historical numbering.

Do not mark a known historical issue as current P0 if it did not appear in this run. Put it under "not observed in this run" or omit it.

Examples of issues that can be P0 when proven by the current run:

- Empty model content causes product-visible fallback such as `嗯`.
- Natural-language relaxed release is so broad that the harness no longer controls structure or tools.
- Fake meme/tool tags, `&&category:keywords&&`, or compatibility `||category:keywords||` markers are visibly sent to the user instead of becoming real visible memes.
- Over-roleplay or persona-boundary violations are product-visible and repeated.
- Cache append-only breaks or provider cache metadata disappears unexpectedly.

## Completion criteria

A run is complete only when:

- The selected providers ran the full case set, or the trace records exactly where and why one provider stopped.
- Conversation history was isolated by provider and by the run-scoped `autotest_<run_id>` session.
- `trace.json`, provider traces, `cases_snapshot.json`, and `summary.md` exist under `autotest/runs/<run_id>/`.
- `summary.md` has been made human-readable according to this SOP.
- The final answer to the user summarizes metrics, current-run P0 findings, and minimum fixes.
