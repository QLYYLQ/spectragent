"""A scripted chat model: the judge's model through the public chat Protocol, without a network."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, TypeVar, cast, overload

from pydantic import BaseModel, JsonValue, ValidationError

from lookahead.judge import JudgeModel
from spectragent.core.llm.base import BaseChatModel
from spectragent.core.llm.exceptions import StructuredOutputValidationError
from spectragent.core.llm.fences import strip_code_fence
from spectragent.core.llm.lifecycle import ClientCleanupDiagnostic
from spectragent.core.llm.messages import BaseMessage, ContentPartImageParam, UserMessage
from spectragent.core.llm.tools import ToolChoice, ToolDefinition
from spectragent.core.llm.views import ChatInvokeCompletion, ModelInvokeUsage

T = TypeVar("T", bound=BaseModel)

USAGE = ModelInvokeUsage(
    prompt_tokens=100,
    prompt_cached_tokens=None,
    prompt_cache_creation_tokens=None,
    prompt_image_tokens=None,
    completion_tokens=20,
    completion_reasoning_tokens=5,
    total_tokens=120,
)
#: What every reply's reasoning reads.
THINKING = "Looking at the screen first."
#: The redacted configuration a scripted judge model records.
CONFIG: dict[str, JsonValue] = {
    "model_type": "fake",
    "max_completion_tokens": 16384,
    "api_key": "<omitted>",
}

#: A reply's text, or an exception the call raises.
Script = Callable[[list[BaseMessage]], "str | Exception"]


def prompt_text(messages: Sequence[BaseMessage]) -> str:
    """Every text of the messages, joined."""
    return "\n".join(message.text for message in messages if hasattr(message, "text"))


def images(messages: Sequence[BaseMessage]) -> list[ContentPartImageParam]:
    return [
        part
        for message in messages
        if isinstance(message, UserMessage) and not isinstance(message.content, str)
        for part in message.content
        if isinstance(part, ContentPartImageParam)
    ]


class ScriptedChatModel:
    """Answers each call with what *script* returns for its messages.

    Validates a structured reply as a provider does (``validate_structured``):
    a fence is removed, and a reply that does not fit raises
    ``StructuredOutputValidationError`` with the unwrapped text and the usage.
    """

    inference_kind: Literal["chat"] = "chat"
    model: str = "fake-judge"
    model_type: str = "fake"
    provider: str = "test"
    name: str = "fake judge"
    supports_tools: bool = False

    def __init__(self, script: Script, *, delay: float = 0.0) -> None:
        self.script = script
        self.delay = delay
        self.calls: list[list[BaseMessage]] = []
        self.formats: list[type[BaseModel] | None] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self.closed = 0

    @overload
    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: None = None,
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: object,
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T],
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: object,
    ) -> ChatInvokeCompletion[T]: ...

    async def ainvoke(
        self,
        messages: Sequence[BaseMessage],
        output_format: type[T] | None = None,
        *,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto",
        **kwargs: object,
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        self.calls.append(list(messages))
        self.formats.append(output_format)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            result = self.script(list(messages))
        finally:
            self.in_flight -= 1
        if isinstance(result, Exception):
            raise result
        if output_format is None:
            return ChatInvokeCompletion(completion=result, usage=USAGE, stop_reason="stop")
        body = strip_code_fence(result.strip())
        try:
            value = output_format.model_validate_json(body)
        except ValidationError as exc:
            raise StructuredOutputValidationError(
                f"{self.name} returned a reply that does not match {output_format.__name__}: {exc}",
                raw_output=body,
                model=self.model,
                usage=USAGE,
            ) from exc
        return ChatInvokeCompletion(
            completion=value, thinking=THINKING, usage=USAGE, stop_reason="stop"
        )

    async def aclose(self, *, timeout: float = 5.0) -> tuple[ClientCleanupDiagnostic, ...]:
        self.closed += 1
        return ()


def judge_model(llm: ScriptedChatModel, source: Path = Path("judge.yaml")) -> JudgeModel:
    """*llm* as the judge's model, as if composed from *source*."""
    # The Protocol also declares pydantic's schema hook, which a fake never needs.
    return JudgeModel(llm=cast(BaseChatModel, llm), source=source, config=dict(CONFIG))


def loader(llm: ScriptedChatModel) -> Callable[[Path], JudgeModel]:
    """A ``load_judge`` for the CLI that hands out *llm* for any configuration file."""
    return lambda path: judge_model(llm, path)


def refuse_loader(path: Path) -> JudgeModel:
    raise AssertionError(f"no judge model may be composed (asked for {path})")


def answers(**values: str) -> str:
    """A judgment reply: ``question=answer`` pairs, each with a reason."""
    return json.dumps(
        {
            name: {"answer": answer, "reason": f"{name} is {answer}."}
            for name, answer in values.items()
        }
    )
