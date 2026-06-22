import hashlib
import json
import copy
import re
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from .event_gate import ProcessContext
from .decisions import ActionDecision, Action, SendItem, SendItemType
from .protocol import (
    ProtocolResult,
    VALID_MEME_CATEGORIES,
    build_repair_messages,
    parse_and_validate_main_output,
    parse_meme_selection_output,
)
from .state import ChatStatus
from ..active.messages import ACTIVE_SECTIONS, expire_stale_candidates, parse_active_candidates
from ..llm.client import LLMClient, LLMResponseEnvelope
from ..llm.prompts import (
    FINAL_ACTION_OUTPUT_REMINDER,
    build_stable_prompt_hash_source,
    build_system_prompt,
    build_runtime_context_message,
    build_hot_turn_system_reminder_message,
    build_meme_search_messages,
)
from ..memory.files import MemoryFileManager
from .media_jobs import MediaJob, MediaJobQueue
from ..memes.catalog import MemeCatalog
from ..memes.search import MemeSearch
from ..memes.renderer import MemeRenderer

PROMPT_VIEW_LIMITS = {
    "soul": 12000,
    "memory_core": 8000,
    "today_memory": 4000,
    "tomorrow_topics": 4000,
}

REASONING_RESCUE_BLOCKLIST = (
    "用户说",
    "系统提示",
    "系统要求",
    "我应该",
    "我们应该",
    "我需要",
    "我们需要",
    "需要输出",
    "输出json",
    "输出 json",
    "最终输出",
    "候选",
    "分析",
    "思考",
    "推理",
    "考虑：",
    "当前是",
    "根据",
    "the user",
    "the system",
    "i should",
    "i need",
    "we need",
    "let me",
    "final json",
    "output json",
    "candidate",
    "reasoning",
    "analysis",
)


def _looks_like_rescuable_reasoning_output(raw_output: str) -> bool:
    text = (raw_output or "").strip()
    if not text or len(text) > 260:
        return False
    if text.count("\n") > 2:
        return False

    compact_lower = re.sub(r"\s+", "", text.lower())
    spaced_lower = text.lower()
    for marker in REASONING_RESCUE_BLOCKLIST:
        marker_lower = marker.lower()
        if marker_lower in compact_lower or marker_lower in spaced_lower:
            return False

    return True


class GraphState(TypedDict, total=False):
    ctx: ProcessContext
    messages: list
    decision: ActionDecision


