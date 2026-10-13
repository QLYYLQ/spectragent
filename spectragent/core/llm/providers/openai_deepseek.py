"""ChatDeepSeek — DeepSeek provider via OpenAI-compatible base."""

# NOTE: 不使用 from __future__ import annotations，为适配 conscribe 0.5
# 的 nested config 类型提取机制（详见 openai_base.py 注释）。

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel, Field

from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.providers.openai_base import OpenAICompatBase, message_reasoning
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatDeepSeek(OpenAICompatBase):
    """DeepSeek provider — uses OpenAI-compatible API with tool-calling structured output."""

    __registry_key__ = "openai.deepseek"

    # --- DeepSeek-specific ---
    client_params: Annotated[
        dict[str, Any] | None,
        Field(description="Extra client constructor arguments. None (the default): none."),
    ] = None

    @property
    def provider(self) -> str:
        return "deepseek"

    def get_client(self) -> AsyncOpenAI:
        effective_base_url = self.base_url or "https://api.deepseek.com/v1"
        params: dict[str, Any] = {
            "api_key": self.api_key,
            "max_retries": self.max_retries,
            "base_url": effective_base_url,
        }
        if self.client_params:
            params.update(self.client_params)
        return AsyncOpenAI(**params)

    def _apply_deepseek_beta(self, ds_messages: list) -> None:
        if (
            self.base_url
            and str(self.base_url).endswith("/beta")
            and ds_messages
            and isinstance(ds_messages[-1], dict)
            and ds_messages[-1].get("role") == "assistant"
        ):
            ds_messages[-1]["prefix"] = True

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
        openai_messages = OpenAIMessageSerializer.serialize_messages(messages)
        self._apply_deepseek_beta(openai_messages)

        try:
            model_params = self._common_model_params()

            if output_format is None:
                async with self._request_client() as client:
                    response = await client.chat.completions.create(
                        model=self.model,
                        messages=openai_messages,
                        **self._tool_kwargs(tools, tool_choice),
                        **model_params,
                    )
                choice = response.choices[0] if response.choices else None
                if choice is None:
                    raise ModelProviderError(
                        message="Invalid response: missing or empty `choices`.",
                        status_code=502,
                        model=self.name,
                    )
                return ChatInvokeCompletion(
                    completion=choice.message.content or "",
                    thinking=message_reasoning(choice.message),
                    usage=self._get_usage(response),
                    stop_reason=choice.finish_reason,
                    tool_calls=OpenAIMessageSerializer.parse_tool_calls(
                        getattr(choice.message, "tool_calls", None)
                    ),
                )
            else:
                # DeepSeek: forced tool calling for structured output. The tool
                # slot is already spoken for by the output schema, and
                # tool_choice can only name one function, so caller tools cannot
                # also be offered on this path.
                self.reject_unsupported_tools(
                    tools,
                    reason=(
                        "its structured-output path forces a single tool call to "
                        "carry the schema, leaving no free tool slot. Pass tools "
                        "without output_format."
                    ),
                )
                tool_name = output_format.__name__
                schema = SchemaOptimizer.create_optimized_json_schema(
                    output_format, strict=self._use_strict_schema(output_format)
                )
                schema.pop("title", None)
                schema_tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": f"Return a JSON object of type {tool_name}",
                            "parameters": schema,
                        },
                    }
                ]
                schema_tool_choice = {"type": "function", "function": {"name": tool_name}}

                async with self._request_client() as client:
                    resp = await client.chat.completions.create(
                        model=self.model,
                        messages=openai_messages,
                        tools=schema_tools,
                        tool_choice=schema_tool_choice,
                        **model_params,
                    )
                msg = resp.choices[0].message
                if not msg.tool_calls:
                    raise ValueError("Expected tool_calls in response but got none")
                raw_args = msg.tool_calls[0].function.arguments
                # DeepSeek returns the same `usage` block as OpenAI on the
                # tool-call path; reporting None discarded it.
                usage = self._get_usage(resp)
                return ChatInvokeCompletion(
                    completion=self._validate_structured(output_format, raw_args, usage=usage),
                    thinking=message_reasoning(msg),
                    usage=usage,
                    stop_reason=resp.choices[0].finish_reason,
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
