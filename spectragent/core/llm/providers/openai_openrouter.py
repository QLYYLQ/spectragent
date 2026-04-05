"""ChatOpenRouter — OpenRouter provider via OpenAI-compatible base."""

# NOTE: 不使用 from __future__ import annotations，为适配 conscribe 0.5
# 的 nested config 类型提取机制（详见 openai_base.py 注释）。

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.providers.openai_base import OpenAICompatBase
from spectragent.core.llm.views import ChatInvokeCompletion

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatOpenRouter(OpenAICompatBase):
    """OpenRouter provider — adds HTTP-Referer and extra_body support."""

    __registry_key__ = "openai.openrouter"

    # --- OpenRouter-specific ---
    http_referer: Annotated[str | None, Field(description="HTTP referer")] = None
    extra_body: Annotated[dict | None, Field(description="Extra body params")] = None

    @property
    def provider(self) -> str:
        return "openrouter"

    def get_client(self) -> AsyncOpenAI:
        effective_base_url = self.base_url or "https://openrouter.ai/api/v1"
        params: dict[str, Any] = {
            "api_key": self.api_key, "max_retries": self.max_retries,
            "base_url": effective_base_url,
        }
        return AsyncOpenAI(**params)

    @overload
    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: type[T], **kwargs: Any
    ) -> ChatInvokeCompletion[T]: ...

    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: type[T] | None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        extra_kwargs: dict[str, Any] = {}
        if self.http_referer:
            extra_kwargs["extra_headers"] = {"HTTP-Referer": self.http_referer}
        if self.extra_body:
            extra_kwargs.update(self.extra_body)
        return await self._standard_ainvoke(messages, output_format, extra_kwargs)
