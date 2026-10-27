"""SimpleMemory — stores all messages up to a configurable cap."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from pydantic import Field as PydanticField

from spectragent.agents.memory._registrar import MemoryBase
from spectragent.agents.memory._tool_pairing import drop_broken_tool_groups
from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.llm.messages import BaseMessage, SystemMessage


@dataclass
class SimpleMemory(MemoryBase):
    """Stores all messages in a plain list, capped at *max_messages*.

    When *keep_system* is ``True`` (default), system messages are exempt from
    eviction.  Without that guard a long run silently drops the system prompt —
    and with it the agent's action list — producing cascading parse errors with
    nothing pointing back at memory.

    This strategy deliberately does **not** advertise ``tool_call``. Its
    ``max_messages`` eviction can cut a tool-call group in half exactly as a
    sliding window can, and while ``get_context_messages`` repairs that
    defensively via :func:`drop_broken_tool_groups`, "repair" here means
    *dropping* the broken group — acceptable as a safety net, not as a claimed
    capability. Use ``sliding_window`` or ``token_budget`` for function calling;
    both keep groups intact by construction.
    """

    __registry_key__ = "simple"
    supports_protocols: ClassVar[frozenset[MessageProtocol]] = frozenset({"user_assistant"})

    # ── config ─────────────────────────────────────────────────
    max_messages: Annotated[int, PydanticField(description="Maximum messages to retain")] = 1000
    keep_system: Annotated[bool, PydanticField(description="Never evict system messages")] = True

    # ── internal state ─────────────────────────────────────────
    _messages: list[BaseMessage] = field(default_factory=list, init=False, repr=False)

    def _evict(self) -> None:
        """Trim the store back to *max_messages*, oldest non-system first."""
        if len(self._messages) <= self.max_messages:
            return

        if not self.keep_system:
            self._messages = self._messages[-self.max_messages :]
            return

        # Budget left for non-system messages once every system message is kept.
        n_system = sum(1 for m in self._messages if isinstance(m, SystemMessage))
        budget = self.max_messages - n_system
        if budget <= 0:
            self._messages = [m for m in self._messages if isinstance(m, SystemMessage)]
            return

        non_system_positions = [
            i for i, m in enumerate(self._messages) if not isinstance(m, SystemMessage)
        ]
        dropped = set(non_system_positions[:-budget])
        self._messages = [m for i, m in enumerate(self._messages) if i not in dropped]

    def add_message(self, message: BaseMessage) -> None:
        self._messages.append(message)
        self._evict()

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        self._messages.extend(messages)
        self._evict()

    def get_context_messages(self) -> list[BaseMessage]:
        return drop_broken_tool_groups(self._messages)

    def clear(self) -> None:
        self._messages.clear()
