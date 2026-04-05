"""BaseChatModel Protocol — the public contract for all LLM chat implementations."""

from collections.abc import Sequence
from typing import Any, Protocol, TypeVar, overload, runtime_checkable

from pydantic import BaseModel

from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.views import ChatInvokeCompletion

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class BaseChatModel(Protocol):
    _verified_api_keys: bool = False

    model: str

    @property
    def model_type(self) -> str: ...

    @property
    def provider(self) -> str:
        """Optional sub-provider within a model_type (e.g. 'azure' under openai)."""
        return ""

    @property
    def name(self) -> str: ...

    @property
    def model_name(self) -> str:
        # for legacy support
        return self.model

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
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]: ...

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: type,
        handler: Any,
    ) -> Any:
        """Allow this Protocol to be used in Pydantic models."""
        from pydantic_core import core_schema

        return core_schema.any_schema()
