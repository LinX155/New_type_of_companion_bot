import json
import re
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from .event_gate import ProcessContext
from .decisions import ActionDecision, Action, SendItem, SendItemType
from ..llm.client import LLMClient
from ..llm.prompts import build_system_prompt, build_messages, build_meme_search_messages
from ..memory.files import MemoryFileManager
from ..memes.catalog import MemeCatalog
from ..memes.search import MemeSearch
from ..memes.renderer import MemeRenderer


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
    ):
        self.llm = llm_client
        self.memory = memory_manager
        self.meme_catalog = meme_catalog
        self.meme_search = MemeSearch(meme_catalog)
        self.meme_renderer = MemeRenderer(meme_catalog)
        self._conversation_history: list = []
        self._workflow = self._build_workflow()
        # Debug / observability fields for the MVP status bar
        self._last_llm_raw_output: Optional[str] = None
        self._last_parsed_action: Optional[str] = None
        self._last_parsed_text: Optional[str | list[str]] = None
        self._last_parsed_items: Optional[list[dict]] = None
        self._last_parse_status: str = "ok"
        self._last_search_meme: Optional[str] = None
        self._last_render_status: Optional[str] = None

    def _build_workflow(self):
        graph = StateGraph(GraphState)
        graph.add_node("receive_snapshot", self._receive_snapshot)
        graph.add_node("build_context", self._build_context)
        graph.add_node("call_llm_for_decision", self._call_llm_for_decision)
        graph.add_node("search_meme", self._search_meme)
        graph.add_node("validate_decision", self._validate_decision)

        graph.set_entry_point("receive_snapshot")
        graph.add_edge("receive_snapshot", "build_context")
        graph.add_edge("build_context", "call_llm_for_decision")
        graph.add_conditional_edges(
            "call_llm_for_decision",
            self._route_after_first_decision,
            {
                "search_meme": "search_meme",
                "validate_decision": "validate_decision",
            },
        )
        graph.add_edge("search_meme", "validate_decision")
        graph.add_edge("validate_decision", END)
        return graph.compile()

    async def run(self, ctx: ProcessContext) -> ActionDecision:
        result = await self._workflow.ainvoke({"ctx": ctx})
        decision = result.get("decision") or ActionDecision(action=Action.WAIT, text=None)
        ctx.decision = decision

        return decision

    async def _receive_snapshot(self, state: GraphState) -> GraphState:
        return state

    async def _build_context(self, state: GraphState) -> GraphState:
        ctx = state["ctx"]
        snapshot = ctx.snapshot
        include_profile = snapshot.status.value == "COLD"

        system_prompt = build_system_prompt(
            soul_md=self.memory.read_soul() if include_profile else "",
            memory_core_md=self.memory.read_memory_core() if include_profile else "",
            today_memory_md=self.memory.read_today_memory() if include_profile else "",
            tomorrow_topics_md=self.memory.read_tomorrow_topics() if include_profile else "",
            chat_status=snapshot.status.value,
            msg_index=snapshot.cold_start_meta.msg_index if snapshot.cold_start_meta else 0,
            last_message_age=snapshot.cold_start_meta.last_user_message_age if snapshot.cold_start_meta else "unknown",
            include_profile=include_profile,
        )

        pending_history = []
        for evt in snapshot.events:
            text = evt.get("text")
            event_type = evt.get("event_type", "")
            if not text and event_type.endswith("image"):
                text = "[图片]"
            if not text and event_type.endswith("sticker"):
                text = "[表情]"
            if text:
                pending_history.append({"role": "user", "text": text})

        history = [*self._conversation_history[-30:], *pending_history]
        messages = build_messages(
            system_prompt=system_prompt,
            history=history,
            cold_start_meta=snapshot.cold_start_meta.model_dump() if snapshot.cold_start_meta else None,
        )
        return {**state, "messages": messages}

    async def _call_llm_for_decision(self, state: GraphState) -> GraphState:
        raw_output = await self.llm.chat_completion(
            messages=state["messages"],
            temperature=0.3,
            max_tokens=512,
        )
        self._last_llm_raw_output = raw_output
        decision, parse_status = self._parse_decision(raw_output)
        self._last_parse_status = parse_status
        decision = self._apply_protocol_guards(state["ctx"], decision)
        self._last_parsed_action = decision.action.value
        self._last_parsed_text = decision.text
        self._last_parsed_items = [item.model_dump(mode="json") for item in decision.all_items()]
        state["ctx"].decision = decision
        return {**state, "decision": decision}

    def _route_after_first_decision(self, state: GraphState) -> str:
        decision = state.get("decision")
        if decision and decision.search_meme_items():
            return "search_meme"
        return "validate_decision"

    async def _search_meme(self, state: GraphState) -> GraphState:
        ctx = state["ctx"]
        decision = state["decision"]
        search_items = decision.search_meme_items()

        self._last_search_meme = "\n".join(item.content for item in search_items) or None
        self._last_render_status = None

        if ctx.meme_search_used:
            self._last_render_status = "fallback"
            return {**state, "decision": self._drop_search_items_or_fallback(decision)}

        ctx.meme_search_used = True
        search_results = []
        candidate_union: set[str] = set()

        for item in search_items:
            parsed = self.meme_renderer.parse_react_text(item.content)
            category = parsed.get("category", "")
            keywords = parsed.get("keywords", "")
            candidates = self.meme_search.search(category, keywords, top_k=5)
            search_results.append(
                {
                    "request": item.content,
                    "category": category,
                    "keywords": keywords,
                    "candidates": candidates,
                }
            )
            candidate_union.update(candidates)

        ctx.meme_candidates = search_results

        if not candidate_union:
            self._last_render_status = "fallback"
            return {**state, "decision": self._drop_search_items_or_fallback(decision)}

        messages = build_meme_search_messages(
            base_messages=state["messages"],
            search_results=search_results,
            original_decision=decision.model_dump(mode="json", exclude_none=True),
        )

        raw_output = await self.llm.chat_completion(
            messages=messages,
            temperature=0.2,
            max_tokens=512,
        )
        second_decision, _ = self._parse_decision(raw_output)

        if self._meme_selection_is_valid(second_decision, candidate_union):
            selected = self._selected_meme_stems(second_decision)
            ctx.selected_memes = selected
            ctx.selected_meme = selected[0] if selected else None
            return {**state, "decision": second_decision}

        fallback_decision = self._replace_search_items_with_first_candidates(decision, search_results)
        selected = self._selected_meme_stems(fallback_decision)
        ctx.selected_memes = selected
        ctx.selected_meme = selected[0] if selected else None
        return {**state, "decision": fallback_decision}

    def _meme_selection_is_valid(self, decision: ActionDecision, candidate_union: set[str]) -> bool:
        if decision.search_meme_items():
            return False

        for item in decision.all_items():
            if item.type == SendItemType.MEME:
                stem = item.content[5:]
                if stem not in candidate_union:
                    return False
        return bool(decision.send_items())

    def _selected_meme_stems(self, decision: ActionDecision) -> list[str]:
        return [item.content[5:] for item in decision.all_items() if item.type == SendItemType.MEME]

    def _replace_search_items_with_first_candidates(
        self,
        decision: ActionDecision,
        search_results: list[dict],
    ) -> ActionDecision:
        results_by_request = {item.get("request"): item.get("candidates") or [] for item in search_results}
        replaced_items: list[SendItem] = []

        for item in decision.all_items():
            if item.type != SendItemType.SEARCH_MEME:
                replaced_items.append(item)
                continue

            candidates = results_by_request.get(item.content) or []
            if candidates:
                replaced_items.append(SendItem(type=SendItemType.MEME, content=f"meme:{candidates[0]}"))

        if replaced_items:
            return decision.with_items(replaced_items)
        return ActionDecision(action=Action.LIGHT_ACK, items=[SendItem(type=SendItemType.TEXT, content="嗯")])

    def _drop_search_items_or_fallback(self, decision: ActionDecision) -> ActionDecision:
        kept_items = [item for item in decision.all_items() if item.type != SendItemType.SEARCH_MEME]
        if kept_items:
            return decision.with_items(kept_items)
        return ActionDecision(action=Action.LIGHT_ACK, items=[SendItem(type=SendItemType.TEXT, content="嗯")])

    async def _validate_decision(self, state: GraphState) -> GraphState:
        decision = state["decision"]
        ctx = state["ctx"]

        validated_items: list[SendItem] = []
        selected_memes: list[str] = []
        dropped_internal = False
        missed_meme = False

        for item in decision.all_items():
            if item.type == SendItemType.SEARCH_MEME:
                dropped_internal = True
                continue

            if item.type == SendItemType.MEME:
                file_stem = item.content[5:]
                path = self.meme_renderer.render_meme(file_stem)
                if not path:
                    missed_meme = True
                    continue
                selected_memes.append(file_stem)

            validated_items.append(item)

        if selected_memes:
            self._last_render_status = "hit"
            ctx.selected_memes = selected_memes
            ctx.selected_meme = selected_memes[0]
        elif missed_meme:
            self._last_render_status = "miss"
            ctx.selected_memes = []
            ctx.selected_meme = None
        elif dropped_internal:
            self._last_render_status = "fallback"

        if validated_items != decision.all_items():
            if validated_items:
                decision = decision.with_items(validated_items)
            elif decision.action in (Action.WAIT, Action.END_CHAT, Action.ENTER_CHAT):
                decision = ActionDecision(action=decision.action, items=None)
            else:
                decision = ActionDecision(action=Action.LIGHT_ACK, items=[SendItem(type=SendItemType.TEXT, content="嗯")])

        ctx.decision = decision
        return {**state, "decision": decision}

    def commit_turn(self, ctx: ProcessContext, decision: ActionDecision, visible: bool):
        if visible:
            self.commit_sent_items(ctx, decision.send_items())

    def commit_sent_bubbles(self, ctx: ProcessContext, bubbles: list[str]):
        self.commit_sent_items(ctx, [SendItem(type=SendItemType.TEXT, content=bubble) for bubble in bubbles])

    def commit_sent_items(self, ctx: ProcessContext, items: list[SendItem]):
        if not items:
            return

        for evt in ctx.snapshot.events:
            text = evt.get("text")
            if text:
                self._conversation_history.append({"role": "user", "text": text})

        for item in items:
            self._conversation_history.append({"role": "assistant", "text": self._history_text_for_item(item)})

        self._conversation_history = self._conversation_history[-60:]

    def _history_text_for_item(self, item: SendItem) -> str:
        if item.type == SendItemType.TEXT:
            return item.content
        if item.type == SendItemType.EMOJI:
            return item.content[6:]
        return f"[表情: {item.content}]"

    def _parse_decision(self, raw_output: str) -> tuple[ActionDecision, str]:
        text = raw_output.strip() if raw_output else ""
        if not text:
            self._last_parsed_action = Action.WAIT.value
            self._last_parsed_text = None
            self._last_parsed_items = None
            return ActionDecision(action=Action.WAIT, text=None), "fallback"

        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                action_str = str(data.get("action", "REPLY")).strip().upper()
                if action_str in [a.value for a in Action]:
                    parsed = self._build_decision(action_str, data.get("text"), data.get("items"))
                    self._last_parsed_action = parsed.action.value
                    self._last_parsed_text = parsed.text
                    self._last_parsed_items = [item.model_dump(mode="json") for item in parsed.all_items()]
                    return parsed, "ok"
            except Exception:
                pass

        try:
            text_clean = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
            text_clean = re.sub(r"\s*```$", "", text_clean)
            data = json.loads(text_clean)
            action_str = str(data.get("action", "REPLY")).strip().upper()
            if action_str in [a.value for a in Action]:
                parsed = self._build_decision(action_str, data.get("text"), data.get("items"))
                self._last_parsed_action = parsed.action.value
                self._last_parsed_text = parsed.text
                self._last_parsed_items = [item.model_dump(mode="json") for item in parsed.all_items()]
                return parsed, "ok"
        except Exception:
            pass

        bracket_match = re.match(r"\[(WAIT|REPLY|LIGHT_ACK|REACT|ENTER_CHAT|END_CHAT)\]\s*(.*)", text, re.IGNORECASE)
        if bracket_match:
            try:
                action_str = bracket_match.group(1).upper()
                action_text = bracket_match.group(2).strip() or None
                parsed = self._build_decision(action_str, action_text)
                self._last_parsed_action = parsed.action.value
                self._last_parsed_text = parsed.text
                self._last_parsed_items = [item.model_dump(mode="json") for item in parsed.all_items()]
                return parsed, "ok"
            except Exception:
                pass

        upper_text = text.upper()
        if '"ACTION":"WAIT"' in upper_text or "'ACTION':'WAIT'" in upper_text:
            self._last_parsed_action = Action.WAIT.value
            self._last_parsed_text = None
            self._last_parsed_items = None
            return ActionDecision(action=Action.WAIT, text=None), "ok"

        if '"ACTION":"REACT"' in upper_text or "'ACTION':'REACT'" in upper_text:
            text_match = re.search(r'"text"[:\s]*"([^"]*)"', text, re.IGNORECASE)
            if text_match:
                try:
                    parsed = self._build_decision(Action.REACT.value, text_match.group(1))
                    self._last_parsed_action = parsed.action.value
                    self._last_parsed_text = parsed.text
                    self._last_parsed_items = [item.model_dump(mode="json") for item in parsed.all_items()]
                    return parsed, "ok"
                except Exception:
                    self._last_parsed_action = Action.WAIT.value
                    self._last_parsed_text = None
                    self._last_parsed_items = None
                    return ActionDecision(action=Action.WAIT, text=None), "fallback"

        if "REACT" in upper_text:
            self._last_parsed_action = Action.WAIT.value
            self._last_parsed_text = None
            self._last_parsed_items = None
            return ActionDecision(action=Action.WAIT, text=None), "fallback"

        parsed = ActionDecision(action=Action.REPLY, text=text)
        self._last_parsed_action = parsed.action.value
        self._last_parsed_text = parsed.text
        self._last_parsed_items = [item.model_dump(mode="json") for item in parsed.all_items()]
        return parsed, "fallback"

    def _build_decision(self, action_str: str, text_value, items_value=None) -> ActionDecision:
        if action_str in (Action.WAIT.value, Action.END_CHAT.value):
            text_value = None
            items_value = None
        if action_str == Action.REACT.value and isinstance(text_value, list):
            text_value = [self._normalize_react_text(item) for item in text_value]
        if action_str == Action.REACT.value and isinstance(text_value, str):
            text_value = self._normalize_react_text(text_value)
        return ActionDecision(action=action_str, text=text_value, items=items_value)

    def _normalize_react_text(self, action_text: str) -> str:
        valid_categories = {
            "amused", "distress", "observing", "disdain", "overload",
            "surprised", "resting", "smiling", "helpless", "confused",
            "affection", "praise", "intimidating", "bashful", "angry",
            "energetic", "interaction", "miscellaneous",
        }
        lower_text = action_text.lower().strip()
        if lower_text.startswith(("search_meme:", "meme:", "emoji:")):
            return action_text
        parts = lower_text.split(maxsplit=1)
        if parts and parts[0] in valid_categories:
            keywords = parts[1] if len(parts) > 1 else ""
            return f"search_meme:{parts[0]}:{keywords}"
        if self._looks_like_emoji(action_text):
            return f"emoji:{action_text.strip()}"
        return action_text

    def _looks_like_emoji(self, text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped or len(stripped) > 8:
            return False
        return any(ord(ch) >= 0x2600 for ch in stripped)

    def _apply_protocol_guards(self, ctx: ProcessContext, decision: ActionDecision) -> ActionDecision:
        pending_text = "\n".join(
            str(evt.get("text") or "")
            for evt in ctx.snapshot.events
            if evt.get("text")
        )
        if self._requires_meme_response(pending_text):
            has_react_item = any(
                item.type in (SendItemType.SEARCH_MEME, SendItemType.MEME, SendItemType.EMOJI)
                for item in decision.all_items()
            )
            if not has_react_item:
                return ActionDecision(
                    action=Action.REACT,
                    items=[SendItem(type=SendItemType.SEARCH_MEME, content="search_meme:amused:funny")],
                )
        return decision

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

    # Debug / observability getters for the MVP status bar
    def get_last_llm_raw_output(self) -> Optional[str]:
        return self._last_llm_raw_output

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

    def get_last_llm_observability(self) -> dict:
        return {
            "last_llm_raw": self._last_llm_raw_output,
            "last_parsed_action": self._last_parsed_action,
            "last_parsed_text": self._last_parsed_text,
            "last_parsed_items": self._last_parsed_items,
            "parse_status": self._last_parse_status,
            "last_search_meme": self._last_search_meme,
            "last_render_status": self._last_render_status,
        }
