"""ChatVercel — Vercel AI Gateway provider via OpenAI-compatible base."""

# NOTE: 不使用 from __future__ import annotations，为适配 conscribe 0.5
# 的 nested config 类型提取机制（详见 openai_base.py 注释）。

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel, Field

from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.messages import BaseMessage, ContentPartTextParam as MsgTextParam, SystemMessage
from spectragent.core.llm.providers.openai_base import OpenAICompatBase
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.openai_format import OpenAIMessageSerializer
from spectragent.core.llm.views import ChatInvokeCompletion

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatVercel(OpenAICompatBase):
    """Vercel AI Gateway provider — prompt-based JSON for some models."""

    __registry_key__ = "openai.vercel"

    # --- Vercel-specific ---
    provider_options: Annotated[dict[str, Any] | None, Field(description="Provider options")] = None

    @property
    def provider(self) -> str:
        return "vercel"

    def get_client(self) -> AsyncOpenAI:
        effective_base_url = self.base_url or "https://ai-gateway.vercel.sh/v1"
        params: dict[str, Any] = {
            "api_key": self.api_key, "max_retries": self.max_retries,
            "base_url": effective_base_url,
        }
        return AsyncOpenAI(**params)

    def _vercel_needs_prompt_json(self) -> bool:
        """Check if this Vercel model needs prompt-based JSON extraction."""
        is_google = self.model.startswith("google/")
        is_anthropic = self.model.startswith("anthropic/")
        is_reasoning = self.reasoning_models and any(
            str(p).lower() in str(self.model).lower() for p in self.reasoning_models
        )
        return is_google or is_anthropic or bool(is_reasoning)

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
        extra_kwargs: dict[str, Any] = {}
        if self.provider_options:
            extra_kwargs["extra_body"] = {"providerOptions": self.provider_options}

        try:
            if output_format is None:
                return await self._standard_ainvoke(messages, None, extra_kwargs)

            # Structured output: prompt-based for Google/Anthropic/reasoning models
            if self._vercel_needs_prompt_json():
                return await self._structured_vercel_prompt(messages, output_format, extra_kwargs)

            # Default: response_format
            return await self._standard_ainvoke(messages, output_format, extra_kwargs)

        except ModelProviderError:
            raise
        except RateLimitError as e:
            raise ModelRateLimitError(message=e.message, model=self.name) from e
        except APIConnectionError as e:
            raise ModelProviderError(message=str(e), model=self.name) from e
        except APIStatusError as e:
            raise ModelProviderError(message=e.message, status_code=e.status_code, model=self.name) from e
        except Exception as e:
            raise ModelProviderError(message=str(e), model=self.name) from e

    async def _structured_vercel_prompt(
        self, messages: Sequence[BaseMessage], output_format: type[T],
        extra_kwargs: dict[str, Any],
    ) -> ChatInvokeCompletion[T]:
        from spectragent.core.llm.schema.gemini import fix_gemini_schema

        modified = [m.model_copy(deep=True) for m in messages]
        schema = SchemaOptimizer.create_gemini_optimized_schema(output_format)
        instruction = f"\n\nIMPORTANT: Respond with ONLY a valid JSON object matching this schema:\n{json.dumps(schema, indent=2)}"

        added = False
        if modified and modified[0].role == "system":
            if isinstance(modified[0].content, str):
                modified[0].content += instruction
                added = True
            elif isinstance(modified[0].content, list):
                modified[0].content.append(MsgTextParam(text=instruction))
                added = True
        elif modified and modified[-1].role == "user":
            if isinstance(modified[-1].content, str):
                modified[-1].content += instruction
                added = True
        if not added:
            modified.insert(0, SystemMessage(content=instruction))

        vercel_messages = OpenAIMessageSerializer.serialize_messages(modified)
        model_params = self._common_model_params()
        response = await self.get_client().chat.completions.create(
            model=self.model, messages=vercel_messages, **model_params, **extra_kwargs,
        )
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise ModelProviderError(message="No response from model", status_code=500, model=self.name)

        text = content.strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        elif text.startswith("```") and text.endswith("```"):
            text = text[3:-3].strip()

        parsed = output_format.model_validate(json.loads(text))
        return ChatInvokeCompletion(
            completion=parsed,
            usage=self._get_usage(response),
            stop_reason=response.choices[0].finish_reason if response.choices else None,
        )
