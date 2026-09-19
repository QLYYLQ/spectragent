"""Shared base for Anthropic providers (direct API + AWS Bedrock via Anthropic SDK)."""

# NOTE: 不使用 from __future__ import annotations —— conscribe 0.5 的
# _extract_params_by_level 需要通过 get_type_hints() 拿到真实的 Annotated
# 类型对象来做 __config_annotated_only__ 过滤；future annotations 会导致
# 跨模块类型解析失败，使 get_origin() 返回 None。

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, TypeVar, overload

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
from spectragent.core.llm.fences import strip_code_fence
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.anthropic_format import AnthropicMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
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
    seed: Annotated[
        int | None,
        Field(
            description=(
                "UNSUPPORTED by the Anthropic Messages API — must be left as None. "
                "Setting it raises at construction rather than silently giving you "
                "non-reproducible runs."
            )
        ),
    ] = None
    stop_sequences: Annotated[list[str] | None, Field(description="Stop sequences")] = None

    # --- Structured output ---
    # Default = native output_config.format (constrained decoding): ~40-60% fewer
    # tokens than forced tool-use and returns JSON directly. Set False to fall back
    # to tool-use for endpoints/proxies that don't forward the output_config param.
    use_native_structured_output: Annotated[
        bool,
        Field(
            description="Use native output_config.format structured outputs instead of forced tool-use"
        ),
    ] = True
    structured_outputs_beta: Annotated[
        str | None,
        Field(
            description="anthropic-beta header enabling native structured outputs (None/'' to omit)"
        ),
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

    def __post_init__(self) -> None:
        # `AsyncMessages.create` has no `seed` parameter and no `**kwargs`, so
        # forwarding it raised TypeError on *every* call — which the catch-all
        # below then repackaged as an opaque ModelProviderError. Anthropic has no
        # determinism seed at all, so silently dropping it is worse than failing:
        # it would hand a reproducibility researcher a false guarantee.
        if self.seed is not None:
            raise ValueError(
                f"The Anthropic Messages API has no `seed` parameter, so "
                f"seed={self.seed!r} cannot be honoured. Remove `seed` from this "
                f"LLM's config (model={self.model!r})."
            )

    @property
    def model_type(self) -> str:
        return "anthropic"

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def provider(self) -> str:
        return ""

    # ``model_family`` and ``_use_strict_schema`` come from ``ChatModelBase``.

    #: ``tool_choice`` spelled the Anthropic way.
    _TOOL_CHOICE: ClassVar[dict[str, dict[str, str]]] = {
        "auto": {"type": "auto"},
        "required": {"type": "any"},
        "none": {"type": "none"},
    }

    def _tool_kwargs(
        self, tools: Sequence[ToolDefinition] | None, tool_choice: ToolChoice
    ) -> dict[str, Any]:
        """Request kwargs advertising *tools*, or empty when there are none."""
        if not tools:
            return {}
        return {
            "tools": AnthropicMessageSerializer.serialize_tools(tools),
            "tool_choice": self._TOOL_CHOICE[tool_choice],
        }

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
        if self.stop_sequences is not None:
            params["stop_sequences"] = self.stop_sequences
        return params

    def get_client(self) -> AsyncAnthropic | AsyncAnthropicBedrock:
        """Override in sub-classes."""
        raise NotImplementedError

    def _get_usage(self, response: Message) -> ChatInvokeUsage:
        """Anthropic's three input counters, folded into one prompt total.

        ``input_tokens`` here is the *uncached remainder only* — it excludes
        both ``cache_read_input_tokens`` and ``cache_creation_input_tokens``,
        so the prompt actually processed is the sum of all three. Reading any
        one of them as "the input" under-reports the call.

        This used to add only the cache read, which dropped every cache write
        on the floor and left ``total_tokens`` (``input + output``) *smaller
        than* ``prompt_tokens`` on any cached step — the run looked cheaper the
        better the cache worked.
        """
        cache_read = response.usage.cache_read_input_tokens or 0
        cache_write = response.usage.cache_creation_input_tokens or 0
        prompt_tokens = response.usage.input_tokens + cache_read + cache_write
        return ChatInvokeUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=response.usage.output_tokens,
            total_tokens=prompt_tokens + response.usage.output_tokens,
            prompt_cached_tokens=response.usage.cache_read_input_tokens,
            prompt_cache_creation_tokens=response.usage.cache_creation_input_tokens,
            prompt_image_tokens=None,
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
        anthropic_messages, system_prompt = AnthropicMessageSerializer.serialize_messages(messages)

        # Checked before the try: this is a caller contract error, and the
        # catch-all below would otherwise launder it into an opaque
        # ModelProviderError that points at the transport rather than the call.
        if tools and output_format is not None:
            self.reject_unsupported_tools(
                tools,
                reason=(
                    "output_config.format constrains the whole response to the "
                    "output schema, leaving the model no way to emit a tool_use "
                    "block. Pass tools without output_format."
                )
                if self.use_native_structured_output
                else (
                    "its forced tool-use fallback (use_native_structured_output="
                    "False) reserves tool_choice for the output schema. Pass "
                    "tools without output_format."
                ),
            )

        try:
            if output_format is None:
                response = await self.get_client().messages.create(
                    model=self.model,
                    messages=anthropic_messages,
                    system=system_prompt or omit,
                    **self._tool_kwargs(tools, tool_choice),
                    **self._get_invoke_params(),
                )

                if not isinstance(response, Message):
                    raise ModelProviderError(
                        message=f"Unexpected response type: {type(response).__name__}",
                        status_code=502,
                        model=self.name,
                    )

                usage = self._get_usage(response)
                # With tools advertised the model may answer with tool_use blocks
                # and no text at all, so the first block is not necessarily text.
                text_blocks = [b for b in response.content if isinstance(b, TextBlock)]
                if text_blocks:
                    text = "".join(b.text for b in text_blocks)
                elif response.content:
                    text = "" if tools else str(response.content[0])
                else:
                    text = ""
                return ChatInvokeCompletion(
                    completion=text,
                    usage=usage,
                    stop_reason=response.stop_reason,
                    tool_calls=AnthropicMessageSerializer.parse_tool_calls(response.content),
                )

            elif self.use_native_structured_output:
                # Native structured outputs via output_config.format (constrained
                # decoding) — returns JSON directly in a text block, no tool round-trip.
                schema = SchemaOptimizer.create_optimized_json_schema(
                    output_format, strict=self._use_strict_schema(output_format)
                )
                schema.pop("title", None)

                create_kwargs: dict[str, Any] = dict(
                    model=self.model,
                    messages=anthropic_messages,
                    system=system_prompt or omit,
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                    **self._get_invoke_params(),
                )
                if self.structured_outputs_beta:
                    create_kwargs["extra_headers"] = {
                        "anthropic-beta": self.structured_outputs_beta
                    }

                response = await self.get_client().messages.create(**create_kwargs)

                if not isinstance(response, Message):
                    raise ModelProviderError(
                        message=f"Unexpected response type: {type(response).__name__}",
                        status_code=502,
                        model=self.name,
                    )

                usage = self._get_usage(response)
                first_content = response.content[0] if response.content else None
                text = (
                    first_content.text
                    if isinstance(first_content, TextBlock)
                    else str(first_content)
                )
                t = strip_code_fence(text)
                return ChatInvokeCompletion(
                    completion=self._validate_structured(
                        output_format, t, usage=usage, wire_text=text
                    ),
                    usage=usage,
                    stop_reason=response.stop_reason,
                )

            else:
                tool_name = output_format.__name__
                schema = SchemaOptimizer.create_optimized_json_schema(
                    output_format, strict=self._use_strict_schema(output_format)
                )
                schema.pop("title", None)

                tool = ToolParam(
                    name=tool_name,
                    description=f"Extract information in the format of {tool_name}",
                    input_schema=schema,
                    cache_control=CacheControlEphemeralParam(type="ephemeral"),
                )
                # Named `schema_tool_choice` rather than `tool_choice`: the
                # latter is now a parameter of this method, and shadowing it
                # here would read as if the caller's choice were being honoured.
                schema_tool_choice = ToolChoiceToolParam(type="tool", name=tool_name)

                response = await self.get_client().messages.create(
                    model=self.model,
                    messages=anthropic_messages,
                    tools=[tool],
                    system=system_prompt or omit,
                    tool_choice=schema_tool_choice,
                    **self._get_invoke_params(),
                )

                if not isinstance(response, Message):
                    raise ModelProviderError(
                        message=f"Unexpected response type: {type(response).__name__}",
                        status_code=502,
                        model=self.name,
                    )

                usage = self._get_usage(response)
                for block in response.content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        # ``input`` is a dict on the normal path and a JSON
                        # string when the model stuffed everything into one
                        # argument; ``_validate_structured`` takes either and
                        # raises the recoverable error for both. The retry that
                        # used to sit here caught its own typed error and
                        # re-parsed with ``json.loads``, so malformed text came
                        # back out as a bare ``JSONDecodeError`` — a reply the
                        # model could have fixed, laundered into one it cannot.
                        return ChatInvokeCompletion(
                            completion=self._validate_structured(
                                output_format, block.input, usage=usage
                            ),
                            usage=usage,
                            stop_reason=response.stop_reason,
                        )
                raise ValueError("Expected tool use in response but none found")

        except ModelProviderError:
            # Re-raised as-is, and first: ``StructuredOutputValidationError`` is
            # a ``ModelProviderError``, and the catch-all below would flatten it
            # into a generic one — dropping ``raw_output`` and ``usage`` with it.
            # The Loop reads that type to tell "the model wrote something wrong"
            # (feed it back, keep going) from "the provider failed" (abort), so
            # flattening it here ends a run over one mistyped parameter.
            raise
        except APIConnectionError as e:
            raise ModelProviderError(message=e.message, model=self.name) from e
        except RateLimitError as e:
            raise ModelRateLimitError(message=e.message, model=self.name) from e
        except APIStatusError as e:
            raise ModelProviderError(
                message=e.message, status_code=e.status_code, model=self.name
            ) from e
        except Exception as e:
            # Include the concrete exception type: a bare str(e) turned client-side
            # contract bugs (TypeError from an unsupported kwarg) into an opaque
            # "provider error" that pointed nowhere.
            raise ModelProviderError(message=f"{type(e).__name__}: {e}", model=self.name) from e
