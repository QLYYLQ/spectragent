"""OCI GenAI chat model integration."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

import oci
from oci.generative_ai_inference import GenerativeAiInferenceClient
from oci.generative_ai_inference.models import (
    BaseChatRequest,
    ChatDetails,
    CohereChatRequest,
    GenericChatRequest,
    OnDemandServingMode,
)
from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.exceptions import ModelProviderError, ModelRateLimitError
from spectragent.core.llm.fences import strip_code_fence
from spectragent.core.llm.messages import BaseMessage, ContentPartTextParam, SystemMessage
from spectragent.core.llm.schema.optimizer import SchemaOptimizer
from spectragent.core.llm.serializers.oci_format import OCIRawMessageSerializer
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage, join_reasoning

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatOCI(ChatModelBase):
    __registry_key__ = "oci.default"

    model_id: Annotated[str, Field(description="OCI GenAI model OCID")]
    service_endpoint: Annotated[str, Field(description="OCI service endpoint URL")]
    compartment_id: Annotated[str, Field(description="OCI compartment OCID")]
    oci_provider: Annotated[str, Field(description="OCI model provider (meta/cohere/xai)")] = "meta"

    temperature: Annotated[float | None, Field(description="Sampling temperature")] = 1.0
    max_tokens: Annotated[int | None, Field(description="Maximum tokens in response")] = 600
    frequency_penalty: Annotated[float | None, Field(description="Frequency penalty")] = 0.0
    presence_penalty: Annotated[float | None, Field(description="Presence penalty")] = 0.0
    top_p: Annotated[float | None, Field(description="Top-p sampling parameter")] = 0.75
    top_k: Annotated[int | None, Field(description="Top-k sampling parameter")] = 0

    auth_type: Annotated[str, Field(description="Authentication type")] = "API_KEY"
    auth_profile: Annotated[str, Field(description="Authentication profile name")] = "DEFAULT"
    timeout: Annotated[float, Field(description="Request timeout in seconds")] = 60.0

    @property
    def model_type(self) -> str:
        return "oci"

    @property
    def name(self) -> str:
        if len(self.model_id) > 90:
            parts = self.model_id.split(".")
            if len(parts) >= 4:
                return f"oci-{self.oci_provider}-{parts[3]}"
            return f"oci-{self.oci_provider}-model"
        return self.model_id

    @property
    def model(self) -> str:
        return self.model_id

    def _uses_cohere_format(self) -> bool:
        return self.oci_provider.lower() == "cohere"

    def _get_oci_client(self) -> GenerativeAiInferenceClient:
        if not hasattr(self, "_client"):
            if self.auth_type == "INSTANCE_PRINCIPAL":
                signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
                self._client = GenerativeAiInferenceClient(
                    config={},
                    signer=signer,
                    service_endpoint=self.service_endpoint,
                    retry_strategy=oci.retry.NoneRetryStrategy(),
                    timeout=(10, 240),
                )
            elif self.auth_type == "RESOURCE_PRINCIPAL":
                signer = oci.auth.signers.get_resource_principals_signer()
                self._client = GenerativeAiInferenceClient(
                    config={},
                    signer=signer,
                    service_endpoint=self.service_endpoint,
                    retry_strategy=oci.retry.NoneRetryStrategy(),
                    timeout=(10, 240),
                )
            else:
                config = oci.config.from_file("~/.oci/config", self.auth_profile)
                self._client = GenerativeAiInferenceClient(
                    config=config,
                    service_endpoint=self.service_endpoint,
                    retry_strategy=oci.retry.NoneRetryStrategy(),
                    timeout=(10, 240),
                )
        return self._client

    def _extract_usage(self, response) -> ChatInvokeUsage | None:
        try:
            if hasattr(response, "data") and hasattr(response.data, "chat_response"):
                chat_response = response.data.chat_response
                if hasattr(chat_response, "usage"):
                    usage = chat_response.usage
                    return ChatInvokeUsage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0),
                        prompt_cached_tokens=None,
                        prompt_cache_creation_tokens=None,
                        prompt_image_tokens=None,
                        completion_tokens=getattr(usage, "completion_tokens", 0),
                        total_tokens=getattr(usage, "total_tokens", 0),
                    )
        except Exception:
            pass
        return None

    def _extract_content(self, response) -> str:
        if not hasattr(response, "data"):
            raise ModelProviderError(
                message="Invalid response format", status_code=500, model=self.name
            )
        chat_response = response.data.chat_response
        if hasattr(chat_response, "text"):
            return chat_response.text or ""
        elif hasattr(chat_response, "choices") and chat_response.choices:
            choice = chat_response.choices[0]
            return "\n".join(part.text for part in choice.message.content if hasattr(part, "text"))
        raise ModelProviderError(
            message=f"Unsupported response format: {type(chat_response).__name__}",
            status_code=500,
            model=self.name,
        )

    def _extract_reasoning(self, response) -> str | None:
        """The reasoning a generic-format reply carried (``reasoning_content``), or ``None``.

        Only the generic format's assistant message has the field; a Cohere
        reply has none. Read only when the response already has it: this
        provider asks for none.
        """
        chat_response = getattr(getattr(response, "data", None), "chat_response", None)
        choices = getattr(chat_response, "choices", None)
        if not choices:
            return None
        message = getattr(choices[0], "message", None)
        return join_reasoning([getattr(message, "reasoning_content", None)])

    async def _make_request(self, messages: Sequence[BaseMessage]):
        if self._uses_cohere_format():
            message_text = OCIRawMessageSerializer.serialize_messages_for_cohere(messages)
            chat_request = CohereChatRequest()
            chat_request.message = message_text
            chat_request.max_tokens = self.max_tokens
            chat_request.temperature = self.temperature
            chat_request.frequency_penalty = self.frequency_penalty
            chat_request.top_p = self.top_p
            chat_request.top_k = self.top_k
        else:
            oci_messages = OCIRawMessageSerializer.serialize_messages(messages)
            chat_request = GenericChatRequest()
            chat_request.api_format = BaseChatRequest.API_FORMAT_GENERIC
            chat_request.messages = oci_messages
            chat_request.max_tokens = self.max_tokens
            chat_request.temperature = self.temperature
            chat_request.top_p = self.top_p
            if self.oci_provider.lower() == "meta":
                chat_request.frequency_penalty = self.frequency_penalty
                chat_request.presence_penalty = self.presence_penalty
            elif self.oci_provider.lower() == "xai":
                chat_request.top_k = self.top_k

        serving_mode = OnDemandServingMode(model_id=self.model_id)
        chat_details = ChatDetails()
        chat_details.serving_mode = serving_mode
        chat_details.chat_request = chat_request
        chat_details.compartment_id = self.compartment_id

        def _sync_request():
            try:
                return self._get_oci_client().chat(chat_details)
            except Exception as e:
                status_code = getattr(e, "status", 500)
                if status_code == 429:
                    raise ModelRateLimitError(message=str(e), model=self.name) from e
                raise ModelProviderError(
                    message=str(e), status_code=status_code, model=self.name
                ) from e

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _sync_request)

    @overload
    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: type[T], **kwargs: Any
    ) -> ChatInvokeCompletion[T]: ...

    @property
    def supports_tools(self) -> bool:
        """OCI's raw chat requests carry no tool vocabulary in this provider."""
        return False

    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T] | None = None,
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        # This provider builds CohereChatRequest / GenericChatRequest by hand and
        # does structured output entirely by prompt. There is nowhere to put a
        # tool declaration, and no tool_use in the reply to parse back.
        self.reject_unsupported_tools(tools)

        try:
            if output_format is None:
                response = await self._make_request(messages)
                return ChatInvokeCompletion(
                    completion=self._extract_content(response),
                    thinking=self._extract_reasoning(response),
                    usage=self._extract_usage(response),
                )
            else:
                optimized_schema = SchemaOptimizer.create_optimized_json_schema(output_format)
                system_instruction = (
                    f"You must respond with ONLY a valid JSON object matching this schema:\n"
                    f"{json.dumps(optimized_schema, indent=2)}\n"
                    f"No extra text. Valid JSON only."
                )
                # Deep-copy: ``list(messages)`` is a shallow copy, so mutating
                # ``modified[0].content`` would grow the *caller's* SystemMessage by a
                # full JSON-schema block on every step — unbounded, and the mutation
                # persists into the WAL and every later provider call.
                modified: list[BaseMessage] = [m.model_copy(deep=True) for m in messages]
                if modified and isinstance(modified[0], SystemMessage):
                    head = modified[0]
                    if isinstance(head.content, str):
                        head.content = head.content + "\n\n" + system_instruction
                    else:
                        head.content = [
                            *head.content,
                            ContentPartTextParam(text="\n\n" + system_instruction),
                        ]
                else:
                    modified.insert(0, SystemMessage(content=system_instruction))

                response = await self._make_request(modified)
                wire_text = self._extract_content(response)
                text = strip_code_fence(wire_text)
                if not text.startswith("{"):
                    start = text.find("{")
                    end = text.rfind("}")
                    if start != -1 and end > start:
                        text = text[start : end + 1]

                usage = self._extract_usage(response)
                return ChatInvokeCompletion(
                    completion=self._validate_structured(
                        output_format, text, usage=usage, wire_text=wire_text
                    ),
                    thinking=self._extract_reasoning(response),
                    usage=usage,
                )

        except (ModelRateLimitError, ModelProviderError):
            raise
        except Exception as e:
            raise ModelProviderError(message=str(e), status_code=500, model=self.name) from e
