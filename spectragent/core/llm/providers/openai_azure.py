"""ChatAzureOpenAI — Azure OpenAI provider."""

# NOTE: 不使用 from __future__ import annotations，为适配 conscribe 0.5
# 的 nested config 类型提取机制（详见 openai_base.py 注释）。

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

import httpx
from openai import APIConnectionError, APIStatusError, RateLimitError
from openai import AsyncAzureOpenAI as AsyncAzureOpenAIClient
from pydantic import BaseModel, Field

from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.fences import strip_code_fence
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.providers.openai_base import OpenAICompatBase
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.openai_responses import ResponsesAPIMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# Azure models that require the Responses API
RESPONSES_API_ONLY_MODELS: list[str] = [
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5.1-codex-max",
    "gpt-5-codex",
    "codex-mini-latest",
    "computer-use-preview",
]

# Accepted spellings for the tri-state `use_responses_api` YAML field.
_RESPONSES_API_TRUE = frozenset({"true", "1", "yes", "on"})
_RESPONSES_API_FALSE = frozenset({"false", "0", "no", "off"})


@dataclass
class ChatAzureOpenAI(OpenAICompatBase):
    """Azure OpenAI provider."""

    __registry_key__ = "openai.azure"

    # --- Azure-specific fields ---
    api_version: Annotated[
        str | None, Field(description="Azure API version. None (the default): 2024-12-01-preview.")
    ] = None
    azure_endpoint: Annotated[
        str | None,
        Field(
            description="Azure endpoint URL. None (the default): read from AZURE_OPENAI_ENDPOINT."
        ),
    ] = None
    azure_deployment: Annotated[
        str | None,
        Field(
            description="Azure deployment name. None (the default): read from AZURE_OPENAI_DEPLOYMENT."
        ),
    ] = None
    azure_ad_token: Annotated[
        str | None,
        Field(
            description="Azure AD token. None (the default): the client reads AZURE_OPENAI_AD_TOKEN."
        ),
    ] = None
    azure_ad_token_provider: Any | None = None
    use_responses_api: Annotated[
        str, Field(description="Use Responses API: true, false, or auto")
    ] = "auto"

    @property
    def provider(self) -> str:
        return "azure"

    def get_client(self) -> AsyncAzureOpenAIClient:
        import os

        self.api_key = (
            self.api_key or os.getenv("AZURE_OPENAI_KEY") or os.getenv("AZURE_OPENAI_API_KEY")
        )
        self.azure_endpoint = self.azure_endpoint or os.getenv("AZURE_OPENAI_ENDPOINT")
        self.azure_deployment = self.azure_deployment or os.getenv("AZURE_OPENAI_DEPLOYMENT")

        params: dict[str, Any] = {}
        for k, v in {
            "api_key": self.api_key,
            "api_version": self.api_version or "2024-12-01-preview",
            "azure_endpoint": self.azure_endpoint,
            "azure_deployment": self.azure_deployment,
            "base_url": self.base_url,
            "azure_ad_token": self.azure_ad_token,
            "azure_ad_token_provider": self.azure_ad_token_provider,
            "http_client": self.http_client,
        }.items():
            if v is not None:
                params[k] = v
        if self.default_headers:
            params["default_headers"] = self.default_headers
        if self.default_query:
            params["default_query"] = self.default_query
        if "http_client" not in params:
            params["http_client"] = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=6)
            )
        return AsyncAzureOpenAIClient(**params)

    def _should_use_responses_api(self) -> bool:
        """Resolve the tri-state ``use_responses_api`` field.

        The field is typed ``str`` (it comes from YAML), so the previous
        ``isinstance(..., bool)`` test never fired and ``"true"``/``"false"``
        were both silently treated as ``"auto"``.
        """
        mode = self.use_responses_api
        if isinstance(mode, bool):
            return mode
        normalized = str(mode).strip().lower()
        if normalized in _RESPONSES_API_TRUE:
            return True
        if normalized in _RESPONSES_API_FALSE:
            return False
        if normalized != "auto":
            raise ValueError(
                f"Invalid use_responses_api={mode!r}. "
                f"Expected 'auto', one of {sorted(_RESPONSES_API_TRUE)}, "
                f"or one of {sorted(_RESPONSES_API_FALSE)}."
            )
        model_lower = str(self.model).lower()
        return any(m.lower() in model_lower for m in RESPONSES_API_ONLY_MODELS)

    async def _ainvoke_responses_api(
        self, messages: Sequence[BaseMessage], output_format: type[T] | None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        input_messages = ResponsesAPIMessageSerializer.serialize_messages(messages)
        model_params: dict[str, Any] = {"model": self.model, "input": input_messages}
        if self.temperature is not None:
            model_params["temperature"] = self.temperature
        if self.max_completion_tokens is not None:
            model_params["max_output_tokens"] = self.max_completion_tokens
        if self.top_p is not None:
            model_params["top_p"] = self.top_p
        if self.service_tier is not None:
            model_params["service_tier"] = self.service_tier
        effort = self._effective_reasoning_effort()
        if effort is not None:
            model_params["reasoning"] = {"effort": effort}
        if self._is_listed_reasoning_model():
            model_params.pop("temperature", None)

        try:
            if output_format is None:
                async with self._request_client() as client:
                    response = await client.responses.create(**model_params)
                return ChatInvokeCompletion(
                    completion=response.output_text or "",
                    usage=self._get_usage_from_responses(response),
                    stop_reason=response.status if response.status else None,
                )
            else:
                strict = self._use_strict_schema(output_format)
                json_schema = SchemaOptimizer.create_optimized_json_schema(
                    output_format, strict=strict
                )
                if self.model_family.schema_rejects_closed_union_branches:
                    SchemaOptimizer.open_union_branches(json_schema)
                model_params["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": "agent_output",
                        "strict": strict,
                        "schema": json_schema,
                    }
                }
                async with self._request_client() as client:
                    response = await client.responses.create(**model_params)
                if not response.output_text:
                    raise ModelProviderError(
                        message="Failed to parse structured output",
                        status_code=500,
                        model=self.name,
                    )
                usage = self._get_usage_from_responses(response)
                return ChatInvokeCompletion(
                    completion=self._validate_structured(
                        output_format,
                        strip_code_fence(response.output_text),
                        usage=usage,
                        wire_text=response.output_text,
                    ),
                    usage=usage,
                    stop_reason=response.status if response.status else None,
                )
        except ModelProviderError:
            # Already typed — a reply the model can fix
            # (StructuredOutputValidationError) must not be flattened into a
            # generic provider failure by the catch-all below, or the Loop
            # ends the run over one mistyped parameter.
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

    @property
    def supports_tools(self) -> bool:
        """False on the Responses API path — see :meth:`_ainvoke_responses_api`."""
        return not self._should_use_responses_api()

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
        if self._should_use_responses_api():
            # The Responses API models tool traffic as `function_call` /
            # `function_call_output` *items*, not chat messages, which
            # ResponsesAPIMessageSerializer does not emit. Flattening them into
            # text would break call/result correlation invisibly.
            self.reject_unsupported_tools(
                tools,
                reason=(
                    f"model={self.model!r} is routed to the Responses API, whose "
                    f"tool traffic uses function_call/function_call_output items "
                    f"rather than chat messages. Set use_responses_api='false' to "
                    f"drive tools through chat completions."
                ),
            )
            return await self._ainvoke_responses_api(messages, output_format, **kwargs)
        return await self._standard_ainvoke(
            messages, output_format, tools=tools, tool_choice=tool_choice
        )
