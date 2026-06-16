from enum import Enum
from typing import Optional
from pydantic import BaseModel, field_validator, model_validator


class Action(str, Enum):
    WAIT = "WAIT"
    REPLY = "REPLY"
    LIGHT_ACK = "LIGHT_ACK"
    REACT = "REACT"
    ENTER_CHAT = "ENTER_CHAT"
    END_CHAT = "END_CHAT"


class ActionDecision(BaseModel):
    action: Action
    text: Optional[str] = None

    @field_validator("text", mode="before")
    @classmethod
    def normalize_empty_text(cls, value):
        if value == "":
            return None
        return value

    @model_validator(mode="after")
    def validate_action_text(self):
        if self.action == Action.WAIT and self.text is not None:
            raise ValueError("WAIT action requires text to be null")

        if self.action == Action.REPLY and not self.text:
            raise ValueError("REPLY action requires visible text")

        if self.action == Action.LIGHT_ACK and not self.text:
            raise ValueError("LIGHT_ACK action requires short visible text")

        if self.action == Action.REACT and not self.text:
            raise ValueError("REACT action requires emoji, image path, or resource id")

        if self.action == Action.END_CHAT and self.text is not None:
            raise ValueError("END_CHAT action requires text to be null")

        return self
