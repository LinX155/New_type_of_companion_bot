from typing import Optional
from .decisions import ActionDecision, Action


class ActionRouter:
    def __init__(self):
        self._last_visible_text: Optional[str] = None
        self._last_action: Optional[Action] = None

    def route(self, decision: ActionDecision) -> dict:
        """路由 action 决定，返回处理结果"""
        self._last_action = decision.action

        if decision.action == Action.WAIT:
            return {
                "visible": False,
                "text": None,
                "action": Action.WAIT.value,
                "reason": "waiting_for_user",
            }

        if decision.action == Action.END_CHAT:
            return {
                "visible": False,
                "text": None,
                "action": Action.END_CHAT.value,
                "reason": "end_chat",
            }

        if decision.action == Action.ENTER_CHAT:
            # ENTER_CHAT 可能带有短回复，也可能为 null
            result = {
                "visible": decision.text is not None,
                "text": decision.text,
                "action": Action.ENTER_CHAT.value,
                "reason": "enter_chat",
            }
            if decision.text:
                self._last_visible_text = decision.text
            return result

        if decision.action == Action.LIGHT_ACK:
            self._last_visible_text = decision.text
            return {
                "visible": True,
                "text": decision.text,
                "action": Action.LIGHT_ACK.value,
                "reason": "light_ack",
            }

        if decision.action == Action.REPLY:
            self._last_visible_text = decision.text
            return {
                "visible": True,
                "text": decision.text,
                "action": Action.REPLY.value,
                "reason": "reply",
            }

        if decision.action == Action.REACT:
            if decision.text and decision.text.startswith("search_meme:"):
                return {
                    "visible": False,
                    "text": None,
                    "action": Action.REACT.value,
                    "reason": "internal_meme_search",
                    "is_meme": False,
                }
            # REACT 需要特殊处理表情包
            return {
                "visible": True,
                "text": decision.text,
                "action": Action.REACT.value,
                "reason": "react",
                "is_meme": decision.text and (decision.text.startswith("meme:") or decision.text.startswith("emoji:") or decision.text.startswith("search_meme:")),
            }

        return {
            "visible": False,
            "text": None,
            "action": decision.action.value,
            "reason": "unknown_action",
        }

    def get_last_visible_text(self) -> Optional[str]:
        return self._last_visible_text

    def get_last_action(self) -> Optional[Action]:
        return self._last_action
