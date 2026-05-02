"""SimpleMemory — stores all messages up to a configurable cap."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from pydantic import Field as PydanticField

from spectragent.agents.memory._registrar import MemoryBase
from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.llm.messages import BaseMessage


@dataclass
class SimpleMemory(MemoryBase):
    """Stores all messages in a plain list, capped at *max_messages*."""

    __registry_key__ = "simple"
    supports_protocols: ClassVar[frozenset[MessageProtocol]] = frozenset({"user_assistant"})

    # ── config ─────────────────────────────────────────────────
    max_messages: Annotated[int, PydanticField(description="Maximum messages to retain")] = 1000

    # ── internal state ─────────────────────────────────────────
    _messages: list[BaseMessage] = field(default_factory=list, init=False, repr=False)

    @property
    def memory_type(self) -> str:
        return "simple"

    def add_message(self, message: BaseMessage) -> None:
        self._messages.append(message)
        if len(self._messages) > self.max_messages:
            self._messages = self._messages[-self.max_messages :]

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        for msg in messages:
            self.add_message(msg)

    def get_messages(self) -> list[BaseMessage]:
        return list(self._messages)

    def get_context_messages(self, *, max_tokens: int | None = None) -> list[BaseMessage]:
        return self.get_messages()

    def clear(self) -> None:
        self._messages.clear()

    @property
    def message_count(self) -> int:
        return len(self._messages)
