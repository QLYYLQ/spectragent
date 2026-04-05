"""TokenBudgetMemory — keeps recent messages within a token budget."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import Field as PydanticField

from spectragent.agents.memory._registrar import MemoryBase
from spectragent.core.llm.messages import BaseMessage, SystemMessage


def _estimate_tokens_char_ratio(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


@dataclass
class TokenBudgetMemory(MemoryBase):
    """Keeps the most recent messages that fit within *max_tokens*.

    System messages are always included first (their tokens are
    subtracted from the budget).  Remaining budget is filled with
    the most recent non-system messages, newest first.
    """

    __registry_key__ = "token_budget"

    # ── config ─────────────────────────────────────────────────
    max_tokens: Annotated[int, PydanticField(description="Token budget for context messages")] = 8000
    token_estimator: Annotated[
        str, PydanticField(description="Token estimation method: 'char_ratio'")
    ] = "char_ratio"

    # ── internal state ─────────────────────────────────────────
    _messages: list[BaseMessage] = field(default_factory=list, init=False, repr=False)

    @property
    def memory_type(self) -> str:
        return "token_budget"

    def _estimate_tokens(self, message: BaseMessage) -> int:
        text = message.text if hasattr(message, "text") else str(message.content)
        return _estimate_tokens_char_ratio(text)

    def add_message(self, message: BaseMessage) -> None:
        self._messages.append(message)

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        self._messages.extend(messages)

    def get_messages(self) -> list[BaseMessage]:
        return list(self._messages)

    def get_context_messages(self, *, max_tokens: int | None = None) -> list[BaseMessage]:
        budget = max_tokens if max_tokens is not None else self.max_tokens
        if not self._messages:
            return []

        # Always include system messages first
        system_msgs: list[BaseMessage] = []
        non_system: list[BaseMessage] = []
        for msg in self._messages:
            if isinstance(msg, SystemMessage):
                system_msgs.append(msg)
            else:
                non_system.append(msg)

        used = sum(self._estimate_tokens(m) for m in system_msgs)

        # Fill remaining budget from most recent non-system messages
        selected: list[BaseMessage] = []
        for msg in reversed(non_system):
            cost = self._estimate_tokens(msg)
            if used + cost > budget:
                break
            selected.append(msg)
            used += cost

        selected.reverse()
        return system_msgs + selected

    def clear(self) -> None:
        self._messages.clear()

    @property
    def message_count(self) -> int:
        return len(self._messages)
