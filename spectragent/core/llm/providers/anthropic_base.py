"""Shared base for Anthropic providers (direct API + AWS Bedrock via Anthropic SDK)."""

# NOTE: 不使用 from __future__ import annotations —— conscribe 0.5 的
# _extract_params_by_level 需要通过 get_type_hints() 拿到真实的 Annotated
# 类型对象来做 __config_annotated_only__ 过滤；future annotations 会导致
# 跨模块类型解析失败，使 get_origin() 返回 None。

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

import httpx
from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    AsyncAnthropicBedrock,
    NotGiven,
    RateLimitError,
    omit,
)
from anthropic.types import CacheControlEphemeralParam, Message, ToolParam
from anthropic.types.text_block import TextBlock
from anthropic.types.tool_choice_tool_param import ToolChoiceToolParam
from httpx import Timeout
from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.anthropic_format import AnthropicMessageSerializer
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage

T = TypeVar("T", bound=BaseModel)


@dataclass
class AnthropicCompatBase(ChatModelBase):
    """Abstract base for Anthropic providers."""

    __abstract__ = True
    __registry_key__ = "anthropic"

    # --- Model ---
    model: Annotated[str, Field(description="Anthropic model name")]

    # --- Generation params ---
    max_tokens: Annotated[int, Field(description="Maximum output tokens")] = 8192
    temperature: Annotated[float | None, Field(description="Sampling temperature")] = None
    top_p: Annotated[float | None, Field(description="Nucleus sampling top-p")] = None
    top_k: Annotated[int | None, Field(description="Top-k sampling parameter")] = None
    seed: Annotated[int | None, Field(description="Random seed for determinism")] = None
    stop_sequences: Annotated[list[str] | None, Field(description="Stop sequences")] = None

    # --- Structured output ---
    # Default = native output_config.format (constrained decoding): ~40-60% fewer
    # tokens than forced tool-use and returns JSON directly. Set False to fall back
    # to tool-use for endpoints/proxies that don't forward the output_config param.
    use_native_structured_output: Annotated[
        bool,
        Field(description="Use native output_config.format structured outputs instead of forced tool-use"),
    ] = True
    structured_outputs_beta: Annotated[
        str | None,
        Field(description="anthropic-beta header enabling native structured outputs (None/'' to omit)"),
    ] = "structured-outputs-2025-12-15"

    # --- Client params ---
    api_key: Annotated[str | None, Field(description="Anthropic API key")] = None
    auth_token: Annotated[str | None, Field(description="Anthropic auth token")] = None
    base_url: Annotated[str | None, Field(description="Custom API base URL")] = None
    max_retries: Annotated[int, Field(description="Max API retry attempts")] = 10

    # --- Non-config internal fields ---
    timeout: float | Timeout | None | NotGiven = NotGiven()
    default_headers: Mapping[str, str] | None = None
    default_query: Mapping[str, object] | None = None
    http_client: httpx.AsyncClient | None = None

    @property
    def model_type(self) -> str:
        return "anthropic"

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def provider(self) -> str:
        return ""

    def _get_invoke_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.max_tokens is not None:
            params["max_tokens"] = self.max_tokens
        if self.top_p is not None:
            params["top_p"] = self.top_p
        if self.top_k is not None:
            params["top_k"] = self.top_k
        if self.seed is not None:
            params["seed"] = self.seed
        if self.stop_sequences is not None:
            params["stop_sequences"] = self.stop_sequences
        return params

    def get_client(self) -> AsyncAnthropic | AsyncAnthropicBedrock:
        """Override in sub-classes."""
        raise NotImplementedError

    def _get_usage(self, response: Message) -> ChatInvokeUsage:
        return ChatInvokeUsage(
            prompt_tokens=response.usage.input_tokens + (response.usage.cache_read_input_tokens or 0),
            completion_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            prompt_cached_tokens=response.usage.cache_read_input_tokens,
            prompt_cache_creation_tokens=response.usage.cache_creation_input_tokens,
            prompt_image_tokens=None,
        )

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
        anthropic_messages, system_prompt = AnthropicMessageSerializer.serialize_messages(messages)

        try:
            if output_format is None:
                response = await self.get_client().messages.create(
                    model=self.model,
                    messages=anthropic_messages,
                    system=system_prompt or omit,
                    **self._get_invoke_params(),
                )

                if not isinstance(response, Message):
                    raise ModelProviderError(
                        message=f"Unexpected response type: {type(response).__name__}",
                        status_code=502, model=self.name,
                    )

                usage = self._get_usage(response)
                first_content = response.content[0]
                text = first_content.text if isinstance(first_content, TextBlock) else str(first_content)
                return ChatInvokeCompletion(completion=text, usage=usage, stop_reason=response.stop_reason)

            elif self.use_native_structured_output:
                # Native structured outputs via output_config.format (constrained
                # decoding) — returns JSON directly in a text block, no tool round-trip.
                schema = SchemaOptimizer.create_optimized_json_schema(output_format)
                schema.pop("title", None)

                create_kwargs: dict[str, Any] = dict(
                    model=self.model,
                    messages=anthropic_messages,
                    system=system_prompt or omit,
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                    **self._get_invoke_params(),
                )
                if self.structured_outputs_beta:
                    create_kwargs["extra_headers"] = {"anthropic-beta": self.structured_outputs_beta}

                response = await self.get_client().messages.create(**create_kwargs)

                if not isinstance(response, Message):
                    raise ModelProviderError(
                        message=f"Unexpected response type: {type(response).__name__}",
                        status_code=502, model=self.name,
                    )

                usage = self._get_usage(response)
                first_content = response.content[0] if response.content else None
                text = first_content.text if isinstance(first_content, TextBlock) else str(first_content)
                t = text.strip()
                if t.startswith("```json") and t.endswith("```"):
                    t = t[7:-3].strip()
                elif t.startswith("```") and t.endswith("```"):
                    t = t[3:-3].strip()
                return ChatInvokeCompletion(
                    completion=output_format.model_validate_json(t),
                    usage=usage,
                    stop_reason=response.stop_reason,
                )

            else:
                tool_name = output_format.__name__
                schema = SchemaOptimizer.create_optimized_json_schema(output_format)
                schema.pop("title", None)

                tool = ToolParam(
                    name=tool_name,
                    description=f"Extract information in the format of {tool_name}",
                    input_schema=schema,
                    cache_control=CacheControlEphemeralParam(type="ephemeral"),
                )
                tool_choice = ToolChoiceToolParam(type="tool", name=tool_name)

                response = await self.get_client().messages.create(
                    model=self.model,
                    messages=anthropic_messages,
                    tools=[tool],
                    system=system_prompt or omit,
                    tool_choice=tool_choice,
                    **self._get_invoke_params(),
                )

                if not isinstance(response, Message):
                    raise ModelProviderError(
                        message=f"Unexpected response type: {type(response).__name__}",
                        status_code=502, model=self.name,
                    )

                usage = self._get_usage(response)
                for block in response.content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        try:
                            return ChatInvokeCompletion(
                                completion=output_format.model_validate(block.input),
                                usage=usage, stop_reason=response.stop_reason,
                            )
                        except Exception as e:
                            if isinstance(block.input, str):
                                data = json.loads(block.input)
                                return ChatInvokeCompletion(
                                    completion=output_format.model_validate(data),
                                    usage=usage, stop_reason=response.stop_reason,
                                )
                            raise e
                raise ValueError("Expected tool use in response but none found")

        except APIConnectionError as e:
            raise ModelProviderError(message=e.message, model=self.name) from e
        except RateLimitError as e:
            raise ModelRateLimitError(message=e.message, model=self.name) from e
        except APIStatusError as e:
            raise ModelProviderError(message=e.message, status_code=e.status_code, model=self.name) from e
        except Exception as e:
            raise ModelProviderError(message=str(e), model=self.name) from e
