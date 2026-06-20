import os
from typing import Any, List, Dict, Optional, AsyncGenerator
from openai import AsyncOpenAI


class LLMClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        thinking_enabled: bool = False,
    ):
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.base_url = base_url or os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
        self.model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        self.thinking_enabled = thinking_enabled
        self._client: Optional[AsyncOpenAI] = None
        self._last_usage: Optional[dict] = None

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
            kwargs["temperature"] = temperature
        return kwargs

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
    ) -> str:
        client = self._get_client()
        response = await client.chat.completions.create(
            **self._build_kwargs(messages, temperature, max_tokens, stream)
        )

        if stream:
            self._last_usage = None
            content = ""
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    content += chunk.choices[0].delta.content
            return content

        self._last_usage = self._serialize_usage(getattr(response, "usage", None))
        return response.choices[0].message.content or ""

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        client = self._get_client()
        response = await client.chat.completions.create(
            **self._build_kwargs(messages, temperature, max_tokens, True)
        )

        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
        self._last_usage = None

    def get_last_usage(self) -> Optional[dict]:
        return self._last_usage

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
            "input_tokens",
            "output_tokens",
            "input_token_details",
        ):
            if hasattr(usage, key):
                value = getattr(usage, key)
                if hasattr(value, "model_dump"):
                    value = value.model_dump()
                result[key] = value
        return result or None

    def update_config(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
    ):
        if api_key is not None:
            self.api_key = api_key
        if base_url is not None:
            self.base_url = base_url
        if model is not None:
            self.model = model
        if thinking_enabled is not None:
            self.thinking_enabled = thinking_enabled
        # Reset client to use new config
        self._client = None
