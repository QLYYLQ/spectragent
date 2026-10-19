from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

import httpx
from ollama import AsyncClient as OllamaAsyncClient
from ollama import Options
from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.exceptions import ModelProviderError
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.model_families import ModelFamily, detect_model_family
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.ollama_format import OllamaMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion, ModelInvokeUsage, join_reasoning

T = TypeVar("T", bound=BaseModel)


def _thinking(response: Any) -> str | None:
    """The reasoning Ollama returned beside the reply (``message.thinking``), or ``None``.

    Read only when the response already has it: this provider sends no
    ``think`` flag. Through :func:`~spectragent.core.llm.views.join_reasoning`.
    """
    return join_reasoning([getattr(getattr(response, "message", None), "thinking", None)])


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """Let the SDK close its client without closing an injected transport."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        pass


@dataclass
class ChatOllama(ChatModelBase):
    __registry_key__ = "ollama.default"

    model: Annotated[str, Field(description="Ollama model name")]

    # Client initialization parameters
    host: Annotated[
        str | None,
        Field(
            description=(
                "Ollama server host. None (the default): the client reads OLLAMA_HOST, else "
                "http://127.0.0.1:11434."
            )
        ),
    ] = None
    timeout: Annotated[
        float | None,
        Field(description="Request timeout in seconds. None (the default): no timeout."),
    ] = None
    client_params: dict[str, Any] | None = None
    ollama_options: Mapping[str, Any] | Options | None = None

    @property
    def model_type(self) -> str:
        return "ollama"

    @property
    def name(self) -> str:
        return self.model

    @property
    def model_family(self) -> ModelFamily:
        """Declared capabilities for this model (see ``core/llm/model_families``)."""
        return detect_model_family(str(self.model))

    def get_client(self) -> OllamaAsyncClient:
        params = dict(self.client_params or {})
        if "http_client" in params:
            raise ValueError("Ollama client_params does not support http_client injection")
        if params.get("transport") is not None:
            params["transport"] = _BorrowedTransport(params["transport"])
        if params.get("mounts") is not None:
            params["mounts"] = {
                pattern: _BorrowedTransport(transport) if transport is not None else None
                for pattern, transport in params["mounts"].items()
            }
        return OllamaAsyncClient(host=self.host, timeout=self.timeout, **params)

    @staticmethod
    def _get_usage(response: Any) -> ModelInvokeUsage | None:
        """Read Ollama's token counters off the response.

        Ollama is the offline / reproducible-benchmark provider, so this is
        exactly where token accounting matters most — reporting ``usage=None``
        made every local run unaccounted.
        """
        prompt_tokens = getattr(response, "prompt_eval_count", None)
        completion_tokens = getattr(response, "eval_count", None)
        if prompt_tokens is None and completion_tokens is None:
            return None
        prompt_tokens = prompt_tokens or 0
        completion_tokens = completion_tokens or 0
        return ModelInvokeUsage(
            prompt_tokens=prompt_tokens,
            prompt_cached_tokens=None,
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=None,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
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
        ollama_messages = OllamaMessageSerializer.serialize_messages(messages)

        # Both checks run before the try: they are caller contract errors, and
        # the catch-all below would launder them into an opaque
        # ModelProviderError that points at the transport rather than the call.
        #
        # Ollama's chat endpoint takes `tools` but has no `tool_choice`: the
        # model always decides. Anything other than the default would be a
        # promise this backend cannot keep.
        if tools and tool_choice != "auto":
            self.reject_unsupported_tools(
                tools,
                reason=(
                    f"the Ollama chat endpoint has no tool_choice parameter, so "
                    f"tool_choice={tool_choice!r} cannot be enforced. Only 'auto' "
                    f"is supported here."
                ),
            )
        # `format` compiles into a decoding grammar that pins the whole reply to
        # the schema, so the model cannot emit a tool call too.
        if tools and output_format is not None:
            self.reject_unsupported_tools(
                tools,
                reason=(
                    "its structured-output path compiles the schema into a "
                    "decoding grammar that constrains the entire reply, leaving "
                    "no room for a tool call. Pass tools without output_format."
                ),
            )
        tool_kwargs: dict[str, Any] = (
            {"tools": OllamaMessageSerializer.serialize_tools(tools)} if tools else {}
        )

        try:
            async with self.client_resources.use():
                client = self.client_resources.get_or_create(
                    "ollama", self.get_client, close=lambda value: value.close()
                )
                if output_format is None:
                    response = await client.chat(
                        model=self.model,
                        messages=ollama_messages,
                        options=self.ollama_options,
                        **tool_kwargs,
                    )
                    return ChatInvokeCompletion(
                        completion=response.message.content or "",
                        thinking=_thinking(response),
                        usage=self._get_usage(response),
                        tool_calls=OllamaMessageSerializer.parse_tool_calls(response.message),
                    )
                else:
                    # Flatten $ref/$defs: Ollama compiles `format` into a grammar and
                    # local backends do not resolve JSON-Schema references.
                    schema = SchemaOptimizer.create_optimized_json_schema(
                        output_format, strict=self.use_strict_schema(output_format)
                    )
                    response = await client.chat(
                        model=self.model,
                        messages=ollama_messages,
                        format=schema,
                        options=self.ollama_options,
                    )
                    usage = self._get_usage(response)
                    return ChatInvokeCompletion(
                        completion=self.validate_structured(
                            output_format, response.message.content or "", usage=usage
                        ),
                        thinking=_thinking(response),
                        usage=usage,
                    )

        except ModelProviderError:
            # StructuredOutputValidationError is one of these, and it is the
            # only provider error the model itself can fix; re-wrapping it
            # below would cost the run.
            raise
        except Exception as e:
            raise ModelProviderError(message=str(e), model=self.name) from e
