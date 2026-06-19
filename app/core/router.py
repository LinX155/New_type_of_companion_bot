from typing import Optional

from .decisions import Action, ActionDecision, SendItem, SendItemType


class ActionRouter:
    def __init__(self):
        self._last_visible_text: Optional[str] = None
        self._last_action: Optional[Action] = None

    def route(self, decision: ActionDecision) -> dict:
        """路由 action 决定，返回发送层可消费的 typed items。"""
        self._last_action = decision.action
        send_items = decision.send_items()
        first_item = send_items[0] if send_items else None
        first_text = first_item.content if first_item else None
        texts = [item.content for item in send_items]

        if decision.action == Action.WAIT:
            return self._hidden(Action.WAIT.value, "waiting_for_user")

        if decision.action == Action.END_CHAT:
            return self._hidden(Action.END_CHAT.value, "end_chat")

        if decision.action == Action.REACT and not send_items and decision.search_meme_items():
            return {
                **self._hidden(Action.REACT.value, "internal_meme_search"),
                "is_meme": False,
            }

        if decision.action == Action.ENTER_CHAT:
            return self._visible_or_hidden(
                action=Action.ENTER_CHAT.value,
                reason="enter_chat",
                items=send_items,
                texts=texts,
                first_text=first_text,
            )

        if decision.action == Action.LIGHT_ACK:
            return self._visible_or_hidden(
                action=Action.LIGHT_ACK.value,
                reason="light_ack",
                items=send_items,
                texts=texts,
                first_text=first_text,
            )

        if decision.action == Action.REPLY:
            return self._visible_or_hidden(
                action=Action.REPLY.value,
                reason="reply",
                items=send_items,
                texts=texts,
                first_text=first_text,
            )

        if decision.action == Action.REACT:
            return self._visible_or_hidden(
                action=Action.REACT.value,
                reason="react",
                items=send_items,
                texts=texts,
                first_text=first_text,
                is_meme=True,
            )

        return self._hidden(decision.action.value, "unknown_action")

    def _hidden(self, action: str, reason: str) -> dict:
        return {
            "visible": False,
            "text": None,
            "texts": [],
            "items": [],
            "action": action,
            "reason": reason,
        }

    def _visible_or_hidden(
        self,
        action: str,
        reason: str,
        items: list[SendItem],
        texts: list[str],
        first_text: Optional[str],
        is_meme: Optional[bool] = None,
    ) -> dict:
        if not items:
            return self._hidden(action, reason)

        self._last_visible_text = first_text
        return {
            "visible": True,
            "text": first_text,
            "texts": texts,
            "items": items,
            "action": action,
            "reason": reason,
            "is_meme": is_meme if is_meme is not None else any(item.type != SendItemType.TEXT for item in items),
        }

    def get_last_visible_text(self) -> Optional[str]:
        return self._last_visible_text

    def get_last_action(self) -> Optional[Action]:
        return self._last_action
