import hashlib
import uuid
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from .client import LLMClient, json_dumps_stable


@dataclass(frozen=True)
class ProviderRuntimeConfig:
    api_key: str
    base_url: str
    model: str
    thinking_enabled: bool
    temperature: float
    transient_retry_delays: tuple[float, ...]
    mimo_web_search_mode: str

    @classmethod
    def from_llm_client(cls, client: LLMClient) -> "ProviderRuntimeConfig":
        return cls(
            api_key=client.api_key,
            base_url=client.base_url,
            model=client.model,
            thinking_enabled=client.thinking_enabled,
            temperature=client.temperature,
            transient_retry_delays=tuple(client.transient_retry_delays),
            mimo_web_search_mode=client.mimo_web_search_mode,
        )

    def identity_payload(self) -> dict:
        return {
            "api_key_hash": self._secret_hash(self.api_key),
            "base_url": self.base_url,
            "model": self.model,
            "thinking_enabled": self.thinking_enabled,
            "temperature": self.temperature,
            "transient_retry_delays": list(self.transient_retry_delays),
            "mimo_web_search_mode": self.mimo_web_search_mode,
        }

    def identity_hash(self) -> str:
        raw = json_dumps_stable(self.identity_payload())
        return "pr_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _secret_hash(self, value: str) -> Optional[str]:
        if not value:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class ProviderRuntime:
    """Provider transport/runtime factory shared by scoped LLM clients."""

    def __init__(self, config: ProviderRuntimeConfig):
        self.config = config
        self.identity_hash = config.identity_hash()
        self.runtime_id = f"rt_{self.identity_hash[3:15]}_{uuid.uuid4().hex[:8]}"
        self._client: Optional[AsyncOpenAI] = None

    @classmethod
    def from_llm_client(cls, client: LLMClient) -> "ProviderRuntime":
        return cls(ProviderRuntimeConfig.from_llm_client(client))

    def get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
            )
        return self._client

    def reset_client(self):
        self._client = None

    def make_client(
        self,
        *,
        scope: str,
        cache_affinity_enabled: bool,
        cache_session_id: Optional[str] = None,
        provider_user_id: Optional[str] = None,
    ) -> LLMClient:
        return LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            model=self.config.model,
            thinking_enabled=self.config.thinking_enabled,
            temperature=self.config.temperature,
            cache_affinity_enabled=cache_affinity_enabled,
            cache_session_id=cache_session_id,
            provider_user_id=provider_user_id,
            transient_retry_delays=self.config.transient_retry_delays,
            runtime_id=self.runtime_id,
            client_scope=scope,
            provider_config_hash=self.identity_hash,
            mimo_web_search_mode=self.config.mimo_web_search_mode,
            shared_client_getter=self.get_client,
            shared_client_reset=self.reset_client,
        )
