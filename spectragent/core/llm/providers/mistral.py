# NOTE: 不使用 from __future__ import annotations，为适配 conscribe 0.5
# 的 nested config 类型提取机制。

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, cast, overload

import httpx
from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.messages import BaseMessage, Function, ToolCall
from spectragent.core.llm.schema.mistral import MistralSchemaOptimizer
from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


def _parse_raw_tool_calls(choice: dict[str, Any]) -> list[ToolCall]:
    """Read tool calls off Mistral's raw JSON choice.

    This provider speaks HTTP directly rather than through an SDK, so the
    response is plain dicts rather than typed objects.
    """
    raw = (choice.get("message") or {}).get("tool_calls") or []
    calls: list[ToolCall] = []
    for call in raw:
        function = call.get("function") or {}
        arguments = function.get("arguments", "{}")
        calls.append(
            ToolCall(
                id=call.get("id", ""),
                function=Function(
                    name=function.get("name", ""),
                    arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
                ),
            )
        )
    return calls


@dataclass
class ChatMistral(ChatModelBase):
    __registry_key__ = "mistral.default"

    model: Annotated[str, Field(description="Mistral model name")] = "mistral-medium-latest"

    # Generation params
    temperature: Annotated[float | None, Field(description="Sampling temperature")] = 0.2
    top_p: Annotated[
        float | None, Field(description="Nucleus sampling top-p. None (the default): not sent.")
    ] = None
    max_tokens: Annotated[int | None, Field(description="Max tokens to generate")] = 4096
    seed: Annotated[
        int | None,
        Field(description="Random seed for reproducibility. None (the default): not sent."),
    ] = None
    safe_prompt: Annotated[bool, Field(description="Enable Mistral safe prompt mode")] = False

    # Client params
    api_key: Annotated[
        str | None,
        Field(description="Mistral API key. None (the default): read from MISTRAL_API_KEY."),
    ] = None
    base_url: Annotated[str, Field(description="Mistral API base URL")] = (
        "https://api.mistral.ai/v1"
    )
    timeout: Annotated[
        float | None,
        Field(
            description="Request timeout in seconds. None (the default): the HTTP client's 5 s default."
        ),
    ] = None
    max_retries: Annotated[int, Field(description="Max request retries")] = 5
    default_headers: Mapping[str, str] | None = None
    default_query: Mapping[str, object] | None = None
    http_client: httpx.AsyncClient | None = None

    @property
    def model_type(self) -> str:
        return "mistral"

    @property
    def name(self) -> str:
        return str(self.model)

    def _get_api_key(self) -> str:
        key = self.api_key or os.getenv("MISTRAL_API_KEY")
        if not key:
            raise ModelProviderError("Missing Mistral API key", status_code=401, model=self.name)
        return key

    def _get_base_url(self) -> str:
        return str(os.getenv("MISTRAL_BASE_URL", self.base_url)).rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._get_api_key()}",
            "Content-Type": "application/json",
        }
        if self.default_headers:
            headers.update(self.default_headers)
        return headers

    def _client(self) -> httpx.AsyncClient:
        if self.http_client:
            return self.http_client
        if not hasattr(self, "_cached_client"):
            transport = httpx.AsyncHTTPTransport(retries=self.max_retries)
            client_args: dict[str, Any] = {"transport": transport}
            if self.timeout is not None:
                client_args["timeout"] = self.timeout
            self._cached_client = httpx.AsyncClient(**client_args)
        return self._cached_client

    def _serialize_messages(self, messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
        raw: list[dict[str, Any]] = []
        for msg in OpenAIMessageSerializer.serialize_messages(messages):
            dumper = getattr(msg, "model_dump", None)
            if callable(dumper):
                raw.append(cast(dict[str, Any], dumper(exclude_none=True)))
            else:
                raw.append(cast(dict[str, Any], msg))
        return raw

    def _query_params(self) -> dict[str, str] | None:
        if self.default_query is None:
            return None
        return {k: str(v) for k, v in self.default_query.items() if v is not None}

    def _build_usage(self, usage: dict[str, Any] | None) -> ChatInvokeUsage | None:
        if not usage:
            return None
        return ChatInvokeUsage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            prompt_cached_tokens=None,
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=None,
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )

    def _extract_content_text(self, choice: dict[str, Any]) -> str:
        message = choice.get("message", {})
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text" and "text" in part:
                        parts.append(part["text"])
                    elif "content" in part:
                        parts.append(str(part["content"]))
            return "".join(parts)
        if isinstance(content, dict):
            return json.dumps(content)
        return content or ""

    def _parse_error(self, response: httpx.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict):
                for key in ("message", "error", "detail"):
                    val = body.get(key)
                    if isinstance(val, dict):
                        val = val.get("message") or val.get("detail")
                    if val:
                        return str(val)
        except Exception:
            pass
        return response.text

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._get_base_url()}/chat/completions"
        response = await self._client().post(
            url, headers=self._auth_headers(), json=payload, params=self._query_params()
        )
        if response.status_code >= 400:
            message = self._parse_error(response)
            if response.status_code == 429:
                raise ModelRateLimitError(
                    message=message, status_code=response.status_code, model=self.name
                )
            raise ModelProviderError(
                message=message, status_code=response.status_code, model=self.name
            )
        try:
            return response.json()
        except Exception as e:
            raise ModelProviderError(
                message=f"Failed to parse Mistral response: {e}", model=self.name
            ) from e

    @overload
    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: type[T], **kwargs: Any
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
        if tools and output_format is not None:
            # `response_format: json_schema` pins the whole reply to the schema.
            self.reject_unsupported_tools(
                tools,
                reason=(
                    "its structured-output path pins the reply to a json_schema "
                    "response_format, leaving no room for a tool call. Pass tools "
                    "without output_format."
                ),
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize_messages(messages),
        }
        if tools:
            # Mistral's /chat/completions mirrors the OpenAI tool vocabulary.
            payload["tools"] = OpenAIMessageSerializer.serialize_tools(tools)
            payload["tool_choice"] = tool_choice
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.seed is not None:
            payload["seed"] = self.seed
        if self.safe_prompt:
            payload["safe_prompt"] = self.safe_prompt

        if output_format is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_output",
                    "strict": True,
                    "schema": MistralSchemaOptimizer.create_mistral_compatible_schema(
                        output_format
                    ),
                },
            }

        try:
            data = await self._post(payload)
            choices = data.get("choices", [])
            if not choices:
                raise ModelProviderError("Mistral returned no choices", model=self.name)
            content_text = self._extract_content_text(choices[0])
            usage = self._build_usage(data.get("usage"))

            if output_format is None:
                return ChatInvokeCompletion(
                    completion=content_text,
                    usage=usage,
                    tool_calls=_parse_raw_tool_calls(choices[0]),
                )
            parsed = self._validate_structured(output_format, content_text, usage=usage)
            return ChatInvokeCompletion(completion=parsed, usage=usage)

        except (ModelRateLimitError, ModelProviderError):
            raise
        except Exception as e:
            logger.error(f"Mistral invocation failed: {e}")
            raise ModelProviderError(message=str(e), model=self.name) from e
