from enum import Enum
from typing import Any, Optional, Union

from pydantic import BaseModel, field_validator, model_validator

REACT_PROTOCOL_PREFIXES = ("emoji:", "search_meme:", "meme:")
DecisionText = Union[str, list[str], None]
SEARCH_MEME_PREFIX = "search_meme:"
MEME_PREFIX = "meme:"
EMOJI_PREFIX = "emoji:"


class Action(str, Enum):
    WAIT = "WAIT"
    REPLY = "REPLY"
    LIGHT_ACK = "LIGHT_ACK"
    REACT = "REACT"
    ENTER_CHAT = "ENTER_CHAT"
    END_CHAT = "END_CHAT"


class SendItemType(str, Enum):
    TEXT = "text"
    EMOJI = "emoji"
    MEME = "meme"
    SEARCH_MEME = "search_meme"


class SendItem(BaseModel):
    type: SendItemType
    content: str

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value):
        if isinstance(value, SendItemType):
            return value
        return str(value or "").strip().lower()

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value):
        return str(value or "").strip()

    @model_validator(mode="after")
    def validate_content(self):
        if not self.content:
            raise ValueError("send item content cannot be empty")

        if self.type == SendItemType.EMOJI and not self.content.startswith(EMOJI_PREFIX):
            self.content = f"{EMOJI_PREFIX}{self.content}"

        if self.type == SendItemType.MEME and not self.content.startswith(MEME_PREFIX):
            self.content = f"{MEME_PREFIX}{self.content}"

        if self.type == SendItemType.SEARCH_MEME and not self.content.startswith(SEARCH_MEME_PREFIX):
            self.content = f"{SEARCH_MEME_PREFIX}{self.content}"

        return self

    def harness_value(self) -> str:
        if self.type == SendItemType.EMOJI:
            return self.content[len(EMOJI_PREFIX):] if self.content.startswith(EMOJI_PREFIX) else self.content
        if self.type == SendItemType.MEME:
            return self.content[len(MEME_PREFIX):] if self.content.startswith(MEME_PREFIX) else self.content
        if self.type == SendItemType.SEARCH_MEME:
            return (
                self.content[len(SEARCH_MEME_PREFIX):]
                if self.content.startswith(SEARCH_MEME_PREFIX)
                else self.content
            )
        return self.content

    def to_harness_item(self) -> dict:
        if self.type == SendItemType.MEME:
            return {"meme": self.harness_value()}
        if self.type == SendItemType.SEARCH_MEME:
            return {"search_meme": self.harness_value()}
        return {"text": self.harness_value()}


