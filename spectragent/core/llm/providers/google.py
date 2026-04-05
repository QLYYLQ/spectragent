import asyncio
import json
import logging
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, TypeVar, overload

from google import genai
from google.auth.credentials import Credentials
from google.genai import types
from google.genai.types import MediaModality
from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.exceptions import ModelProviderError
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.schema.gemini import fix_gemini_schema
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.google_format import GoogleMessageSerializer
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatGoogle(ChatModelBase):
    __registry_key__ = "google.default"

    # Model configuration
    model: Annotated[str, Field(description="Google Gemini model name")]
    temperature: Annotated[float | None, Field(description="Sampling temperature")] = 0.5
    top_p: Annotated[float | None, Field(description="Nucleus sampling top-p")] = None
    seed: Annotated[int | None, Field(description="Random seed for reproducibility")] = None
    thinking_budget: Annotated[int | None, Field(description="Thinking token budget (-1 dynamic, 0 disable)")] = None
    thinking_level: Annotated[str | None, Field(description="Thinking level (minimal/low/medium/high)")] = None
    max_output_tokens: Annotated[int | None, Field(description="Maximum output tokens")] = 8096
    config: types.GenerateContentConfigDict | None = None
    include_system_in_user: Annotated[bool, Field(description="Include system messages in first user message")] = False
    supports_structured_output: Annotated[bool, Field(description="Use native JSON mode for structured output")] = True
    max_retries: Annotated[int, Field(description="Number of retries for retryable errors")] = 5
    retryable_status_codes: list[int] = field(default_factory=lambda: [429, 500, 502, 503, 504])
    retry_base_delay: Annotated[float, Field(description="Base delay in seconds for exponential backoff")] = 1.0
    retry_max_delay: Annotated[float, Field(description="Maximum delay in seconds between retries")] = 60.0

    # Client initialization parameters
    api_key: Annotated[str | None, Field(description="Google API key")] = None
    base_url: Annotated[str | None, Field(description="Custom API base URL")] = None
    vertexai: Annotated[bool | None, Field(description="Whether to use Vertex AI")] = None
    credentials: Credentials | None = None
    project: Annotated[str | None, Field(description="Google Cloud project ID")] = None
    location: Annotated[str | None, Field(description="Google Cloud location")] = None
    http_options: types.HttpOptions | types.HttpOptionsDict | None = None

    # Internal client cache
    _client: genai.Client | None = None

    @property
    def model_type(self) -> str:
        return "google"

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(f"spectragent.core.llm.google.{self.model}")

    def _get_client_params(self) -> dict[str, Any]:
        http_options = self.http_options
        if http_options is None and self.base_url is not None:
            http_options = {"base_url": self.base_url}

        base_params = {
            "api_key": self.api_key,
            "vertexai": self.vertexai,
            "credentials": self.credentials,
            "project": self.project,
            "location": self.location,
            "http_options": http_options,
        }
        return {k: v for k, v in base_params.items() if v is not None}

    def get_client(self) -> genai.Client:
        if self._client is not None:
            return self._client
        client_params = self._get_client_params()
        self._client = genai.Client(**client_params)
        return self._client

    @property
    def name(self) -> str:
        return str(self.model)

    def _get_stop_reason(self, response: types.GenerateContentResponse) -> str | None:
        if hasattr(response, "candidates") and response.candidates:
            return (
                str(response.candidates[0].finish_reason)
                if hasattr(response.candidates[0], "finish_reason")
                else None
            )
        return None

    def _get_usage(self, response: types.GenerateContentResponse) -> ChatInvokeUsage | None:
        if response.usage_metadata is None:
            return None

        image_tokens = 0
        if response.usage_metadata.prompt_tokens_details is not None:
            image_tokens = sum(
                detail.token_count or 0
                for detail in response.usage_metadata.prompt_tokens_details
                if detail.modality == MediaModality.IMAGE
            )

        return ChatInvokeUsage(
            prompt_tokens=response.usage_metadata.prompt_token_count or 0,
            completion_tokens=(response.usage_metadata.candidates_token_count or 0)
            + (response.usage_metadata.thoughts_token_count or 0),
            total_tokens=response.usage_metadata.total_token_count or 0,
            prompt_cached_tokens=response.usage_metadata.cached_content_token_count,
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=image_tokens,
        )

    def _build_config(self, output_format: type[BaseModel] | None) -> types.GenerateContentConfigDict:
        """Build the generate_content config dict."""
        config: types.GenerateContentConfigDict = {}
        if self.config:
            config = self.config.copy()

        if self.temperature is not None:
            config["temperature"] = self.temperature
        if self.top_p is not None:
            config["top_p"] = self.top_p
        if self.seed is not None:
            config["seed"] = self.seed

        # Thinking configuration
        is_gemini_3_pro = "gemini-3-pro" in self.model
        is_gemini_3_flash = "gemini-3-flash" in self.model

        if is_gemini_3_pro:
            if self.thinking_budget is not None:
                self.logger.warning(
                    f"thinking_budget={self.thinking_budget} is deprecated for Gemini 3 Pro. Use thinking_level."
                )
            if self.thinking_level in ("minimal", "medium"):
                self.logger.warning(
                    f'thinking_level="{self.thinking_level}" not supported for Gemini 3 Pro. Falling back to "low".'
                )
                self.thinking_level = "low"
            if self.thinking_level is None:
                self.thinking_level = "low"
            level = types.ThinkingLevel(self.thinking_level.upper())
            config["thinking_config"] = types.ThinkingConfigDict(thinking_level=level)
        elif is_gemini_3_flash:
            if self.thinking_level is not None:
                level = types.ThinkingLevel(self.thinking_level.upper())
                config["thinking_config"] = types.ThinkingConfigDict(thinking_level=level)
            else:
                if self.thinking_budget is None:
                    self.thinking_budget = -1
                config["thinking_config"] = types.ThinkingConfigDict(thinking_budget=self.thinking_budget)
        else:
            if self.thinking_level is not None:
                self.logger.warning(
                    f'thinking_level="{self.thinking_level}" not supported for this model. Use thinking_budget.'
                )
            if self.thinking_budget is None and ("gemini-2.5" in self.model or "gemini-flash" in self.model):
                self.thinking_budget = -1
            if self.thinking_budget is not None:
                config["thinking_config"] = types.ThinkingConfigDict(thinking_budget=self.thinking_budget)

        if self.max_output_tokens is not None:
            config["max_output_tokens"] = self.max_output_tokens

        return config

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
        contents, system_instruction = GoogleMessageSerializer.serialize_messages(
            messages, include_system_in_user=self.include_system_in_user
        )

        config = self._build_config(output_format)
        if system_instruction:
            config["system_instruction"] = system_instruction

        async def _make_api_call():
            start_time = time.time()

            try:
                if output_format is None:
                    response = await self.get_client().aio.models.generate_content(
                        model=self.model, contents=contents, config=config,  # type: ignore
                    )
                    text = response.text or ""
                    usage = self._get_usage(response)
                    return ChatInvokeCompletion(
                        completion=text, usage=usage, stop_reason=self._get_stop_reason(response),
                    )
                else:
                    if self.supports_structured_output:
                        config["response_mime_type"] = "application/json"
                        optimized_schema = SchemaOptimizer.create_gemini_optimized_schema(output_format)
                        config["response_schema"] = fix_gemini_schema(optimized_schema)

                        response = await self.get_client().aio.models.generate_content(
                            model=self.model, contents=contents, config=config,
                        )
                        usage = self._get_usage(response)

                        if response.parsed is None:
                            if response.text:
                                text = response.text.strip()
                                if text.startswith("```json") and text.endswith("```"):
                                    text = text[7:-3].strip()
                                elif text.startswith("```") and text.endswith("```"):
                                    text = text[3:-3].strip()
                                parsed_data = json.loads(text)
                                return ChatInvokeCompletion(
                                    completion=output_format.model_validate(parsed_data),
                                    usage=usage,
                                    stop_reason=self._get_stop_reason(response),
                                )
                            raise ModelProviderError(
                                message=f"No response from model {response}", status_code=500, model=self.model,
                            )

                        if isinstance(response.parsed, output_format):
                            return ChatInvokeCompletion(
                                completion=response.parsed, usage=usage, stop_reason=self._get_stop_reason(response),
                            )
                        return ChatInvokeCompletion(
                            completion=output_format.model_validate(response.parsed),
                            usage=usage,
                            stop_reason=self._get_stop_reason(response),
                        )
                    else:
                        # Fallback: request JSON via prompt
                        modified_messages = [m.model_copy(deep=True) for m in messages]
                        if modified_messages and isinstance(modified_messages[-1].content, str):
                            modified_messages[-1].content += (
                                f"\n\nPlease respond with a valid JSON object that matches this schema: "
                                f"{SchemaOptimizer.create_optimized_json_schema(output_format)}"
                            )

                        fb_contents, fb_system = GoogleMessageSerializer.serialize_messages(
                            modified_messages, include_system_in_user=self.include_system_in_user,
                        )
                        fb_config = config.copy()
                        if fb_system:
                            fb_config["system_instruction"] = fb_system

                        response = await self.get_client().aio.models.generate_content(
                            model=self.model, contents=fb_contents, config=fb_config,  # type: ignore
                        )
                        usage = self._get_usage(response)

                        if response.text:
                            text = response.text.strip()
                            if text.startswith("```json") and text.endswith("```"):
                                text = text[7:-3].strip()
                            elif text.startswith("```") and text.endswith("```"):
                                text = text[3:-3].strip()
                            parsed_data = json.loads(text)
                            return ChatInvokeCompletion(
                                completion=output_format.model_validate(parsed_data),
                                usage=usage,
                                stop_reason=self._get_stop_reason(response),
                            )
                        raise ModelProviderError(message="No response from model", status_code=500, model=self.model)
            except Exception:
                raise

        assert self.max_retries >= 1
        for attempt in range(self.max_retries):
            try:
                return await _make_api_call()
            except ModelProviderError as e:
                if e.status_code in self.retryable_status_codes and attempt < self.max_retries - 1:
                    delay = min(self.retry_base_delay * (2**attempt), self.retry_max_delay)
                    jitter = random.uniform(0, delay * 0.1)
                    await asyncio.sleep(delay + jitter)
                    continue
                raise
            except Exception as e:
                error_message = str(e)
                status_code: int | None = None
                if hasattr(e, "response"):
                    resp_obj = getattr(e, "response", None)
                    if resp_obj and hasattr(resp_obj, "status_code"):
                        status_code = getattr(resp_obj, "status_code", None)
                if "timeout" in error_message.lower() or "cancelled" in error_message.lower():
                    status_code = 504 if isinstance(e, asyncio.CancelledError) else 408
                elif "rate limit" in error_message.lower() or "429" in error_message.lower():
                    status_code = 429
                elif "service unavailable" in error_message.lower() or "503" in error_message.lower():
                    status_code = 503
                raise ModelProviderError(
                    message=error_message, status_code=status_code or 502, model=self.name,
                ) from e

        raise RuntimeError("Retry loop completed without return or exception")