class CompanionGraph:
    def __init__(
        self,
        llm_client: LLMClient,
        memory_manager: MemoryFileManager,
        meme_catalog: MemeCatalog,
        media_job_queue: Optional[MediaJobQueue] = None,
    ):
        self.llm = llm_client
        self.memory = memory_manager
        self.meme_catalog = meme_catalog
        self.meme_search = MemeSearch(meme_catalog)
        self.meme_renderer = MemeRenderer(meme_catalog)
        self.media_job_queue = media_job_queue
        self._conversation_history: list = []
        self._prompt_transcript: list[dict] = []
        self._prompt_event_ids: set[str] = set()
        self._media_harness_event_ids: set[str] = set()
        self._prompt_transcript_identity: Optional[str] = None
        self._prefix_rebuild_reason: Optional[str] = None
        self._last_prompt_messages: list[dict] = []
        self._last_prompt_observability: dict = {}
        self._prompt_block_hashes: dict = {}
        self._last_runtime_block_hash: Optional[str] = None
        self._last_hot_turn_reminder_hash: Optional[str] = None
        self._workflow = self._build_workflow()
        # Debug / observability fields for the MVP status bar
        self._last_llm_raw_output: Optional[str] = None
        self._llm_raw_output_history: list[str] = []
        self._last_parsed_action: Optional[str] = None
        self._last_parsed_text: Optional[str | list[str]] = None
        self._last_parsed_items: Optional[list[dict]] = None
        self._last_parse_status: str = "ok"
        self._last_search_meme: Optional[str] = None
        self._last_render_status: Optional[str] = None
        self._last_media_debug: list[dict] = []

    def _record_llm_raw_output(self, raw_output: str):
        self._last_llm_raw_output = raw_output
        if raw_output is not None:
            self._llm_raw_output_history.append(raw_output)

    def _build_workflow(self):
        graph = StateGraph(GraphState)
        graph.add_node("receive_snapshot", self._receive_snapshot)
        graph.add_node("build_context", self._build_context)
        graph.add_node("call_llm_for_decision", self._call_llm_for_decision)
        graph.add_node("validate_decision", self._validate_decision)

        graph.set_entry_point("receive_snapshot")
        graph.add_edge("receive_snapshot", "build_context")
        graph.add_edge("build_context", "call_llm_for_decision")
        graph.add_edge("call_llm_for_decision", "validate_decision")
        graph.add_edge("validate_decision", END)
        return graph.compile()

    async def run(self, ctx: ProcessContext) -> ActionDecision:
        result = await self._workflow.ainvoke({"ctx": ctx})
        decision = result.get("decision") or ActionDecision(action=Action.WAIT, text=None)
        ctx.decision = decision
        self._record_parsed_decision(decision)

        return decision

    async def run_media_followup(self, ctx: ProcessContext, media_payload: dict) -> ActionDecision:
        self._ensure_provider_transcript_identity()
        if not self._prompt_transcript:
            self._initialize_prompt_transcript(ctx.snapshot)

        self._last_hot_turn_reminder_hash = None
        self._append_runtime_context(ctx)
        self._prompt_transcript.append({
            "role": "system",
            "content": json.dumps(
                self._build_media_followup_payload(media_payload),
                ensure_ascii=False,
            ),
        })
        self._append_hot_turn_system_reminder(ctx.snapshot)
        self._prompt_transcript.append({"role": "system", "content": FINAL_ACTION_OUTPUT_REMINDER})

        messages = self._copy_prompt_transcript()
        self._record_prompt_observability(messages)
        state: GraphState = {"ctx": ctx, "messages": messages}
        state = await self._call_llm_for_decision(state)
        state = await self._validate_decision(state)
        decision = state.get("decision") or ActionDecision(action=Action.WAIT, items=None)
        ctx.decision = decision
        self._record_parsed_decision(decision)
        return decision

    async def _receive_snapshot(self, state: GraphState) -> GraphState:
        return state

    async def _build_context(self, state: GraphState) -> GraphState:
        ctx = state["ctx"]
        snapshot = ctx.snapshot

        self._ensure_provider_transcript_identity()
        if not self._prompt_transcript:
            self._initialize_prompt_transcript(snapshot)

        self._last_hot_turn_reminder_hash = None
        self._append_runtime_context(ctx)
        self._append_snapshot_events(snapshot.events)
        self._append_media_pending_payloads_and_enqueue_jobs(ctx)
        self._append_hot_turn_system_reminder(snapshot)
        self._prompt_transcript.append({"role": "system", "content": FINAL_ACTION_OUTPUT_REMINDER})

        messages = [dict(item) for item in self._prompt_transcript]
        self._record_prompt_observability(messages)
        return {**state, "messages": messages}

    async def _call_llm_for_decision(self, state: GraphState) -> GraphState:
        raw_output = await self._call_llm_for_messages(
            messages=state["messages"],
            temperature=self._main_chat_temperature(),
            append_assistant_to_transcript=True,
        )
        decision, parse_status = await self._parse_decision_with_harness(
            ctx=state["ctx"],
            base_messages=state["messages"],
            raw_output=raw_output,
            repair=True,
        )
        self._last_parse_status = parse_status
        decision = self._apply_protocol_guards(state["ctx"], decision)
        self._record_parsed_decision(decision)
        state["ctx"].decision = decision
        return {**state, "decision": decision, "messages": self._copy_prompt_transcript()}

    async def _parse_decision_with_harness(
        self,
        ctx: ProcessContext,
        base_messages: list,
        raw_output: str,
        repair: bool,
    ) -> tuple[ActionDecision, str]:
        result = parse_and_validate_main_output(
            raw_output,
            is_cold_context=self._is_cold_context(ctx),
        )
        if result.ok and result.decision:
            return result.decision, result.status

        should_repair = self._should_attempt_json_repair(raw_output, result)
        if not should_repair:
            relaxed = self._relaxed_visible_decision(raw_output, ctx)
            if relaxed:
                return relaxed, "natural_text_coerced"

        if repair:
            if should_repair:
                repaired = await self._repair_protocol_decision(
                    ctx=ctx,
                    base_messages=base_messages,
                    raw_output=raw_output,
                    result=result,
                )
                if repaired.ok and repaired.decision:
                    if repaired.status == "natural_text_coerced":
                        return repaired.decision, repaired.status
                    return repaired.decision, "repair_ok"
            return self._fallback_decision(ctx), "repair_failed"

        relaxed = self._relaxed_visible_decision(raw_output, ctx)
        if relaxed:
            return relaxed, "natural_text_coerced"
        return self._fallback_decision(ctx), result.status

    async def _repair_protocol_decision(
        self,
        ctx: ProcessContext,
        base_messages: list,
        raw_output: str,
        result: ProtocolResult,
    ) -> ProtocolResult:
        if self._prompt_transcript:
            repair_tail = build_repair_messages(
                base_messages=[],
                errors=result.errors,
                original_raw=raw_output,
                original_decision=result.decision,
            )
            messages = self._append_transcript_messages_for_call(repair_tail)
            append_assistant = True
        else:
            messages = build_repair_messages(
                base_messages=base_messages,
                errors=result.errors,
                original_raw=raw_output,
                original_decision=result.decision,
            )
            append_assistant = False
        try:
            repaired_raw = await self._call_llm_for_messages(
                messages=messages,
                temperature=self._main_chat_temperature(),
                append_assistant_to_transcript=append_assistant,
            )
        except Exception as exc:
            return ProtocolResult(status="repair_failed", errors=[str(exc)])

        repaired = parse_and_validate_main_output(
            repaired_raw,
            is_cold_context=self._is_cold_context(ctx),
        )
        if repaired.ok:
            return repaired
        relaxed = self._relaxed_visible_decision(repaired_raw, ctx)
        if relaxed:
            return ProtocolResult(status="natural_text_coerced", decision=relaxed)
        return ProtocolResult(
            status="repair_failed",
            decision=repaired.decision,
            errors=repaired.errors,
            raw_data=repaired.raw_data,
        )

    def _should_attempt_json_repair(self, raw_output: str, result: ProtocolResult) -> bool:
        if result.status == "protocol_error":
            return True
        return self._looks_like_protocol_output(raw_output)

    def _relaxed_visible_decision(self, raw_output: str, ctx: ProcessContext) -> Optional[ActionDecision]:
        items = self._visible_items_from_relaxed_output(raw_output)
        if not items:
            return None

        has_rich_item = any(item.type != SendItemType.TEXT for item in items)
        has_text_item = any(item.type == SendItemType.TEXT for item in items)
        if self._is_cold_context(ctx):
            action = Action.ENTER_CHAT if has_text_item else Action.REACT
        else:
            action = Action.REACT if has_rich_item else Action.REPLY
        return ActionDecision(action=action, items=items)

    def _visible_items_from_relaxed_output(self, raw_output: str) -> list[SendItem]:
        text = (raw_output or "").strip()
        if not text or self._looks_like_protocol_output(text):
            return []

        items: list[SendItem] = []
        for raw_line in text.replace("```", "").splitlines():
            line = raw_line.strip()
            if not line:
                continue

            meme_stem = self._extract_relaxed_meme_stem(line)
            if meme_stem:
                if self.meme_renderer.render_meme(meme_stem):
                    items.append(SendItem(type=SendItemType.MEME, content=f"meme:{meme_stem}"))
                else:
                    items.append(self._search_meme_item_for_missing_stem(meme_stem))
                continue

            if self._contains_internal_protocol(line):
                continue
            items.append(SendItem(type=SendItemType.TEXT, content=line))

        return items

    def _search_meme_item_for_missing_stem(self, stem: str) -> SendItem:
        tokens = [token for token in re.split(r"[_\-\s]+", (stem or "").strip().lower()) if token]
        category = tokens[0] if tokens and tokens[0] in VALID_MEME_CATEGORIES else "miscellaneous"
        keyword_tokens = tokens[1:] if category != "miscellaneous" else tokens
        keywords = " ".join(keyword_tokens).strip()
        if not keywords:
            keywords = "comfort" if category == "affection" else (category or "comfort")
        return SendItem(type=SendItemType.SEARCH_MEME, content=f"{category}:{keywords}")

    def _looks_like_protocol_output(self, text: str) -> bool:
        stripped = text.strip()
        if stripped.startswith("{"):
            return True
        if stripped.startswith("["):
            try:
                data, _ = json.JSONDecoder().raw_decode(stripped)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, list):
                return True
        protocol_markers = (
            '"action"',
            '"items"',
            "ActionDecision",
            "runtime_context",
            "FINAL_ACTION_OUTPUT_REMINDER",
            "cached_tokens",
            "prefix_rebuild",
            "search_meme:",
        )
        return any(marker in stripped for marker in protocol_markers)

    def _contains_internal_protocol(self, line: str) -> bool:
        markers = (
            "ActionDecision",
            "runtime_context",
            "FINAL_ACTION_OUTPUT_REMINDER",
            "cached_tokens",
            "prefix_rebuild",
            "search_meme:",
            '"action"',
            '"items"',
        )
        if any(marker in line for marker in markers):
            return True
        return "meme:" in line and not self._extract_relaxed_meme_stem(line)

    def _extract_relaxed_meme_stem(self, line: str) -> Optional[str]:
        match = re.fullmatch(
            r"(?:\[)?\s*(?:(?:表情|meme)\s*[:：]\s*)?meme:([A-Za-z0-9_\-]+)\s*(?:\])?",
            line,
        )
        if match:
            return match.group(1)
        return None

    def _fallback_decision(self, ctx: ProcessContext) -> ActionDecision:
        if self._is_cold_context(ctx):
            return ActionDecision(action=Action.WAIT, items=None)
        if ctx.snapshot.events:
            return ActionDecision(
                action=Action.LIGHT_ACK,
                items=[SendItem(type=SendItemType.TEXT, content="嗯")],
            )
        return ActionDecision(action=Action.WAIT, items=None)

    async def resolve_search_meme_item(
        self,
        ctx: ProcessContext,
        item: SendItem,
    ) -> Optional[SendItem]:
        """Resolve one internal search_meme marker into a concrete meme item.

        This is called by the send loop at the marker position, after any
        prefix text has already been sent. The second LLM call only selects a
        file_stem; it must not rewrite or add chat text.
        """
        if item.type != SendItemType.SEARCH_MEME:
            return None

        request_text = item.harness_value()
        parsed = self.meme_renderer.parse_react_text(item.content)
        category = parsed.get("category", "")
        keywords = parsed.get("keywords", "")
        candidates = self.meme_search.search(category, keywords, top_k=5)
        search_results = [
            {
                "request": request_text,
                "category": category,
                "keywords": keywords,
                "candidates": candidates,
            }
        ]
        ctx.meme_candidates = search_results
        self._last_search_meme = request_text

        if not candidates:
            self._last_render_status = "fallback"
            return None

        messages = self._append_transcript_messages_for_call(
            build_meme_search_messages(base_messages=[], search_results=search_results)
        )
        raw_output = await self._call_llm_for_messages(
            messages=messages,
            temperature=self._main_chat_temperature(),
            append_assistant_to_transcript=True,
        )
        selected = parse_meme_selection_output(raw_output, set(candidates))
        if not selected:
            selected = self._first_renderable_candidate(candidates)
            self._last_render_status = "fallback" if selected else "miss"
        else:
            selected = selected if self.meme_renderer.render_meme(selected) else None
            self._last_render_status = "hit" if selected else "miss"

        if not selected:
            return None

        ctx.selected_memes = [selected]
        ctx.selected_meme = selected
        return SendItem(type=SendItemType.MEME, content=selected)

    def _first_renderable_candidate(self, candidates: list[str]) -> Optional[str]:
        for stem in candidates:
            if self.meme_renderer.render_meme(stem):
                return stem
        return None

    async def _validate_decision(self, state: GraphState) -> GraphState:
        decision = state["decision"]
        ctx = state["ctx"]
        decision, context_status = await self._ensure_contextual_decision(
            state=state,
            decision=decision,
            raw_output=self._last_llm_raw_output or json.dumps(decision.to_harness_payload(), ensure_ascii=False),
        )
        if context_status:
            self._last_parse_status = context_status
            state = {**state, "decision": decision, "messages": self._copy_prompt_transcript()}

        ctx.selected_memes = []
        ctx.selected_meme = None
        validated_items: list[SendItem] = []
        selected_memes: list[str] = []
        missed_meme = False
        missing_meme_stems: list[str] = []

        for item in decision.all_items():
            if item.type == SendItemType.SEARCH_MEME:
                validated_items.append(item)
                continue

            if item.type == SendItemType.MEME:
                file_stem = item.harness_value()
                path = self.meme_renderer.render_meme(file_stem)
                if not path:
                    missed_meme = True
                    if file_stem not in missing_meme_stems:
                        missing_meme_stems.append(file_stem)
                    continue
                selected_memes.append(file_stem)

            validated_items.append(item)

        if missing_meme_stems and not ctx.missing_meme_repair_used:
            ctx.missing_meme_repair_used = True
            repaired = await self._repair_missing_meme_decision(state, decision, missing_meme_stems)
            if repaired:
                return await self._validate_decision({**state, "decision": repaired, "messages": self._copy_prompt_transcript()})

        if selected_memes:
            self._last_render_status = "hit"
            ctx.selected_memes = selected_memes
            ctx.selected_meme = selected_memes[0]
        elif missed_meme:
            self._last_render_status = "miss"
            ctx.selected_memes = []
            ctx.selected_meme = None

        if validated_items != decision.all_items():
            if validated_items:
                decision = decision.with_items(validated_items)
            elif decision.action in (Action.WAIT, Action.END_CHAT, Action.ENTER_CHAT):
                decision = ActionDecision(action=decision.action, items=None)
            else:
                decision = ActionDecision(action=Action.LIGHT_ACK, items=[SendItem(type=SendItemType.TEXT, content="嗯")])

        ctx.decision = decision
        return {**state, "decision": decision, "messages": self._copy_prompt_transcript()}

    async def _repair_missing_meme_decision(
        self,
        state: GraphState,
        decision: ActionDecision,
        missing_meme_stems: list[str],
    ) -> Optional[ActionDecision]:
        errors = [f"meme {stem} 在本地表情库中不存在。" for stem in missing_meme_stems]
        if self._prompt_transcript:
            repair_tail = build_repair_messages(
                base_messages=[],
                errors=errors,
                original_raw=self._last_llm_raw_output or "",
                original_decision=decision,
            )
            messages = self._append_transcript_messages_for_call(repair_tail)
            append_assistant = True
        else:
            messages = build_repair_messages(
                base_messages=state["messages"],
                errors=errors,
                original_raw=self._last_llm_raw_output or "",
                original_decision=decision,
            )
            append_assistant = False
        try:
            raw_output = await self._call_llm_for_messages(
                messages=messages,
                temperature=self._main_chat_temperature(),
                append_assistant_to_transcript=append_assistant,
            )
            result = parse_and_validate_main_output(
                raw_output,
                is_cold_context=self._is_cold_context(state["ctx"]),
            )
            if result.ok and result.decision:
                self._last_parse_status = "repair_ok"
                return result.decision
            self._last_parse_status = "repair_failed"
            return None
        except Exception:
            self._last_parse_status = "repair_failed"
            return None

    def commit_turn(self, ctx: ProcessContext, decision: ActionDecision, visible: bool):
        if visible:
            self.commit_sent_items(ctx, decision.send_items())

    def commit_sent_bubbles(self, ctx: ProcessContext, bubbles: list[str]):
        self.commit_sent_items(ctx, [SendItem(type=SendItemType.TEXT, content=bubble) for bubble in bubbles])

    def commit_sent_items(self, ctx: ProcessContext, items: list[SendItem]):
        if not items:
            return

        for evt in ctx.snapshot.events:
            text = self._event_get(evt, "text")
            if text:
                self._conversation_history.append({"role": "user", "text": text})

        for item in items:
            text = self._history_text_for_item(item)
            self._conversation_history.append({"role": "assistant", "text": text})

    def commit_assistant_items(self, items: list[SendItem]):
        if not items:
            return

        for item in items:
            text = self._history_text_for_item(item)
            self._conversation_history.append({"role": "assistant", "text": text})

    def commit_external_assistant_text(self, text: str):
        if not text:
            return
        self._conversation_history.append({"role": "assistant", "text": text})
        if self._prompt_transcript:
            self._append_assistant_text_to_prompt(text)

    def append_internal_system_reminder(self, message: dict):
        if not self._prompt_transcript:
            return
        if message.get("role") != "system":
            return
        content = str(message.get("content") or "").strip()
        if not content:
            return
        self._prompt_transcript.append({"role": "system", "content": content})

    def clear_prompt_state(self):
        self._conversation_history.clear()
        self.reset_provider_transcript(reason="conversation_cleared")

    def reset_provider_transcript(self, reason: str = "provider_transcript_reset"):
        self._prompt_transcript.clear()
        self._prompt_event_ids.clear()
        self._media_harness_event_ids.clear()
        self._last_prompt_messages.clear()
        self._last_prompt_observability.clear()
        self._prompt_block_hashes.clear()
        self._last_media_debug.clear()
        self._last_runtime_block_hash = None
        self._prompt_transcript_identity = self._current_llm_identity()
        self._prefix_rebuild_reason = reason

    def _history_text_for_item(self, item: SendItem) -> str:
        if item.type == SendItemType.TEXT:
            return item.content
        if item.type == SendItemType.EMOJI:
            return item.content[6:]
        return f"[表情: meme:{item.harness_value()}]"

    def _record_parsed_decision(self, parsed: ActionDecision):
        self._last_parsed_action = parsed.action.value
        self._last_parsed_text = parsed.text
        self._last_parsed_items = [item.to_harness_item() for item in parsed.all_items()]

    def _apply_protocol_guards(self, ctx: ProcessContext, decision: ActionDecision) -> ActionDecision:
        pending_text = "\n".join(
            str(self._event_get(evt, "text") or "")
            for evt in ctx.snapshot.events
            if self._event_get(evt, "text")
        )
        if self._requires_meme_response(pending_text):
            has_react_item = any(
                item.type in (SendItemType.SEARCH_MEME, SendItemType.MEME)
                for item in decision.all_items()
            )
            if not has_react_item:
                return ActionDecision(
                    action=Action.REACT,
                    items=[SendItem(type=SendItemType.SEARCH_MEME, content="amused:funny")],
                )
        return decision

    async def _ensure_contextual_decision(
        self,
        state: GraphState,
        decision: ActionDecision,
        raw_output: str,
    ) -> tuple[ActionDecision, Optional[str]]:
        errors = self._validate_contextual_decision(state["ctx"], decision)
        if not errors:
            return decision, None

        ctx = state["ctx"]
        if ctx.contextual_repair_used:
            return self._fallback_decision(ctx), "context_repair_failed"

        ctx.contextual_repair_used = True
        repair_result = ProtocolResult(
            status="context_protocol_error",
            decision=decision,
            errors=errors,
        )
        repaired = await self._repair_protocol_decision(
            ctx=ctx,
            base_messages=state["messages"],
            raw_output=raw_output,
            result=repair_result,
        )
        if repaired.ok and repaired.decision:
            fixed = self._apply_protocol_guards(ctx, repaired.decision)
            if not self._validate_contextual_decision(ctx, fixed):
                return fixed, "context_repair_ok"

        return self._fallback_decision(ctx), "context_repair_failed"

    def _validate_contextual_decision(self, ctx: ProcessContext, decision: ActionDecision) -> list[str]:
        if ctx.internal_source == "media_followup":
            return []

        if not self._is_cold_context(ctx):
            return []

        items = decision.all_items()
        if decision.action in (Action.WAIT, Action.END_CHAT, Action.ENTER_CHAT, Action.LIGHT_ACK):
            return []

        if decision.action == Action.REACT:
            if items and all(item.type != SendItemType.TEXT for item in items):
                return []
            return [
                "当前聊天状态是 COLD。包含 text item 的 REACT 属于文字+表情混合回复；"
                "先判断这条用户输入是否值得进入 HOT；"
                "如果用户明确开启对话、求陪、提问、倾诉或继续追问，需要发送文字+表情并进入热聊，请改用 ENTER_CHAT: 自然语言；"
                "如果只是低负担回应，请只保留表情占位或短句；如果不回应，请输出 WAIT。"
            ]

        if decision.action == Action.REPLY:
            return [
                "当前聊天状态是 COLD。普通文本展开回复不能使用 REPLY；"
                "先判断这条用户输入是否值得进入 HOT；"
                "如果用户明确开启对话、求陪、提问、倾诉或继续追问，需要展开接话并进入热聊，请改用 ENTER_CHAT: 自然语言；"
                "如果只是轻轻接一下，请使用很短自然句；如果不回应，请输出 WAIT。"
            ]

        return []

    def _is_cold_context(self, ctx: ProcessContext) -> bool:
        return ctx.snapshot.status == ChatStatus.COLD and not ctx.snapshot.nudge_triggered

    def _requires_meme_response(self, text: str) -> bool:
        lowered = text.lower()
        meme_signals = ["表情包", "斗图", "meme"]
        if any(signal in lowered for signal in meme_signals):
            return True
        if "表情" in lowered and any(verb in lowered for verb in ["看", "发", "来", "给", "要", "看看"]):
            return True
        return False

    def get_history(self) -> list:
        return list(self._conversation_history)

    def get_prompt_observability(self) -> dict:
        return dict(self._last_prompt_observability)

    # Debug / observability getters for the MVP status bar
    def get_last_llm_raw_output(self) -> Optional[str]:
        return self._last_llm_raw_output

    def get_llm_raw_output_history(self) -> Optional[str]:
        if not self._llm_raw_output_history:
            return None
        return "\n------------------------------\n".join(self._llm_raw_output_history)

    def get_last_parsed_decision(self) -> dict:
        return {
            "action": self._last_parsed_action,
            "text": self._last_parsed_text,
            "items": self._last_parsed_items,
            "parse_status": self._last_parse_status,
        }

    def get_last_meme_search(self) -> Optional[str]:
        return self._last_search_meme

    def get_last_render_status(self) -> Optional[str]:
        return self._last_render_status

    def get_last_media_debug(self) -> list[dict]:
        return copy.deepcopy(self._last_media_debug)

    def get_last_llm_observability(self) -> dict:
        return {
            "last_llm_raw": self.get_llm_raw_output_history(),
            "last_parsed_action": self._last_parsed_action,
            "last_parsed_text": self._last_parsed_text,
            "last_parsed_items": self._last_parsed_items,
            "parse_status": self._last_parse_status,
            "last_search_meme": self._last_search_meme,
            "last_render_status": self._last_render_status,
            "last_media_debug": self.get_last_media_debug(),
            "prompt_cache": self.get_prompt_observability(),
        }

    async def _call_llm_for_messages(
        self,
        messages: list[dict],
        temperature: float,
        append_assistant_to_transcript: bool,
    ) -> str:
        envelope = await self._request_llm_envelope(messages, temperature)
        raw_output, raw_source = self._raw_output_from_envelope(envelope)
        self._record_prompt_usage(envelope=envelope, raw_output_source=raw_source)
        self._record_llm_raw_output(raw_output)
        if append_assistant_to_transcript:
            self._append_provider_assistant_message(envelope.assistant_message)
        return raw_output

    async def _request_llm_envelope(self, messages: list[dict], temperature: float) -> LLMResponseEnvelope:
        if hasattr(self.llm, "chat_completion_envelope"):
            return await self.llm.chat_completion_envelope(
                messages=self._provider_safe_messages(messages),
                temperature=temperature,
            )

        raw_output = await self.llm.chat_completion(
            messages=self._provider_safe_messages(messages),
            temperature=temperature,
        )
        assistant_message = (
            self.llm.get_last_assistant_message()
            if hasattr(self.llm, "get_last_assistant_message")
            else None
        ) or {"role": "assistant", "content": raw_output}
        reasoning_content = (
            self.llm.get_last_reasoning_content()
            if hasattr(self.llm, "get_last_reasoning_content")
            else None
        )
        usage = self.llm.get_last_usage() if hasattr(self.llm, "get_last_usage") else None
        return LLMResponseEnvelope(
            assistant_message=assistant_message,
            content=assistant_message.get("content") if isinstance(assistant_message, dict) else raw_output,
            reasoning_content=reasoning_content,
            usage=usage,
            raw_response_meta=None,
        )

    def _raw_output_from_envelope(self, envelope: LLMResponseEnvelope) -> tuple[str, str]:
        content = envelope.content or ""
        if content.strip():
            return content, "content"

        reasoning_content = envelope.reasoning_content or ""
        if reasoning_content.strip():
            result = parse_and_validate_main_output(reasoning_content)
            if result.ok:
                if _looks_like_rescuable_reasoning_output(reasoning_content):
                    return reasoning_content, "reasoning_content_rescue"

        return content, "content_empty"

    def _initialize_prompt_transcript(self, snapshot):
        self._prompt_transcript_identity = self._current_llm_identity()
        include_profile = snapshot.status.value == "COLD"
        profile_views = self._build_profile_prompt_views(include_profile)
        system_prompt = build_system_prompt(
            soul_md=profile_views["soul"],
            memory_core_md=profile_views["memory_core"],
            today_memory_md=profile_views["today_memory"],
            tomorrow_topics_md=profile_views["tomorrow_topics"],
            include_profile=include_profile,
        )
        self._prompt_transcript = [{"role": "system", "content": system_prompt}]
        self._prompt_block_hashes = {
            "stable_block_hash": self._hash_text(build_stable_prompt_hash_source()),
            "profile_block_hash": self._hash_text(
                json.dumps(profile_views, ensure_ascii=False, sort_keys=True)
            ),
            "system_prompt_hash": self._hash_text(system_prompt),
        }
        for item in self._conversation_history:
            role = item.get("role", "user")
            content = item.get("text", "")
            if content:
                self._prompt_transcript.append({"role": role, "content": content})

    def _ensure_provider_transcript_identity(self):
        identity = self._current_llm_identity()
        if not self._prompt_transcript:
            self._prompt_transcript_identity = identity
            return
        if identity and self._prompt_transcript_identity and identity != self._prompt_transcript_identity:
            self.reset_provider_transcript(reason="llm_identity_changed")
            self._prompt_transcript_identity = identity
        elif identity and not self._prompt_transcript_identity:
            self._prompt_transcript_identity = identity

    def _current_llm_identity(self) -> Optional[str]:
        if hasattr(self.llm, "get_transcript_identity"):
            return self.llm.get_transcript_identity()
        return None

    def _build_profile_prompt_views(self, include_profile: bool) -> dict[str, str]:
        if not include_profile:
            return {
                "soul": "",
                "memory_core": "",
                "today_memory": "",
                "tomorrow_topics": "",
            }

        return {
            "soul": self._markdown_prompt_view(
                self.memory.read_soul(),
                PROMPT_VIEW_LIMITS["soul"],
                "SOUL.md",
            ),
            "memory_core": self._markdown_prompt_view(
                self.memory.read_memory_core(),
                PROMPT_VIEW_LIMITS["memory_core"],
                "MEMORY_CORE.md",
            ),
            "today_memory": self._markdown_prompt_view(
                self.memory.read_today_memory(),
                PROMPT_VIEW_LIMITS["today_memory"],
                "dm/YYYY-MM-DD.md",
            ),
            "tomorrow_topics": self._tomorrow_topics_prompt_view(
                self.memory.read_tomorrow_topics(),
                PROMPT_VIEW_LIMITS["tomorrow_topics"],
            ),
        }

    def _markdown_prompt_view(self, markdown: str, max_chars: int, source_name: str) -> str:
        text = (markdown or "").strip()
        if len(text) <= max_chars:
            return text

        headers = [line.rstrip() for line in text.splitlines() if line.lstrip().startswith("#")]
        header_text = "\n".join(headers)
        marker = f"\n\n[系统提示：{source_name} 已裁剪为 prompt 视图，仅保留标题和最近片段。]\n\n"
        reserved = len(header_text) + len(marker)
        tail_chars = max(0, max_chars - reserved)
        if tail_chars <= 0:
            return (header_text or text[:max_chars]).strip()[:max_chars]
        return f"{header_text}{marker}{text[-tail_chars:]}".strip()

    def _tomorrow_topics_prompt_view(self, markdown: str, max_chars: int) -> str:
        markdown, _ = expire_stale_candidates(markdown or "")
        candidates = parse_active_candidates(markdown or "")
        pending_by_section = {section: [] for section in ACTIVE_SECTIONS}
        for candidate in candidates:
            if candidate.status == "pending":
                pending_by_section.setdefault(candidate.section, []).append(candidate.text)

        lines = ["# 明日话题"]
        has_pending = False
        for section in ACTIVE_SECTIONS:
            lines.extend(["", f"## {section}"])
            pending = pending_by_section.get(section) or []
            if pending:
                has_pending = True
                lines.extend(f"- [pending] {text}" for text in pending)

        if not has_pending:
            lines.extend(["", "（无待处理候选）"])

        return self._markdown_prompt_view(
            "\n".join(lines),
            max_chars,
            "TOMORROW_TOPICS.md",
        )

    def _append_runtime_context(self, ctx: ProcessContext):
        snapshot = ctx.snapshot
        cold_meta = snapshot.cold_start_meta
        runtime_message = build_runtime_context_message(
            chat_status=snapshot.status.value,
            msg_index=ctx.gate.state.msg_index_today,
            last_message_age=(
                cold_meta.last_user_message_age
                if cold_meta
                else ctx.gate._get_last_message_age() or "unknown"
            ),
            cold_start_timestamp=cold_meta.timestamp if cold_meta else None,
        )
        self._last_runtime_block_hash = self._hash_text(runtime_message["content"])
        self._prompt_transcript.append(runtime_message)

    def _append_hot_turn_system_reminder(self, snapshot):
        if snapshot.status != ChatStatus.HOT:
            return
        reminder_message = build_hot_turn_system_reminder_message()
        self._last_hot_turn_reminder_hash = self._hash_text(reminder_message["content"])
        self._prompt_transcript.append(reminder_message)

    def _build_media_followup_payload(self, payload: dict) -> dict:
        result = payload.get("result") if isinstance(payload, dict) else None
        media_ref = payload.get("media_ref") if isinstance(payload, dict) else None
        download = payload.get("download") if isinstance(payload, dict) else None

        allowed_media_ref_keys = (
            "source",
            "qq_user_id",
            "onebot_message_id",
            "segment_index",
            "segment_type",
            "sub_type",
            "summary",
            "file",
            "file_size",
            "is_sticker",
            "download_status",
        )
        safe_media_ref = {}
        if isinstance(media_ref, dict):
            safe_media_ref = {
                key: media_ref.get(key)
                for key in allowed_media_ref_keys
                if media_ref.get(key) is not None
            }

        safe_download = {}
        if isinstance(download, dict):
            safe_download = {
                key: download.get(key)
                for key in ("status", "content_type", "bytes", "error")
                if download.get(key) is not None
            }

        return {
            "internal_event_harness": "media_followup",
            "status": "ready_to_reply",
            "media_key": payload.get("media_key") if isinstance(payload, dict) else None,
            "media_job_id": payload.get("media_job_id") if isinstance(payload, dict) else None,
            "session_id": payload.get("session_id") if isinstance(payload, dict) else None,
            "is_sticker": bool(payload.get("is_sticker")) if isinstance(payload, dict) else False,
            "media_ref": safe_media_ref,
            "download": safe_download,
            "image_understanding": result if isinstance(result, dict) else {},
            "required_output": (
                "这是后台刚看完的、用户刚刚发来的普通图片。"
                "请补发一条自然的网聊回应，明确回应图片内容或图片带来的话题。"
                "如果用户已经继续说话，用“刚刚那张图/你刚发的那张”轻轻衔接；"
                "不要解释内部看图流程，不要说自己在分析，不要复述字段名。"
                "输出普通自然语言；如果适合表情包，可在自然文本中插入 &&category:keywords&&。"
            ),
        }

    def _append_media_pending_payloads_and_enqueue_jobs(self, ctx: ProcessContext):
        self._last_media_debug = []
        for evt in ctx.snapshot.events:
            raw = self._event_get(evt, "raw") or {}
            if not isinstance(raw, dict):
                continue
            media_refs = raw.get("media_refs") or []
            if not isinstance(media_refs, list):
                continue

            for index, media_ref in enumerate(media_refs):
                if not isinstance(media_ref, dict):
                    continue
                media_key = self._media_harness_key(ctx, evt, media_ref, index)
                if media_key in self._media_harness_event_ids:
                    continue

                pending_payload = self._build_media_pending_payload(ctx, evt, media_ref, media_key, index)
                if self.media_job_queue:
                    enqueued = self.media_job_queue.enqueue(
                        MediaJob(
                            media_key=media_key,
                            session_id=ctx.snapshot.session_id,
                            snapshot_id=ctx.snapshot.snapshot_id,
                            buffer_version=ctx.snapshot.buffer_version,
                            source_job_id=ctx.job_id,
                            event=copy.deepcopy(evt),
                            media_ref=copy.deepcopy(media_ref),
                            event_text=str(self._event_get(evt, "text") or ""),
                            context_text=self._media_context_text(evt),
                        )
                    )
                    pending_payload["status"] = "queued" if enqueued else "already_queued"
                else:
                    pending_payload["status"] = "queue_not_configured"

                self._prompt_transcript.append({
                    "role": "system",
                    "content": json.dumps(pending_payload, ensure_ascii=False),
                })
                self._last_media_debug.append(pending_payload)
                self._media_harness_event_ids.add(media_key)

    def _build_media_pending_payload(
        self,
        ctx: ProcessContext,
        evt: dict,
        media_ref: dict,
        media_key: str,
        index: int,
    ) -> dict:
        is_sticker = bool(media_ref.get("is_sticker"))
        has_user_text = self._media_event_has_user_text(evt)
        kind = "sticker" if is_sticker else "ordinary_image"
        return {
            "internal_event_harness": "sticker_pending" if is_sticker else "media_pending",
            "status": "pending",
            "media_key": media_key,
            "snapshot_id": ctx.snapshot.snapshot_id,
            "buffer_version": ctx.snapshot.buffer_version,
            "segment_index": media_ref.get("segment_index", index),
            "kind": kind,
            "is_sticker": is_sticker,
            "has_user_text": has_user_text,
            "visibility": "internal_only",
            "background_processing": "queued_for_download_vision_and_intake",
            "media_ref": self._safe_media_ref_for_prompt(media_ref),
            "instruction": self._media_pending_instruction(is_sticker, has_user_text),
        }

    def _media_pending_instruction(self, is_sticker: bool, has_user_text: bool) -> str:
        if is_sticker:
            if has_user_text:
                return (
                    "用户混合发送了文字和表情包/贴纸。主聊天可以结合文字内容和“表情包=语气/心情/接梗信号”"
                    "轻轻回应；不要等待后台偷表情入库，不要分析表情包画面给用户听。"
                )
            return (
                "用户发的是表情包/贴纸，通常只是表达心情、语气或接梗。主聊天轻轻接住即可，"
                "可短句、WAIT、继续原话题或回表情包；不要等待后台入库，也不要输出“这个表情包说明你……”这类分析腔。"
            )

        if has_user_text:
            return (
                "用户混合发送了文字和普通图片。主聊天优先回复文字内容；图片是分享，后台正在理解，"
                "当前不要编造图片内容，也不要装作已经看清图片细节。"
            )
        return (
            "用户只发来普通图片，第一语义是分享。图片内容正在后台理解；当前可以先轻轻接住，"
            "例如表示看到了/正在看，但不要编造图片里有什么。"
        )

    def _media_event_has_user_text(self, evt: dict) -> bool:
        text = str(self._event_get(evt, "text") or "").strip()
        if not text:
            return False
        clean = re.sub(r"\[(?:图片|表情|QQ表情(?::[^\]]*)?)\]", "", text)
        return bool(clean.strip())

    def _media_harness_key(self, ctx: ProcessContext, evt: dict, media_ref: dict, index: int) -> str:
        event_id = self._event_get(evt, "event_id") or self._event_get(evt, "id") or f"snapshot_{ctx.snapshot.snapshot_id}"
        segment_index = media_ref.get("segment_index")
        if segment_index is None:
            segment_index = index
        identity = (
            media_ref.get("file_unique")
            or media_ref.get("file_id")
            or media_ref.get("file")
            or media_ref.get("url")
            or segment_index
        )
        return f"{ctx.snapshot.session_id}:{event_id}:{segment_index}:{identity}"

    def _safe_media_ref_for_prompt(self, media_ref: dict) -> dict:
        allowed_keys = (
            "source",
            "qq_user_id",
            "onebot_message_id",
            "segment_index",
            "segment_type",
            "sub_type",
            "summary",
            "file",
            "file_id",
            "file_unique",
            "file_size",
            "is_sticker",
            "sha256",
            "download_status",
            "download_error",
        )
        return {key: media_ref.get(key) for key in allowed_keys if media_ref.get(key) is not None}

    def _media_context_text(self, evt: dict) -> str:
        history_tail = self._conversation_history[-8:]
        lines = []
        for item in history_tail:
            role = item.get("role") or "unknown"
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(f"{role}: {text}")
        current = str(self._event_get(evt, "text") or "").strip()
        if current:
            lines.append(f"user: {current}")
        return "\n".join(lines)

    def _append_snapshot_events(self, events: list[dict]):
        for evt in events:
            event_id = self._event_get(evt, "event_id") or self._event_get(evt, "id")
            if event_id and event_id in self._prompt_event_ids:
                continue

            text = self._prompt_text_for_event(evt)
            if not text:
                continue

            self._prompt_transcript.append({"role": "user", "content": text})
            if event_id:
                self._prompt_event_ids.add(event_id)

    def _prompt_text_for_event(self, evt: dict) -> str:
        text = self._event_get(evt, "text")
        event_type = (
            self._event_get(evt, "event_type")
            or self._event_get(evt, "kind")
            or self._event_get(evt, "type")
            or ""
        )
        if hasattr(event_type, "value"):
            event_type = event_type.value
        event_type = str(event_type or "")
        if not text and event_type.endswith("image"):
            text = "[图片]"
        if not text and event_type.endswith("sticker"):
            text = "[表情]"
        text = text or ""

        raw = self._event_get(evt, "raw") or {}
        if not isinstance(raw, dict):
            return text
        reply_context = raw.get("reply_context")
        reply_to_message_id = str(raw.get("reply_to_message_id") or "").strip()
        if isinstance(reply_context, dict):
            quoted_text = str(reply_context.get("text") or "").strip()
            quoted_role = str(reply_context.get("role") or "unknown").strip() or "unknown"
            if quoted_text:
                return f"用户引用了 {quoted_role} 的消息：{quoted_text}\n用户回复：{text or '(空消息)'}"
        if reply_to_message_id:
            return f"用户引用了一条消息（message_id={reply_to_message_id}）\n用户回复：{text or '(空消息)'}"
        return text

    def _event_get(self, evt, key: str, default=None):
        if isinstance(evt, dict):
            if key in evt:
                return evt.get(key, default)
            payload = evt.get("payload")
            if isinstance(payload, dict) and key in payload:
                return payload.get(key, default)
            return default
        return getattr(evt, key, default)

    def _append_assistant_text_to_prompt(self, text: str):
        if text:
            self._prompt_transcript.append({"role": "assistant", "content": text})

    def _append_provider_assistant_message(self, assistant_message: dict):
        sanitized, skip_reason = self._sanitize_provider_assistant_message(assistant_message)
        if skip_reason:
            self._last_prompt_observability["assistant_message_skipped_reason"] = skip_reason
            return
        self._prompt_transcript.append(sanitized)

    def _append_transcript_messages_for_call(self, messages: list[dict]) -> list[dict]:
        self._prompt_transcript.extend(copy.deepcopy(item) for item in messages)
        current = self._copy_prompt_transcript()
        self._record_prompt_observability(current)
        return current

    def _copy_prompt_transcript(self) -> list[dict]:
        return copy.deepcopy(self._prompt_transcript)

    def _provider_role_sequence(self, messages: list[dict]) -> list[str]:
        return [str(item.get("role", "")) for item in messages]

    def _main_chat_temperature(self) -> float:
        return float(getattr(self.llm, "temperature", 1.0) or 1.0)

    def _sanitize_provider_assistant_message(self, assistant_message: Optional[dict]) -> tuple[dict, Optional[str]]:
        if not assistant_message:
            return {}, "empty_assistant_message"
        allowed = {
            "role",
            "content",
            "reasoning_content",
            "tool_calls",
            "function_call",
            "name",
            "tool_call_id",
        }
        sanitized = {
            key: copy.deepcopy(value)
            for key, value in assistant_message.items()
            if key in allowed
        }
        sanitized["role"] = "assistant"
        if "content" not in sanitized:
            sanitized["content"] = None

        has_content = bool(str(sanitized.get("content") or "").strip())
        has_reasoning = bool(str(sanitized.get("reasoning_content") or "").strip())
        has_tool_calls = bool(sanitized.get("tool_calls") or sanitized.get("function_call"))
        if not (has_content or has_reasoning or has_tool_calls):
            return {}, "empty_assistant_message"
        return sanitized, None

    def _provider_safe_messages(self, messages: list[dict]) -> list[dict]:
        safe_messages: list[dict] = []
        skipped = 0
        for item in messages:
            role = item.get("role")
            if role == "assistant":
                sanitized, skip_reason = self._sanitize_provider_assistant_message(item)
                if skip_reason:
                    skipped += 1
                    continue
                safe_messages.append(sanitized)
            elif role in {"system", "user"}:
                safe_messages.append(copy.deepcopy(item))
            else:
                # 当前项目没有 provider-native tool call，不向 provider 伪造其他 role。
                skipped += 1

        if skipped:
            self._last_prompt_observability["provider_safe_messages_skipped"] = skipped
        return safe_messages

    def _record_prompt_observability(self, messages: list[dict]):
        safe_messages = self._provider_safe_messages(messages)
        previous_messages = copy.deepcopy(self._last_prompt_messages)
        messages = safe_messages
        append_only = self._is_messages_prefix(previous_messages, messages) if previous_messages else True
        previous_hash = self._messages_hash(previous_messages) if previous_messages else None
        full_hash = self._messages_hash(messages)
        serialized = self._messages_json(messages)
        observability = {
            "message_count": len(messages),
            "previous_message_count": len(previous_messages),
            "role_sequence": self._provider_role_sequence(messages),
            "full_request_hash": full_hash,
            "previous_request_hash": previous_hash,
            **self._prompt_block_hashes,
            "runtime_block_hash": self._last_runtime_block_hash,
            "hot_turn_reminder_hash": self._last_hot_turn_reminder_hash,
            "append_only_check": append_only,
            "prefix_rebuild_reason": None if append_only else "previous_request_not_prefix",
            "char_count": len(serialized),
            "estimated_tokens": max(1, len(serialized) // 4),
            "estimated_prompt_tokens": max(1, len(serialized) // 4),
        }
        cache_debug = self.llm.get_cache_debug() if hasattr(self.llm, "get_cache_debug") else None
        if cache_debug:
            observability["cache_affinity"] = cache_debug
        if append_only and self._prefix_rebuild_reason:
            observability["prefix_rebuild_reason"] = self._prefix_rebuild_reason
        self._prefix_rebuild_reason = None
        self._last_prompt_messages = copy.deepcopy(messages)
        self._last_prompt_observability = observability
        print(f"[prompt_cache_debug] {json.dumps(observability, ensure_ascii=False)}")

    def _record_prompt_usage(
        self,
        envelope: Optional[LLMResponseEnvelope] = None,
        raw_output_source: Optional[str] = None,
    ):
        usage = envelope.usage if envelope else (
            self.llm.get_last_usage() if hasattr(self.llm, "get_last_usage") else None
        )
        if usage:
            self._last_prompt_observability["llm_usage"] = usage
            cached_tokens = self._extract_cached_tokens(usage)
            if cached_tokens is not None:
                self._last_prompt_observability["cached_tokens"] = cached_tokens
            prompt_tokens = self._extract_prompt_tokens(usage)
            if prompt_tokens is not None:
                self._last_prompt_observability["prompt_tokens"] = prompt_tokens
            cache_miss_tokens = self._extract_cache_miss_tokens(usage)
            if cache_miss_tokens is not None:
                self._last_prompt_observability["cache_miss_tokens"] = cache_miss_tokens
            reasoning_tokens = self._extract_reasoning_tokens(usage)
            if reasoning_tokens is not None:
                self._last_prompt_observability["reasoning_tokens"] = reasoning_tokens
            if cached_tokens is not None and prompt_tokens:
                self._last_prompt_observability["cache_hit_rate"] = cached_tokens / prompt_tokens
        reasoning_content = envelope.reasoning_content if envelope else (
            self.llm.get_last_reasoning_content()
            if hasattr(self.llm, "get_last_reasoning_content")
            else None
        )
        if reasoning_content:
            self._last_prompt_observability["llm_reasoning_content"] = reasoning_content
        assistant_message = envelope.assistant_message if envelope else (
            self.llm.get_last_assistant_message()
            if hasattr(self.llm, "get_last_assistant_message")
            else None
        )
        if assistant_message:
            self._last_prompt_observability["llm_assistant_message"] = assistant_message
            self._last_prompt_observability["assistant_has_reasoning_content"] = bool(
                assistant_message.get("reasoning_content")
            )
            self._last_prompt_observability["content_empty_with_reasoning"] = not bool(
                str(assistant_message.get("content") or "").strip()
            ) and bool(assistant_message.get("reasoning_content"))
        if envelope and envelope.raw_response_meta:
            self._last_prompt_observability["llm_response_meta"] = envelope.raw_response_meta
        if raw_output_source:
            self._last_prompt_observability["raw_output_source"] = raw_output_source

    def _extract_cached_tokens(self, usage: dict) -> Optional[int]:
        details = usage.get("prompt_tokens_details") or usage.get("input_token_details") or {}
        cached = (
            details.get("cached_tokens")
            or details.get("cache_read_input_tokens")
            or usage.get("prompt_cache_hit_tokens")
            or usage.get("cache_read_input_tokens")
        )
        if isinstance(cached, int):
            return cached
        return None

    def _extract_cache_miss_tokens(self, usage: dict) -> Optional[int]:
        details = usage.get("prompt_tokens_details") or usage.get("input_token_details") or {}
        missed = details.get("cache_miss_tokens") or usage.get("prompt_cache_miss_tokens")
        if isinstance(missed, int):
            return missed
        return None

    def _extract_prompt_tokens(self, usage: dict) -> Optional[int]:
        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
        if isinstance(prompt_tokens, int):
            return prompt_tokens
        return None

    def _extract_reasoning_tokens(self, usage: dict) -> Optional[int]:
        details = usage.get("completion_tokens_details") or usage.get("output_token_details") or {}
        reasoning_tokens = details.get("reasoning_tokens")
        if isinstance(reasoning_tokens, int):
            return reasoning_tokens
        return None

    def _messages_hash(self, messages: list[dict]) -> str:
        return hashlib.sha256(self._messages_json(messages).encode("utf-8")).hexdigest()

    def _messages_json(self, messages: list[dict]) -> str:
        return json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _is_messages_prefix(self, previous: list[dict], current: list[dict]) -> bool:
        if len(previous) > len(current):
            return False
        return current[: len(previous)] == previous

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