class ActionDecision(BaseModel):
    action: Action
    items: Optional[list[SendItem]] = None
    text: DecisionText = None

    @field_validator("text", mode="before")
    @classmethod
    def normalize_empty_text(cls, value):
        if value == "":
            return None
        if isinstance(value, list):
            bubbles = []
            for item in value:
                if item is None:
                    continue
                text = str(item).strip()
                if text:
                    bubbles.append(text)
            return bubbles or None
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("items", mode="before")
    @classmethod
    def normalize_items(cls, value):
        if value in (None, ""):
            return None
        if not isinstance(value, list):
            value = [value]

        items: list[SendItem] = []
        for raw in value:
            try:
                item = cls._item_from_raw(raw)
            except Exception:
                continue
            if item:
                items.append(item)
        return items or None

    @model_validator(mode="after")
    def validate_action_payload(self):
        if self.action in (Action.WAIT, Action.END_CHAT):
            self.items = None
            self.text = None
            return self

        if self.items is None:
            self.items = self._items_from_legacy_text(self.action, self.text)

        if self.items is not None and self.text is None:
            self.text = self._legacy_text_from_items(self.items)

        all_items = self.all_items()

        if self.action == Action.REPLY and not all_items:
            raise ValueError("REPLY action requires at least one item")

        if self.action == Action.LIGHT_ACK and not all_items:
            raise ValueError("LIGHT_ACK action requires at least one item")

        if self.action == Action.REACT:
            if not all_items:
                raise ValueError("REACT action requires at least one react item")
            if not any(item.type != SendItemType.TEXT for item in all_items):
                self.action = Action.REPLY

        return self

    @classmethod
    def _item_from_raw(cls, raw: Any) -> Optional[SendItem]:
        if isinstance(raw, SendItem):
            return raw

        if isinstance(raw, str):
            return cls._item_from_string(raw)

        if isinstance(raw, dict):
            simple_item = cls._item_from_simple_dict(raw)
            if simple_item:
                return simple_item
            return None

        return cls._item_from_string(str(raw))

    @classmethod
    def _item_from_simple_dict(cls, raw: dict) -> Optional[SendItem]:
        for key, item_type in (
            ("text", SendItemType.TEXT),
            ("meme", SendItemType.MEME),
            ("search_meme", SendItemType.SEARCH_MEME),
        ):
            if key not in raw:
                continue
            value = str(raw.get(key) or "").strip()
            if not value:
                return None
            return SendItem(type=item_type, content=value)
        return None

    @classmethod
    def _item_from_string(cls, value: str) -> Optional[SendItem]:
        value = str(value or "").strip()
        if not value:
            return None

        protocol_item = cls._item_from_protocol_string(value)
        if protocol_item:
            return protocol_item

        return SendItem(type=SendItemType.TEXT, content=value)

    @classmethod
    def _item_from_protocol_string(cls, value: str) -> Optional[SendItem]:
        if value.startswith(EMOJI_PREFIX):
            return SendItem(type=SendItemType.EMOJI, content=value)
        if value.startswith(MEME_PREFIX):
            return SendItem(type=SendItemType.MEME, content=value)
        if value.startswith(SEARCH_MEME_PREFIX):
            return SendItem(type=SendItemType.SEARCH_MEME, content=value)
        return None

    @classmethod
    def _items_from_legacy_text(cls, action: Action, value: DecisionText) -> Optional[list[SendItem]]:
        if value is None:
            return None

        values = value if isinstance(value, list) else [value]
        items: list[SendItem] = []
        for raw in values:
            text = str(raw or "").strip()
            if not text:
                continue

            if action == Action.REACT:
                protocol_item = cls._item_from_protocol_string(text)
                if protocol_item:
                    items.append(protocol_item)
                    continue
                if cls._looks_like_emoji(text):
                    items.append(SendItem(type=SendItemType.EMOJI, content=text))
                    continue
                items.append(SendItem(type=SendItemType.TEXT, content=text))
                continue

            items.append(cls._item_from_string(text))

        return items or None

    @staticmethod
    def _legacy_text_from_items(items: list[SendItem]) -> DecisionText:
        if not items:
            return None
        values = [item.content for item in items]
        if len(values) == 1:
            return values[0]
        return values

    @staticmethod
    def _looks_like_emoji(text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped or len(stripped) > 8:
            return False
        return any(ord(ch) >= 0x2600 for ch in stripped)

    def all_items(self) -> list[SendItem]:
        return list(self.items or [])

    def send_items(self) -> list[SendItem]:
        return [item for item in self.all_items() if item.type != SendItemType.SEARCH_MEME]

    def search_meme_items(self) -> list[SendItem]:
        return [item for item in self.all_items() if item.type == SendItemType.SEARCH_MEME]

    def text_bubbles(self) -> list[str]:
        return [item.content for item in self.all_items() if item.type == SendItemType.TEXT]

    def with_items(self, items: list[SendItem], action: Optional[Action] = None) -> "ActionDecision":
        return ActionDecision(action=action or self.action, items=items)

    def to_harness_payload(self, exclude_none: bool = False) -> dict:
        items = [item.to_harness_item() for item in self.all_items()]
        payload = {
            "action": self.action.value,
            "items": items or None,
        }
        if exclude_none and payload["items"] is None:
            payload.pop("items")
        return payload
