"""ChatGroq — Groq provider via OpenAI-compatible base."""

# NOTE: 不使用 from __future__ import annotations，为适配 conscribe 0.5
# 的 nested config 类型提取机制（详见 openai_base.py 注释）。

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, overload

from pydantic import BaseModel

from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.providers.openai_base import OpenAICompatBase
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatGroq(OpenAICompatBase):
    """Groq provider — uses the Groq Python SDK client."""

    __registry_key__ = "openai.groq"

    @property
    def provider(self) -> str:
        return "groq"

    def get_client(self):
        from groq import AsyncGroq
        return AsyncGroq(
            api_key=self.api_key, base_url=self.base_url,
            timeout=self.timeout, max_retries=self.max_retries,
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
        from groq import APIError
        from groq import APIStatusError as GroqStatusError
        from groq import RateLimitError as GroqRateLimitError
        from groq.types.chat.completion_create_params import (
            ResponseFormatResponseFormatJsonSchema,
            ResponseFormatResponseFormatJsonSchemaJsonSchema,
        )

        if output_format is not None:
            # Groq documents "Streaming and tool use are not currently supported
            # with Structured Outputs", so sending both bought a round trip and
            # a vendor 400. Refuse before the request — and outside the `try`,
            # which would otherwise relabel this as a generic provider error.
            self.reject_unsupported_tools(
                tools,
                reason=(
                    "Groq does not support tool use together with Structured "
                    "Outputs (response_format json_schema). Pass tools without "
                    "output_format."
                ),
            )

        groq_messages = OpenAIMessageSerializer.serialize_messages(messages)
        client = self.get_client()

        try:
            if output_format is None:
                resp = await client.chat.completions.create(
                    messages=groq_messages, model=self.model,
                    service_tier=self.service_tier, temperature=self.temperature,
                    top_p=self.top_p, seed=self.seed,
                    **self._tool_kwargs(tools, tool_choice),
                )
                usage = self._get_usage(resp) if hasattr(resp, 'usage') else None
                return ChatInvokeCompletion(
                    completion=resp.choices[0].message.content or "",
                    usage=usage,
                    tool_calls=OpenAIMessageSerializer.parse_tool_calls(
                        getattr(resp.choices[0].message, "tool_calls", None)
                    ),
                )
            else:
                schema = SchemaOptimizer.create_optimized_json_schema(
                    output_format, strict=self._use_strict_schema(output_format)
                )
                resp = await client.chat.completions.create(
                    model=self.model, messages=groq_messages,
                    temperature=self.temperature, top_p=self.top_p, seed=self.seed,
                    response_format=ResponseFormatResponseFormatJsonSchema(
                        json_schema=ResponseFormatResponseFormatJsonSchemaJsonSchema(
                            name=output_format.__name__, description="Model output schema", schema=schema,
                        ),
                        type="json_schema",
                    ),
                    service_tier=self.service_tier,
                )
                if not resp.choices[0].message.content:
                    raise ModelProviderError(message="No content in response", status_code=500, model=self.name)
                parsed = output_format.model_validate_json(resp.choices[0].message.content)
                usage = self._get_usage(resp) if hasattr(resp, 'usage') else None
                # No `tool_calls=`: tools are refused on this path above.
                return ChatInvokeCompletion(completion=parsed, usage=usage)

        except GroqRateLimitError as e:
            raise ModelRateLimitError(message=e.response.text, model=self.name) from e
        except GroqStatusError as e:
            if output_format is not None:
                try:
                    from spectragent.core.llm.providers._groq_parser import (
                        try_parse_groq_failed_generation,
                    )
                    parsed = try_parse_groq_failed_generation(e, output_format)
                    return ChatInvokeCompletion(completion=parsed, usage=None)
                except Exception:
                    pass
            raise ModelProviderError(message=str(e), status_code=e.response.status_code, model=self.name) from e
        except APIError as e:
            raise ModelProviderError(message=e.message, model=self.name) from e
        except Exception as e:
            raise ModelProviderError(message=str(e), model=self.name) from e
