"""SlidingWindowMemory — keeps the most recent *window_size* messages."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from pydantic import Field as PydanticField

from spectragent.agents.memory._registrar import MemoryBase
from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.llm.messages import BaseMessage, SystemMessage


@dataclass
class SlidingWindowMemory(MemoryBase):
    """Retains the last *window_size* non-system messages.

    When *keep_system* is ``True`` (default), system messages are always
    included at the front regardless of window limits.
    """

    __registry_key__ = "sliding_window"
    supports_protocols: ClassVar[frozenset[MessageProtocol]] = frozenset(
        {"user_assistant", "tool_call"}
    )

    # ── config ─────────────────────────────────────────────────
    window_size: Annotated[int, PydanticField(description="Number of recent messages to keep")] = 20
    keep_system: Annotated[bool, PydanticField(description="Always keep system messages")] = True

    # ── internal state ─────────────────────────────────────────
    _messages: list[BaseMessage] = field(default_factory=list, init=False, repr=False)

    @property
    def memory_type(self) -> str:
        return "sliding_window"

    def add_message(self, message: BaseMessage) -> None:
        self._messages.append(message)

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        self._messages.extend(messages)

    def get_messages(self) -> list[BaseMessage]:
        return list(self._messages)

    def get_context_messages(self, *, max_tokens: int | None = None) -> list[BaseMessage]:
        if not self._messages:
            return []

        if self.keep_system:
            system_msgs = [m for m in self._messages if isinstance(m, SystemMessage)]
            non_system = [m for m in self._messages if not isinstance(m, SystemMessage)]
            windowed = non_system[-self.window_size :] if len(non_system) > self.window_size else non_system
            return system_msgs + windowed

        return self._messages[-self.window_size :] if len(self._messages) > self.window_size else list(self._messages)

    def clear(self) -> None:
        self._messages.clear()

    @property
    def message_count(self) -> int:
        return len(self._messages)
