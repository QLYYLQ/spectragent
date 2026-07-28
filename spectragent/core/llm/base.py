"""BaseChatModel Protocol — the public contract for all LLM chat implementations."""

from collections.abc import Sequence
from typing import Any, Protocol, TypeVar, overload, runtime_checkable

from pydantic import BaseModel

from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
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

    @property
    def supports_tools(self) -> bool:
        """Whether this provider can execute native function calling.

        Consulted by :meth:`reject_unsupported_tools`. A provider that returns
        ``False`` raises on ``tools=`` rather than dropping the argument, so an
        unsupported combination surfaces at the call rather than as a model that
        mysteriously never calls a tool.
        """
        return True

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
        """Invoke the model.

        Parameters
        ----------
        tools:
            Tools the model may call. Keyword-only: ``output_format`` is already
            positional at several call sites, and a third positional slot is
            taken by at least one provider. When the model calls a tool the
            calls come back on :attr:`ChatInvokeCompletion.tool_calls`.
        tool_choice:
            How the model should choose between answering and calling; ignored
            when *tools* is empty.
        """
        ...

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: type,
        handler: Any,
    ) -> Any:
        """Allow this Protocol to be used in Pydantic models."""
        from pydantic_core import core_schema

        return core_schema.any_schema()
