import asyncio
import json
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, TypeVar, overload

from google import genai
from google.auth.credentials import Credentials
from google.genai import types
from google.genai.types import MediaModality
from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.exceptions import ModelOutputTruncatedError, ModelProviderError
from spectragent.core.llm.fences import strip_code_fence
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.model_families import ModelFamily, detect_model_family
from spectragent.core.llm.schema.gemini import fix_gemini_schema
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.google_format import GoogleMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition

#: The property ``fix_gemini_schema`` invents for an object with no
#: properties, because Gemini's ``responseSchema`` subset rejects an empty
#: one. A zero-parameter action (``browser.go_back``) is exactly that, so
#: the model dutifully answers ``{"browser.go_back": {"_placeholder": ""}}``
#: — and a closed params model with ``extra="forbid"`` refuses it.
#:
#: The provider that added the key is the provider that takes it away.
#: Doing it in the agent instead would put one provider's schema dialect
#: inside domain knowledge, where every *other* provider would carry it too.
_GEMINI_PLACEHOLDER = "_placeholder"


def _strip_placeholders(value: Any) -> Any:
    """Remove :data:`_GEMINI_PLACEHOLDER` from a decoded reply, recursively.

    Only this key, and only because this module put it in the schema. Any
    other unexpected property is the model's own invention and must still
    fail validation. Non-mapping values pass through untouched.
    """
    if isinstance(value, dict):
        cleaned = {
            key: _strip_placeholders(item)
            for key, item in value.items()
            if key != _GEMINI_PLACEHOLDER
        }
        return cleaned
    if isinstance(value, list):
        return [_strip_placeholders(item) for item in value]
    return value


