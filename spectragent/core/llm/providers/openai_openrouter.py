"""ChatOpenRouter — OpenRouter provider via OpenAI-compatible base."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.providers.openai_base import OpenAICompatBase
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatOpenRouter(OpenAICompatBase):
    """OpenRouter provider — adds HTTP-Referer and extra_body support."""

    __registry_key__ = "openai.openrouter"

    # --- OpenRouter-specific ---
    http_referer: Annotated[
        str | None, Field(description="HTTP referer. None (the default): no HTTP-Referer header.")
    ] = None
    extra_body: Annotated[
        dict | None, Field(description="Extra request body fields. None (the default): none.")
    ] = None

    @property
    def provider(self) -> str:
        return "openrouter"

    def get_client(self) -> AsyncOpenAI:
        effective_base_url = self.base_url or "https://openrouter.ai/api/v1"
        params: dict[str, Any] = {
            "api_key": self.api_key,
            "max_retries": self.max_retries,
            "base_url": effective_base_url,
        }
        if self.http_transport is not None:
            params["http_client"] = self.http_transport
        return AsyncOpenAI(**params)

    @overload
    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: None = None,
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: Any,
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T]: ...

    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T] | None = None,
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        extra_kwargs: dict[str, Any] = {}
        if self.http_referer:
            extra_kwargs["extra_headers"] = {"HTTP-Referer": self.http_referer}
        if self.extra_body:
            # `extra_body` is an OpenAI-SDK transport parameter: its contents are
            # merged into the request JSON body. Splatting it as top-level kwargs
            # instead makes the SDK reject every unknown key with a TypeError.
            extra_kwargs["extra_body"] = dict(self.extra_body)
        return await self.standard_ainvoke(
            messages, output_format, extra_kwargs, tools=tools, tool_choice=tool_choice
        )
