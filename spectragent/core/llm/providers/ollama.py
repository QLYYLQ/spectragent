from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar, overload

from ollama import AsyncClient as OllamaAsyncClient
from ollama import Options
from pydantic import BaseModel, Field

from spectragent.core.llm._registrar import ChatModelBase
from spectragent.core.llm.exceptions import ModelProviderError
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.serializers.ollama_format import OllamaMessageSerializer
from spectragent.core.llm.views import ChatInvokeCompletion

T = TypeVar("T", bound=BaseModel)


@dataclass
class ChatOllama(ChatModelBase):
    __registry_key__ = "ollama.default"

    model: Annotated[str, Field(description="Ollama model name")]

    # Client initialization parameters
    host: Annotated[str | None, Field(description="Ollama server host")] = None
    timeout: Annotated[float | None, Field(description="Request timeout in seconds")] = None
    client_params: dict[str, Any] | None = None
    ollama_options: Mapping[str, Any] | Options | None = None

    @property
    def model_type(self) -> str:
        return "ollama"

    @property
    def name(self) -> str:
        return self.model

    def get_client(self) -> OllamaAsyncClient:
        return OllamaAsyncClient(host=self.host, timeout=self.timeout, **self.client_params or {})

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
        ollama_messages = OllamaMessageSerializer.serialize_messages(messages)

        try:
            if output_format is None:
                response = await self.get_client().chat(
                    model=self.model,
                    messages=ollama_messages,
                    options=self.ollama_options,
                )
                return ChatInvokeCompletion(completion=response.message.content or "", usage=None)
            else:
                schema = output_format.model_json_schema()
                response = await self.get_client().chat(
                    model=self.model,
                    messages=ollama_messages,
                    format=schema,
                    options=self.ollama_options,
                )
                completion = response.message.content or ""
                if output_format is not None:
                    completion = output_format.model_validate_json(completion)
                return ChatInvokeCompletion(completion=completion, usage=None)

        except Exception as e:
            raise ModelProviderError(message=str(e), model=self.name) from e
