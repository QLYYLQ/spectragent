"""Shared base for all OpenAI-compatible providers.

Provides common fields, client creation, usage extraction, and a standard
ainvoke path (text + response_format structured output).
"""

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypeVar, overload

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from openai.types.chat.chat_completion import ChatCompletion
from openai.types.responses import Response
from openai.types.shared.chat_model import ChatModel
from openai.types.shared_params.response_format_json_schema import (
    JSONSchema,
    ResponseFormatJSONSchema,
)
from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.exceptions import (
    ModelOutputTruncatedError,
    ModelProviderError,
    ModelRateLimitError,
)
from spectragent.core.llm.fences import strip_code_fence
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion, ModelInvokeUsage, join_reasoning

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

#: What a model on ``reasoning_models`` receives under ``reasoning_effort: auto``.
_LISTED_MODEL_DEFAULT_EFFORT = "low"

#: Where an OpenAI-compatible reply message carries the model's reasoning, in
#: the order they are read. Neither is in the OpenAI SDK's message model, which
#: keeps such undeclared fields on the object: ``reasoning_content`` is the
#: DeepSeek / Qwen / vLLM name, the one dmxapi.cn returns for ``qwen3.8-flash``;
#: ``reasoning`` is OpenRouter's, and Groq's, Cerebras' and Vercel's.
_REASONING_FIELDS = ("reasoning_content", "reasoning")


def resolve_http_transport(
    http_client: httpx.AsyncClient | None, client_params: Mapping[str, Any] | None
) -> httpx.AsyncClient | None:
    """Resolve Python transport injection without ignoring a conflicting owner."""
    extra_client = (client_params or {}).get("http_client")
    if extra_client is not None and not isinstance(extra_client, httpx.AsyncClient):
        raise TypeError("client_params['http_client'] must be an httpx.AsyncClient")
    if http_client is not None and extra_client is not None and extra_client is not http_client:
        raise ValueError("http_client and client_params['http_client'] name different transports")
    return http_client if http_client is not None else extra_client


def message_reasoning(message: object) -> str | None:
    """The reasoning an OpenAI-compatible reply message carried, or ``None``.

    The first of :data:`_REASONING_FIELDS` that holds text, read by
    attribute: the OpenAI SDK keeps undeclared fields on the message object,
    Groq's SDK declares ``reasoning``, and a message that has neither reads
    as ``None``. Only read — the request never asks for it.
    """
    for name in _REASONING_FIELDS:
        text = join_reasoning([getattr(message, name, None)])
        if text is not None:
            return text
    return None


#: ``reasoning_effort``'s settings: ``auto`` (the provider's policy) plus every
#: value the OpenAI SDK accepts on the wire (its ``ReasoningEffort``, which a
#: test keeps in step). Not optional: ``null`` would be a second spelling of
#: ``auto`` (``docs/architecture_design.md`` §4.2.1).
ReasoningEffortSetting = Literal["auto", "none", "minimal", "low", "medium", "high", "xhigh"]


