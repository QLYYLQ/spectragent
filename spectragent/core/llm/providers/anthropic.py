"""ChatAnthropic — direct Anthropic API provider."""

from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic, NotGiven

from spectragent.core.llm.providers.anthropic_base import AnthropicCompatBase


@dataclass
class ChatAnthropic(AnthropicCompatBase):
    """Direct Anthropic API provider."""

    __registry_key__ = "anthropic.direct"

    @property
    def provider(self) -> str:
        return "direct"

    def get_client(self) -> AsyncAnthropic:
        base_params = {
            "api_key": self.api_key,
            "auth_token": self.auth_token,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "default_headers": self.default_headers,
            "default_query": self.default_query,
            "http_client": self.http_client,
        }
        params: dict[str, Any] = {}
        for k, v in base_params.items():
            if v is not None and v is not NotGiven():
                params[k] = v
        return AsyncAnthropic(**params)

    # ainvoke inherited from AnthropicCompatBase
