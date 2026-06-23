import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, List, Dict, Optional, AsyncGenerator
from openai import AsyncOpenAI


DEFAULT_TEMPERATURE = 1.0


@dataclass
class LLMResponseEnvelope:
    assistant_message: dict
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    usage: Optional[dict] = None
    raw_response_meta: Optional[dict] = None


class LLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        thinking_enabled: bool = True,
        temperature: Optional[float] = None,
        cache_affinity_enabled: bool = True,
        cache_session_id: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.base_url = base_url or os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
        self.model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        self.thinking_enabled = thinking_enabled
        self.temperature = self._normalize_temperature(
            temperature if temperature is not None else os.getenv("LLM_TEMPERATURE")
        )
        self.cache_affinity_enabled = cache_affinity_enabled
        self.cache_session_id = cache_session_id or os.getenv("LLM_CACHE_SESSION_ID") or self._new_cache_session_id()
        self._prompt_cache_key_disabled_reason: Optional[str] = None
        self._client: Optional[AsyncOpenAI] = None
        self._last_usage: Optional[dict] = None
        self._last_reasoning_content: Optional[str] = None
        self._last_assistant_message: Optional[dict] = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    def _build_kwargs(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        stream: bool,
        include_prompt_cache_key: bool = True,
    ) -> dict:
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if self.thinking_enabled:
            # 思考模式：reasoning 模型通常要求省略 temperature，并用 thinking 字段开启
            kwargs["extra_body"] = {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            }
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            kwargs["temperature"] = temperature
        if self.cache_affinity_enabled:
            kwargs["extra_headers"] = self._cache_affinity_headers()
            if include_prompt_cache_key and not self._prompt_cache_key_disabled_reason:
                kwargs["prompt_cache_key"] = self._prompt_cache_key()
        return kwargs

    async def _create_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: Optional[int],
        stream: bool,
    ):
        client = self._get_client()
        try:
            return await client.chat.completions.create(
                **self._build_kwargs(
                    messages,
                    temperature,
                    max_tokens,
                    stream,
                    include_prompt_cache_key=True,
                )
            )
        except Exception as exc:
            if not self._should_retry_without_prompt_cache_key(exc):
                raise
            self._prompt_cache_key_disabled_reason = str(exc)[:300]
            return await client.chat.completions.create(
                **self._build_kwargs(
                    messages,
                    temperature,
                    max_tokens,
                    stream,
                    include_prompt_cache_key=False,
                )
            )

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
    ) -> str:
        response = await self._create_chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
        )

        if stream:
            self._last_usage = None
            self._last_reasoning_content = None
            self._last_assistant_message = None
            content = ""
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    content += chunk.choices[0].delta.content
            return content

        envelope = self._envelope_from_response(response)
        return envelope.content or ""

    async def chat_completion_envelope(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResponseEnvelope:
        response = await self._create_chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        return self._envelope_from_response(response)

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        response = await self._create_chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )

        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
        self._last_usage = None
        self._last_reasoning_content = None
        self._last_assistant_message = None

    def get_last_usage(self) -> Optional[dict]:
        return self._last_usage

    def get_last_reasoning_content(self) -> Optional[str]:
        return self._last_reasoning_content

    def get_last_assistant_message(self) -> Optional[dict]:
        return self._last_assistant_message

    def _serialize_usage(self, usage: Any) -> Optional[dict]:
        if usage is None:
            return None
        if hasattr(usage, "model_dump"):
            return usage.model_dump()
        if isinstance(usage, dict):
            return usage
        result = {}
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_tokens_details",
            "completion_tokens_details",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "input_tokens",
            "output_tokens",
            "input_token_details",
            "output_token_details",
        ):
            if hasattr(usage, key):
                value = getattr(usage, key)
                if hasattr(value, "model_dump"):
                    value = value.model_dump()
                result[key] = value
        return result or None

    def _serialize_message(self, message: Any) -> Optional[dict]:
        if message is None:
            return None
        if hasattr(message, "model_dump"):
            return message.model_dump()
        if isinstance(message, dict):
            return message
        result = {}
        for key in (
            "role",
            "content",
            "reasoning_content",
            "tool_calls",
            "function_call",
            "name",
            "tool_call_id",
        ):
            if hasattr(message, key):
                value = getattr(message, key)
                value = self._to_plain_data(value)
                result[key] = value
        return result or None

    def _serialize_assistant_message_for_history(self, message: Any) -> dict:
        raw = self._serialize_message(message) or {}
        allowed = {
            "role",
            "content",
            "reasoning_content",
            "tool_calls",
            "function_call",
            "name",
            "tool_call_id",
        }
        result = {}
        for key, value in raw.items():
            if key in allowed:
                result[key] = self._to_plain_data(value)
        result["role"] = result.get("role") or "assistant"
        if "content" not in result:
            result["content"] = None
        return result

    def _serialize_response_meta(self, response: Any) -> Optional[dict]:
        result = {}
        for key in ("id", "model", "created", "object", "system_fingerprint"):
            if hasattr(response, key):
                result[key] = self._to_plain_data(getattr(response, key))
        choice = response.choices[0] if getattr(response, "choices", None) else None
        if choice is not None:
            for key in ("finish_reason", "index"):
                if hasattr(choice, key):
                    result[key] = self._to_plain_data(getattr(choice, key))
        return result or None

    def _envelope_from_response(self, response: Any) -> LLMResponseEnvelope:
        message = response.choices[0].message
        usage = self._serialize_usage(getattr(response, "usage", None))
        assistant_message = self._serialize_assistant_message_for_history(message)
        reasoning_content = self._extract_reasoning_content(assistant_message)
        content = assistant_message.get("content")
        content = str(content) if content is not None else None
        envelope = LLMResponseEnvelope(
            assistant_message=assistant_message,
            content=content,
            reasoning_content=reasoning_content,
            usage=usage,
            raw_response_meta=self._serialize_response_meta(response),
        )
        self._last_usage = usage
        self._last_assistant_message = assistant_message
        self._last_reasoning_content = reasoning_content
        return envelope

    def _extract_reasoning_content(self, message: Any) -> Optional[str]:
        if message is None:
            return None
        value = message.get("reasoning_content") if isinstance(message, dict) else getattr(message, "reasoning_content", None)
        if value:
            return str(value)
        data = self._serialize_message(message) or {}
        value = data.get("reasoning_content")
        return str(value) if value else None

    def _to_plain_data(self, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if isinstance(value, list):
            return [self._to_plain_data(item) for item in value]
        if isinstance(value, dict):
            return {key: self._to_plain_data(item) for key, item in value.items()}
        return value

    def get_transcript_identity(self) -> str:
        return self._identity_hash({
            "base_url": self.base_url,
            "model": self.model,
            "thinking_enabled": self.thinking_enabled,
            "temperature": self.temperature,
        })

    def get_cache_debug(self) -> dict:
        return {
            "cache_affinity_enabled": self.cache_affinity_enabled,
            "cache_session_id": self.cache_session_id if self.cache_affinity_enabled else None,
            "prompt_cache_key": self._prompt_cache_key() if self.cache_affinity_enabled else None,
            "prompt_cache_key_disabled": bool(self._prompt_cache_key_disabled_reason),
            "prompt_cache_key_disabled_reason": self._prompt_cache_key_disabled_reason,
        }

    def reset_cache_session(self):
        self.cache_session_id = self._new_cache_session_id()
        self._prompt_cache_key_disabled_reason = None

    def _cache_affinity_headers(self) -> dict:
        session_id = self.cache_session_id
        return {
            "session_id": session_id,
            "x-client-request-id": session_id,
            "x-session-affinity": session_id,
        }

    def _prompt_cache_key(self) -> str:
        return self._identity_hash({
            "base_url": self.base_url,
            "model": self.model,
            "session_id": self.cache_session_id,
        })

    def _new_cache_session_id(self) -> str:
        return f"chat_{uuid.uuid4().hex}"

    def _identity_hash(self, payload: dict) -> str:
        serialized = json_dumps_stable(payload)
        return "pc_" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:48]

    def _should_retry_without_prompt_cache_key(self, exc: Exception) -> bool:
        message = str(exc).lower()
        cache_markers = (
            "prompt_cache_key",
            "prompt cache key",
            "prompt_cache_retention",
            "unknown parameter",
            "extra_forbidden",
            "unexpected keyword",
        )
        return any(marker in message for marker in cache_markers)

    def update_config(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        temperature: Optional[float] = None,
    ):
        if api_key is not None:
            self.api_key = api_key
        if base_url is not None:
            self.base_url = base_url
        if model is not None:
            self.model = model
        if thinking_enabled is not None:
            self.thinking_enabled = thinking_enabled
        if temperature is not None:
            self.temperature = self._normalize_temperature(temperature)
        # Reset client to use new config
        self._client = None
        self.reset_cache_session()

    def _normalize_temperature(self, value: Any) -> float:
        try:
            temperature = float(value)
        except (TypeError, ValueError):
            return DEFAULT_TEMPERATURE
        if temperature != temperature:
            return DEFAULT_TEMPERATURE
        return max(0.0, min(2.0, temperature))


def json_dumps_stable(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
