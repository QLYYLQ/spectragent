"""ChatVercel — Vercel AI Gateway provider via OpenAI-compatible base."""

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
class ChatVercel(OpenAICompatBase):
    """Vercel AI Gateway provider — the schema goes in the prompt for some models."""

    __registry_key__ = "openai.vercel"

    # --- Vercel-specific ---
    provider_options: Annotated[
        dict[str, Any] | None,
        Field(
            description="Provider options, sent as providerOptions. None (the default): not sent."
        ),
    ] = None

    @property
    def provider(self) -> str:
        return "vercel"

    def get_client(self) -> AsyncOpenAI:
        effective_base_url = self.base_url or "https://ai-gateway.vercel.sh/v1"
        params: dict[str, Any] = {
            "api_key": self.api_key,
            "max_retries": self.max_retries,
            "base_url": effective_base_url,
        }
        if self.http_transport is not None:
            params["http_client"] = self.http_transport
        return AsyncOpenAI(**params)

    def takes_schema_in_prompt(self) -> bool:
        """The family's answer, plus two Vercel triggers of its own.

        A Gemini model (its JSON-Schema dialect differs) and a model on the
        provider's built-in reasoning list also take the schema in the prompt
        behind this gateway. Both are this provider's long-standing routing,
        kept as they were; neither was measured.
        """
        return (
            super().takes_schema_in_prompt()
            or self.model_family.schema_requires_gemini_fix
            or self.is_listed_reasoning_model()
        )

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
        if self.provider_options:
            extra_kwargs["extra_body"] = {"providerOptions": self.provider_options}
        return await self.standard_ainvoke(
            messages, output_format, extra_kwargs, tools=tools, tool_choice=tool_choice
        )