#: ``tool_choice`` spelled the Gemini way (``function_calling_config.mode``).
_GEMINI_TOOL_MODE: dict[str, types.FunctionCallingConfigMode] = {
    "auto": types.FunctionCallingConfigMode.AUTO,
    "required": types.FunctionCallingConfigMode.ANY,
    "none": types.FunctionCallingConfigMode.NONE,
}
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
    thinking_budget: Annotated[
        int | None, Field(description="Thinking token budget (-1 dynamic, 0 disable)")
    ] = None
    thinking_level: Annotated[
        str | None, Field(description="Thinking level (minimal/low/medium/high)")
    ] = None
    max_output_tokens: Annotated[int | None, Field(description="Maximum output tokens")] = 8096
    config: types.GenerateContentConfigDict | None = None
    include_system_in_user: Annotated[
        bool, Field(description="Include system messages in first user message")
    ] = False
    supports_structured_output: Annotated[
        bool, Field(description="Use native JSON mode for structured output")
    ] = True
    max_retries: Annotated[int, Field(description="Number of retries for retryable errors")] = 5
    retryable_status_codes: list[int] = field(default_factory=lambda: [429, 500, 502, 503, 504])
    retry_base_delay: Annotated[
        float, Field(description="Base delay in seconds for exponential backoff")
    ] = 1.0
    retry_max_delay: Annotated[
        float, Field(description="Maximum delay in seconds between retries")
    ] = 60.0

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

    @property
    def model_family(self) -> ModelFamily:
        """Declared capabilities for this model (see ``core/llm/model_families``)."""
        return detect_model_family(str(self.model))

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

    def _reject_if_truncated(
        self, response: types.GenerateContentResponse, usage: ChatInvokeUsage | None
    ) -> None:
        """Refuse a reply the output-token ceiling cut off.

        Compared against the enum member, not the string it renders to:
        ``_get_stop_reason`` returns ``str(finish_reason)``, which is
        ``'FinishReason.MAX_TOKENS'`` — so the obvious ``== "MAX_TOKENS"``
        never matches and the guard would look installed while never firing.

        Raised inside ``_make_api_call``, so it passes through this class's
        own retry loop below. That loop retries on
        ``retryable_status_codes``, and ``ModelOutputTruncatedError`` carries
        400 precisely so it does not qualify: retrying a truncation re-runs a
        maximum-length generation against an unchanged ceiling.
        """
        candidates = getattr(response, "candidates", None)
        if not candidates:
            return
        if getattr(candidates[0], "finish_reason", None) is types.FinishReason.MAX_TOKENS:
            raise ModelOutputTruncatedError(
                model=self.name,
                stop_reason=self._get_stop_reason(response),
                ceiling_field="max_output_tokens",
                ceiling=self.max_output_tokens,
                usage=usage,
            )

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

    def _build_config(
        self, output_format: type[BaseModel] | None
    ) -> types.GenerateContentConfigDict:
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
                config["thinking_config"] = types.ThinkingConfigDict(
                    thinking_budget=self.thinking_budget
                )
        else:
            if self.thinking_level is not None:
                self.logger.warning(
                    f'thinking_level="{self.thinking_level}" not supported for this model. Use thinking_budget.'
                )
            if self.thinking_budget is None and (
                "gemini-2.5" in self.model or "gemini-flash" in self.model
            ):
                self.thinking_budget = -1
            if self.thinking_budget is not None:
                config["thinking_config"] = types.ThinkingConfigDict(
                    thinking_budget=self.thinking_budget
                )

        if self.max_output_tokens is not None:
            config["max_output_tokens"] = self.max_output_tokens

        return config

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
        contents, system_instruction = GoogleMessageSerializer.serialize_messages(
            messages, include_system_in_user=self.include_system_in_user
        )

        if tools and output_format is not None:
            # Gemini's response_schema pins the whole reply to JSON, and the
            # prompt-JSON fallback says "reply with JSON only" — neither leaves
            # room for a function_call part.
            self.reject_unsupported_tools(
                tools,
                reason=(
                    "Gemini's structured output constrains the whole reply to the "
                    "response schema, leaving no room for a function_call part. "
                    "Pass tools without output_format."
                ),
            )

        config = self._build_config(output_format)
        if system_instruction:
            config["system_instruction"] = system_instruction
        if tools:
            config["tools"] = list(GoogleMessageSerializer.serialize_tools(tools))
            config["tool_config"] = types.ToolConfigDict(
                function_calling_config=types.FunctionCallingConfigDict(
                    mode=_GEMINI_TOOL_MODE[tool_choice]
                )
            )

        async def _make_api_call():
            try:
                if output_format is None:
                    response = await self.get_client().aio.models.generate_content(
                        model=self.model,
                        contents=contents,
                        config=config,  # type: ignore
                    )
                    # `response.text` is a convenience accessor that concatenates
                    # text parts; with tools advertised the reply can be a bare
                    # function_call and the accessor is unhappy about it. The
                    # tool calls are read off the parts directly below, so an
                    # empty string is the right answer, not a crash.
                    try:
                        text = response.text or ""
                    except (ValueError, AttributeError):
                        text = ""
                    usage = self._get_usage(response)
                    self._reject_if_truncated(response, usage)
                    return ChatInvokeCompletion(
                        completion=text,
                        usage=usage,
                        stop_reason=self._get_stop_reason(response),
                        tool_calls=GoogleMessageSerializer.parse_tool_calls(response),
                    )
                else:
                    if self.supports_structured_output:
                        config["response_mime_type"] = "application/json"
                        optimized_schema = SchemaOptimizer.create_gemini_optimized_schema(
                            output_format
                        )
                        if self.model_family.schema_requires_gemini_fix:
                            optimized_schema = fix_gemini_schema(optimized_schema)
                        # Unconditional, and not a duplicate of the line above:
                        # this provider *is* the Gemini dialect — every request
                        # it makes lands on a ``generateContent`` endpoint whose
                        # ``response_schema`` has no ``additionalProperties``
                        # field to put a value in. Measured: the root node alone
                        # is a 400. The family table decides whether a *model*
                        # needs `fix_gemini_schema`'s other repairs; the
                        # endpoint decides this, so it does not get a vote.
                        #
                        # It also used to work by accident: ``google-genai``
                        # drops unknown keywords when it converts this dict to
                        # its typed ``Schema``. That is an SDK side effect, and
                        # a provider whose correctness lives in someone else's
                        # conversion step has no test that can fail.
                        SchemaOptimizer.strip_additional_properties(optimized_schema)
                        config["response_schema"] = optimized_schema

                        response = await self.get_client().aio.models.generate_content(
                            model=self.model,
                            contents=contents,
                            config=config,
                        )
                        usage = self._get_usage(response)
                        self._reject_if_truncated(response, usage)

                        if response.parsed is None:
                            if response.text:
                                text = strip_code_fence(response.text)
                                parsed_data = json.loads(text)
                                return ChatInvokeCompletion(
                                    completion=self._validate_structured(
                                        output_format, _strip_placeholders(parsed_data), usage=usage
                                    ),
                                    usage=usage,
                                    stop_reason=self._get_stop_reason(response),
                                )
                            raise ModelProviderError(
                                message=f"No response from model {response}",
                                status_code=500,
                                model=self.model,
                            )

                        if isinstance(response.parsed, output_format):
                            return ChatInvokeCompletion(
                                completion=response.parsed,
                                usage=usage,
                                stop_reason=self._get_stop_reason(response),
                            )
                        return ChatInvokeCompletion(
                            completion=self._validate_structured(
                                output_format, _strip_placeholders(response.parsed), usage=usage
                            ),
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
                            modified_messages,
                            include_system_in_user=self.include_system_in_user,
                        )
                        fb_config = config.copy()
                        if fb_system:
                            fb_config["system_instruction"] = fb_system

                        response = await self.get_client().aio.models.generate_content(
                            model=self.model,
                            contents=fb_contents,
                            config=fb_config,  # type: ignore
                        )
                        usage = self._get_usage(response)
                        self._reject_if_truncated(response, usage)

                        if response.text:
                            text = strip_code_fence(response.text)
                            parsed_data = json.loads(text)
                            return ChatInvokeCompletion(
                                completion=self._validate_structured(
                                    output_format, _strip_placeholders(parsed_data), usage=usage
                                ),
                                usage=usage,
                                stop_reason=self._get_stop_reason(response),
                            )
                        raise ModelProviderError(
                            message="No response from model", status_code=500, model=self.model
                        )
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
                elif (
                    "service unavailable" in error_message.lower() or "503" in error_message.lower()
                ):
                    status_code = 503
                raise ModelProviderError(
                    message=error_message,
                    status_code=status_code or 502,
                    model=self.name,
                ) from e

        raise RuntimeError("Retry loop completed without return or exception")
