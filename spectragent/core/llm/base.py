"""Public inference contracts: shared identity, separate typed invocation protocols."""

from collections.abc import Sequence
from typing import Any, Literal, Protocol, TypeVar, overload, runtime_checkable

from pydantic import BaseModel

from spectragent.core.llm.decision import DecisionCapabilities, DecisionRequest, DecisionResult
from spectragent.core.llm.lifecycle import ClientCleanupDiagnostic, ModelClientLifecycle
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion

T = TypeVar("T", bound=BaseModel)


def require_inference_protocol(
    model: object, kind: Literal["chat", "decision"], *, consumer: str
) -> None:
    """Validate explicit protocol declarations on direct-Python consumer paths."""
    actual = getattr(model, "inference_kind", None)
    if actual != kind or not callable(getattr(model, "ainvoke", None)):
        raise TypeError(
            f"{consumer} requires the {kind!r} inference protocol; got {actual!r} from {type(model).__name__}"
        )


@runtime_checkable
class BaseInferenceModel(ModelClientLifecycle, Protocol):
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
    def inference_kind(self) -> Literal["chat", "decision"]: ...


@runtime_checkable
class BaseDecisionModel(BaseInferenceModel, Protocol):
    @property
    def inference_kind(self) -> Literal["decision"]: ...

    @property
    def capabilities(self) -> DecisionCapabilities: ...

    async def ainvoke(self, request: DecisionRequest) -> DecisionResult: ...


@runtime_checkable
class BaseChatModel(BaseInferenceModel, Protocol):
    @property
    def inference_kind(self) -> Literal["chat"]: ...

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

    async def aclose(self, *, timeout: float = 5.0) -> tuple[ClientCleanupDiagnostic, ...]:
        """End this execution's client use without changing any model-call outcome.

        Implementations release only owned resources, report cleanup failures,
        leave borrowed transports open, and never create a client for cleanup.
        Subsequent executions create fresh owned clients. Cancellation of the
        caller propagates while already-started bounded cleanup continues.
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