@dataclass
class OpenAICompatBase(ChatModelBase):
    """Abstract base and author extension API for OpenAI-compatible providers.

    Override ``get_client`` and ``http_transport`` for client construction and
    ownership. Request paths use ``request_client`` to acquire an execution lease;
    providers with a different SDK ownership contract may override that method.
    ``standard_ainvoke`` exposes the shared request/error path. Custom paths reuse
    ``common_model_params``, ``tool_kwargs``, the reasoning-policy methods and
    usage extractors without reaching into the client's private resource cache.
    """

    __abstract__ = True
    __registry_key__ = "openai"

    # --- Model ---
    model: Annotated[str, Field(description="Model name")]

    # --- Generation params ---
    temperature: Annotated[float | None, Field(description="Sampling temperature")] = 0.2
    frequency_penalty: Annotated[float | None, Field(description="Frequency penalty")] = 0.3
    reasoning_effort: Annotated[
        ReasoningEffortSetting,
        Field(
            description=(
                "'auto' (the default) keeps the provider's policy: a model on "
                "the provider's built-in reasoning-model list (o-series, gpt-5*) "
                "gets 'low', every other model gets no parameter. Any other "
                "value is sent as ``reasoning_effort`` for any model, so it "
                "reaches a reasoning model that list does not know, e.g. gemini "
                "behind an OpenAI-compatible gateway; 'none' sends 'none' (no "
                "reasoning). Those values are the OpenAI SDK's; a gateway may "
                "still refuse one of them (a gemini proxy answers 'none' with "
                "HTTP 400)."
            )
        ),
    ] = "auto"
    seed: Annotated[int | None, Field(description="Random seed. None (the default): not sent.")] = (
        None
    )
    service_tier: Annotated[
        str | None, Field(description="Service tier. None (the default): not sent.")
    ] = None
    top_p: Annotated[
        float | None, Field(description="Top-p sampling. None (the default): not sent.")
    ] = None

    # --- Structured output options: none, deliberately ---
    #
    # Four per-config booleans used to live here and all four are gone:
    #
    # * ``dont_force_structured_output`` sent the request *without*
    #   ``response_format`` and hoped the model answered JSON anyway;
    # * ``add_schema_to_system_prompt`` pasted the schema into the system
    #   message in case the model ignored ``response_format``;
    # * ``remove_min_items_from_schema`` / ``remove_defaults_from_schema``
    #   let a YAML hand-edit the compiled schema's keywords.
    #
    # None of them was a property of a *model*. The first two were
    # workarounds for a gateway whose OpenAI-compatible route translates to
    # a vendor's native API and loses the schema on the way; native routing
    # is the fix (Gemini at ``model_type: google``, Claude at
    # ``model_type: anthropic``, both against the gateway root — see the
    # measured routes in ``.agents/skills/af-add-an-llm-provider/references/guide.md``
    # §1). The last two are dialect rules, and
    # ``architecture_design.md`` §4.5 says where a dialect rule goes:
    # "去除写在 optimizer 里一次，而不是每个 provider 各配一个旋钮".
    # ``strict`` implies the ``minItems`` removal inside
    # ``create_optimized_json_schema``; a *non-strict* endpoint that also
    # chokes on it says so through ``ModelFamily``, the same way
    # ``schema_rejects_closed_union_branches`` does — not through YAML.
    #
    # Nothing in the repo ever set any of the four. If you are about to add
    # the fifth, read ``how_to_extend`` in the dialect-transforms section of
    # ``core/llm/schema/optimizer.py`` first.

    # --- Client params ---
    api_key: Annotated[
        str | None,
        Field(
            description=(
                "API key. None (the default): the client library's own environment "
                "variable (OPENAI_API_KEY for the OpenAI client, GROQ_API_KEY for Groq's); "
                "the azure provider first tries AZURE_OPENAI_KEY, then AZURE_OPENAI_API_KEY."
            )
        ),
    ] = None
    base_url: Annotated[
        str | None,
        Field(
            description="Custom API base URL. None (the default): the provider's standard endpoint."
        ),
    ] = None
    timeout: Annotated[
        float | None,
        Field(
            description=(
                "Request timeout in seconds, read by the direct and groq providers only "
                "(the others do not pass it to their client). None (the default): the "
                "direct client's own 600 s; Groq's client is handed None, which waits "
                "without a limit."
            )
        ),
    ] = None
    max_retries: Annotated[int, Field(description="Max retry attempts")] = 5
    max_completion_tokens: Annotated[int | None, Field(description="Max completion tokens")] = 4096

    # --- Non-config internal fields (no Annotated[..., Field()] → excluded from schema) ---
    websocket_base_url: str | httpx.URL | None = None
    default_headers: Mapping[str, str] | None = None
    default_query: Mapping[str, object] | None = None
    http_client: httpx.AsyncClient | None = None
    reasoning_models: list[ChatModel | str] | None = field(
        default_factory=lambda: [
            "o4-mini",
            "o3",
            "o3-mini",
            "o1",
            "o1-pro",
            "o3-pro",
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-nano",
        ]
    )

    @property
    def model_type(self) -> str:
        return "openai"

    @property
    def name(self) -> str:
        return str(self.model)

    @property
    def provider(self) -> str:
        """Override in sub-classes to return the sub-provider name."""
        return ""

    # ``model_family`` and ``use_strict_schema`` are inherited from
    # ``ChatModelBase`` — the declarative capability table plus a compiled-schema
    # check, rather than an inline substring test on the model name.

    def tool_kwargs(
        self, tools: Sequence[ToolDefinition] | None, tool_choice: ToolChoice
    ) -> dict[str, Any]:
        """Request kwargs advertising *tools*, or empty when there are none."""
        if not tools:
            return {}
        return {
            "tools": OpenAIMessageSerializer.serialize_tools(tools),
            "tool_choice": tool_choice,
        }

    # ------------------------------------------------------------------
    # Client creation — default AsyncOpenAI, override for custom clients
    # ------------------------------------------------------------------
    def get_client(self) -> AsyncOpenAI:
        """Construct an SDK wrapper; ``request_client`` owns its reuse and cleanup."""
        params: dict[str, Any] = {"api_key": self.api_key, "max_retries": self.max_retries}
        if self.base_url:
            params["base_url"] = self.base_url
        if self.websocket_base_url:
            params["websocket_base_url"] = self.websocket_base_url
        if self.timeout:
            params["timeout"] = self.timeout
        if self.default_headers:
            params["default_headers"] = self.default_headers
        if self.default_query:
            params["default_query"] = self.default_query
        if self.http_transport is not None:
            params["http_client"] = self.http_transport
        return AsyncOpenAI(**params)

    @property
    def http_transport(self) -> httpx.AsyncClient | None:
        """The actual borrowed transport passed to this provider's SDK."""
        return self.http_client

    @asynccontextmanager
    async def request_client(self) -> AsyncIterator[AsyncOpenAI]:
        """Reuse this execution's SDK, protecting active calls from cleanup.

        OpenAI's public ``close`` closes its HTTP transport. A wrapper built
        around a borrowed transport therefore has no independent close action.
        Every constructor in this family passes ``http_transport`` unchanged.
        """
        async with self.client_resources.use():
            client = self.client_resources.get_or_create(
                "sdk",
                self.get_client,
                close=None if self.http_transport is not None else lambda sdk: sdk.close(),
            )
            yield client

    # ------------------------------------------------------------------
    # Usage extraction
    # ------------------------------------------------------------------
    def get_usage(self, response: ChatCompletion) -> ModelInvokeUsage | None:
        """Normalize chat-completions usage for compatible provider request paths."""
        if response.usage is None:
            return None
        return ModelInvokeUsage(
            prompt_tokens=response.usage.prompt_tokens,
            prompt_cached_tokens=(
                response.usage.prompt_tokens_details.cached_tokens
                if response.usage.prompt_tokens_details
                else None
            ),
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=None,
            completion_tokens=response.usage.completion_tokens,
            completion_reasoning_tokens=(
                response.usage.completion_tokens_details.reasoning_tokens
                if response.usage.completion_tokens_details
                else None
            ),
            total_tokens=response.usage.total_tokens,
        )

    def get_usage_from_responses(self, response: Response) -> ModelInvokeUsage | None:
        """Normalize Responses API usage for compatible provider request paths."""
        if response.usage is None:
            return None
        cached = None
        if response.usage.input_tokens_details is not None:
            cached = getattr(response.usage.input_tokens_details, "cached_tokens", None)
        reasoning = getattr(
            getattr(response.usage, "output_tokens_details", None), "reasoning_tokens", None
        )
        return ModelInvokeUsage(
            prompt_tokens=response.usage.input_tokens,
            prompt_cached_tokens=cached,
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=None,
            completion_tokens=response.usage.output_tokens,
            completion_reasoning_tokens=reasoning,
            total_tokens=response.usage.total_tokens,
        )

    # ------------------------------------------------------------------
    # Model params helper
    # ------------------------------------------------------------------
    def common_model_params(self) -> dict[str, Any]:
        """Build a fresh request-parameter mapping from this provider's settings."""
        params: dict[str, Any] = {}
        if self.temperature is not None:
            params["temperature"] = self.temperature
        if self.frequency_penalty is not None:
            params["frequency_penalty"] = self.frequency_penalty
        if self.max_completion_tokens is not None:
            params["max_completion_tokens"] = self.max_completion_tokens
        if self.top_p is not None:
            params["top_p"] = self.top_p
        if self.seed is not None:
            params["seed"] = self.seed
        if self.service_tier is not None:
            params["service_tier"] = self.service_tier

        effort = self.effective_reasoning_effort()
        if effort is not None:
            params["reasoning_effort"] = effort
        # OpenAI's own reasoning models reject sampling knobs. That is a fact
        # about those models, not about the parameter: a gateway serving
        # gemini takes ``temperature`` and ``reasoning_effort`` together.
        if self.is_listed_reasoning_model():
            params.pop("temperature", None)
            params.pop("frequency_penalty", None)

        return params

    def is_listed_reasoning_model(self) -> bool:
        """Whether ``model`` matches the provider's built-in reasoning list."""
        return bool(self.reasoning_models) and any(
            str(m).lower() in str(self.model).lower() for m in self.reasoning_models or ()
        )

    def effective_reasoning_effort(self) -> str | None:
        """The effort to put on the wire, or ``None`` to send nothing.

        Anything but ``auto`` is sent as written. Under ``auto`` a listed
        reasoning model gets :data:`_LISTED_MODEL_DEFAULT_EFFORT` and every
        other model gets nothing — what configs that never named the knob
        have always sent.
        """
        if self.reasoning_effort != "auto":
            return self.reasoning_effort
        if self.is_listed_reasoning_model():
            return _LISTED_MODEL_DEFAULT_EFFORT
        return None

    # ------------------------------------------------------------------
    # Standard ainvoke — text + response_format structured output
    # ------------------------------------------------------------------
    async def standard_ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T] | None = None,
        extra_kwargs: dict[str, Any] | None = None,
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        """Standard OpenAI-compatible ainvoke with error handling."""
        if output_format is not None:
            # OpenAI accepts `tools` alongside `response_format` on the wire, but
            # the two outcomes it then permits — structured JSON, or a tool call
            # with `content: null` — cannot both be returned from here:
            # `ChatInvokeCompletion.completion` is non-optional, so there is no
            # value that means "called a tool instead of answering". Forwarding
            # them anyway spent a round trip and then turned the tool-call reply
            # into a 500-shaped parse failure with the calls discarded, which is
            # strictly worse than refusing up front.
            self.reject_unsupported_tools(
                tools,
                reason=(
                    "structured output and tools cannot be combined: a tool-call "
                    "reply carries no content, and ChatInvokeCompletion.completion "
                    "has no value that can represent it. Pass tools without "
                    "output_format."
                ),
            )
        openai_messages = OpenAIMessageSerializer.serialize_messages(messages)
        try:
            model_params = self.common_model_params()
            extra = {**(extra_kwargs or {}), **self.tool_kwargs(tools, tool_choice)}

            if output_format is None:
                async with self.request_client() as client:
                    response = await client.chat.completions.create(
                        model=self.model,
                        messages=openai_messages,
                        **model_params,
                        **extra,
                    )
                choice = response.choices[0] if response.choices else None
                if choice is None:
                    raise ModelProviderError(
                        message="Invalid response: missing or empty `choices`.",
                        status_code=502,
                        model=self.name,
                    )
                if choice.finish_reason == "length":
                    raise ModelOutputTruncatedError(
                        model=self.name,
                        stop_reason=choice.finish_reason,
                        ceiling_field="max_completion_tokens",
                        ceiling=self.max_completion_tokens,
                        usage=self.get_usage(response),
                        partial_output=choice.message.content,
                    )
                return ChatInvokeCompletion(
                    completion=choice.message.content or "",
                    thinking=message_reasoning(choice.message),
                    usage=self.get_usage(response),
                    stop_reason=choice.finish_reason,
                    tool_calls=OpenAIMessageSerializer.parse_tool_calls(
                        getattr(choice.message, "tool_calls", None)
                    ),
                )
            else:
                return await self._structured_response_format(
                    openai_messages,
                    output_format,
                    model_params,
                    extra,
                )

        except ModelProviderError:
            raise
        except RateLimitError as e:
            raise ModelRateLimitError(message=e.message, model=self.name) from e
        except APIConnectionError as e:
            raise ModelProviderError(message=str(e), model=self.name) from e
        except APIStatusError as e:
            raise ModelProviderError(
                message=e.message, status_code=e.status_code, model=self.name
            ) from e
        except Exception as e:
            raise ModelProviderError(message=str(e), model=self.name) from e

    # ------------------------------------------------------------------
    # Structured output: response_format (shared by multiple providers)
    # ------------------------------------------------------------------
    async def _structured_response_format(
        self,
        openai_messages,
        output_format: type[T],
        model_params: dict,
        extra_kwargs: dict,
    ) -> ChatInvokeCompletion[T]:
        # Strict is only requested when the *compiled schema* can satisfy the
        # strict dialect. An open map (``dict[str, Any]``) compiles to an object
        # node with no ``properties``; asking for strict alongside it ships a
        # schema OpenAI rejects outright.
        strict = self.use_strict_schema(output_format)
        schema = SchemaOptimizer.create_optimized_json_schema(output_format, strict=strict)
        # One dialect rule, applied where the family declares it: a gateway
        # that translates this request into Gemini's ``response_schema``
        # rejects ``additionalProperties`` inside an ``anyOf`` branch, which
        # is exactly what a closed action union is made of. OpenAI's own
        # strict mode wants the opposite, so this is not unconditional.
        if self.model_family.schema_rejects_closed_union_branches:
            SchemaOptimizer.open_union_branches(schema)
        response_format: JSONSchema = {
            "name": "agent_output",
            "strict": strict,
            "schema": schema,
        }

        async with self.request_client() as client:
            response = await client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                response_format=ResponseFormatJSONSchema(
                    json_schema=response_format, type="json_schema"
                ),
                **model_params,
                **extra_kwargs,
            )

        choice = response.choices[0] if response.choices else None
        if choice is None or choice.message.content is None:
            raise ModelProviderError(
                message="Failed to parse structured output", status_code=500, model=self.name
            )

        # Before the parse, not after: half-written JSON fails validation, and
        # the resulting report would name the model's compliance instead of the
        # budget. See ModelOutputTruncatedError for why this is its own type.
        if choice.finish_reason == "length":
            raise ModelOutputTruncatedError(
                model=self.name,
                stop_reason=choice.finish_reason,
                ceiling_field="max_completion_tokens",
                ceiling=self.max_completion_tokens,
                usage=self.get_usage(response),
                partial_output=choice.message.content,
            )

        # No `tool_calls=` here: `standard_ainvoke` refuses tools on the
        # structured path, so a compliant provider cannot return any.
        usage = self.get_usage(response)
        return ChatInvokeCompletion(
            # This base is what every OpenAI-compatible *gateway* goes
            # through, and a gateway is where a reply arrives fenced despite
            # `response_format` — the translation to some vendor's native API
            # dropped it. Five other providers each unwrapped fences on their
            # own and this one, the widest path, did not.
            completion=self.validate_structured(
                output_format,
                strip_code_fence(choice.message.content),
                usage=usage,
                wire_text=choice.message.content,
            ),
            thinking=message_reasoning(choice.message),
            usage=usage,
            stop_reason=choice.finish_reason,
        )

    # ------------------------------------------------------------------
    # Default ainvoke — delegates to standard_ainvoke
    # ------------------------------------------------------------------
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
        return await self.standard_ainvoke(
            messages, output_format, tools=tools, tool_choice=tool_choice
        )
