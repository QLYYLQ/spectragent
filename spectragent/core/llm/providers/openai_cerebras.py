"""ChatCerebras — Cerebras provider via OpenAI-compatible base."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel, Field

from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.providers.openai_base import (
    OpenAICompatBase,
    message_reasoning,
    resolve_http_transport,
)
from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatCerebras(OpenAICompatBase):
    """Cerebras provider — prompt-injection fallback for structured output."""

    __registry_key__ = "openai.cerebras"

    # --- Cerebras-specific ---
    client_params: Annotated[
        dict[str, Any] | None,
        Field(description="Extra client constructor arguments. None (the default): none."),
    ] = None

    @property
    def provider(self) -> str:
        return "cerebras"

    @property
    def http_transport(self) -> httpx.AsyncClient | None:
        return resolve_http_transport(self.http_client, self.client_params)

    def get_client(self) -> AsyncOpenAI:
        effective_base_url = self.base_url or "https://api.cerebras.ai/v1"
        params: dict[str, Any] = {
            "api_key": self.api_key,
            "max_retries": self.max_retries,
            "base_url": effective_base_url,
        }
        if self.client_params:
            params.update(self.client_params)
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
        if output_format is None:
            return await self.standard_ainvoke(messages, tools=tools, tool_choice=tool_choice)

        # Cerebras has no native json_schema response_format, so structured output
        # is prompt-injected ("valid JSON only, no other text") — an instruction
        # that directly contradicts offering the model tools it may call instead.
        self.reject_unsupported_tools(
            tools,
            reason=(
                "its structured-output path injects a 'reply with JSON only' "
                "instruction, which contradicts offering callable tools. Pass "
                "tools without output_format, or use a provider with native "
                "json_schema support."
            ),
        )

        # Cerebras: prompt injection fallback for structured output
        openai_messages = OpenAIMessageSerializer.serialize_messages(messages)
        try:
            model_params = self.common_model_params()
            schema = output_format.model_json_schema()
            json_prompt = f"\nPlease respond with a JSON object that follows this exact schema:\n{json.dumps(schema, indent=2)}\nYour response must be valid JSON only, no other text.\n"

            if openai_messages and openai_messages[-1]["role"] == "user":
                if isinstance(openai_messages[-1]["content"], str):
                    openai_messages[-1]["content"] += json_prompt
                elif isinstance(openai_messages[-1]["content"], list):
                    openai_messages[-1]["content"].append({"type": "text", "text": json_prompt})
            else:
                openai_messages.append({"role": "user", "content": json_prompt})

            async with self.request_client() as client:
                resp = await client.chat.completions.create(
                    model=self.model,
                    messages=openai_messages,
                    **model_params,
                )
            content = resp.choices[0].message.content
            if not content:
                raise ModelProviderError("Empty response from Cerebras", model=self.name)

            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            json_str = json_match.group(0) if json_match else content
            usage = self.get_usage(resp)
            return ChatInvokeCompletion(
                completion=self.validate_structured(output_format, json_str, usage=usage),
                thinking=message_reasoning(resp.choices[0].message),
                usage=usage,
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
