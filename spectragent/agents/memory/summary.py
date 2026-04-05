"""SummaryMemory — periodically compresses older messages via LLM."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import Field as PydanticField

from spectragent.agents.memory._registrar import MemoryBase
from spectragent.core.llm.messages import BaseMessage, SystemMessage


@dataclass
class SummaryMemory(MemoryBase):
    """Keeps a running LLM-generated summary of older messages.

    Every *summary_interval* non-system messages, the oldest messages
    beyond the window are summarised into a single ``SystemMessage``
    that replaces them.

    The ``llm`` dependency is injected at runtime (not a conscribe
    config field) via :meth:`set_llm`.
    """

    __registry_key__ = "summary"

    # ── config ─────────────────────────────────────────────────
    summary_interval: Annotated[
        int, PydanticField(description="Summarise after this many new non-system messages")
    ] = 5
    max_tokens: Annotated[
        int, PydanticField(description="Max tokens for context (approx)")
    ] = 4000

    # ── internal state ─────────────────────────────────────────
    _messages: list[BaseMessage] = field(default_factory=list, init=False, repr=False)
    _summary: str | None = field(default=None, init=False, repr=False)
    _unsummarised_count: int = field(default=0, init=False, repr=False)
    _llm: Any = field(default=None, init=False, repr=False)

    def set_llm(self, llm: Any) -> None:
        """Inject the LLM used for summarisation."""
        self._llm = llm

    @property
    def memory_type(self) -> str:
        return "summary"

    def add_message(self, message: BaseMessage) -> None:
        self._messages.append(message)
        if not isinstance(message, SystemMessage):
            self._unsummarised_count += 1

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        for msg in messages:
            self.add_message(msg)

    def get_messages(self) -> list[BaseMessage]:
        return list(self._messages)

    @property
    def needs_summary(self) -> bool:
        """Whether the memory has accumulated enough messages to trigger summarisation."""
        return self._unsummarised_count >= self.summary_interval

    async def summarise(self) -> None:
        """Compress older messages into a summary.

        Requires an LLM to be set via :meth:`set_llm`.
        If no LLM is available, this is a no-op.
        """
        if self._llm is None or not self.needs_summary:
            return

        # Separate system messages from conversation
        system_msgs = [m for m in self._messages if isinstance(m, SystemMessage)]
        non_system = [m for m in self._messages if not isinstance(m, SystemMessage)]

        if len(non_system) <= self.summary_interval:
            return

        # Messages to summarise (older ones)
        to_summarise = non_system[: -self.summary_interval]
        to_keep = non_system[-self.summary_interval :]

        # Build summary prompt
        conversation = "\n".join(f"[{m.role}]: {m.text}" for m in to_summarise)
        existing = f"Previous summary: {self._summary}\n\n" if self._summary else ""

        from spectragent.core.llm.messages import UserMessage

        summary_messages: list[BaseMessage] = [
            SystemMessage(content="You are a conversation summariser. Produce a concise summary."),
            UserMessage(content=f"{existing}Summarise this conversation:\n{conversation}"),
        ]

        result = await self._llm.ainvoke(summary_messages)
        self._summary = result.completion if isinstance(result.completion, str) else str(result.completion)
        self._messages = system_msgs + to_keep
        self._unsummarised_count = 0

    def get_context_messages(self, *, max_tokens: int | None = None) -> list[BaseMessage]:
        result: list[BaseMessage] = []

        # System messages first
        for msg in self._messages:
            if isinstance(msg, SystemMessage):
                result.append(msg)

        # Inject summary if available
        if self._summary:
            result.append(SystemMessage(content=f"[Conversation Summary]\n{self._summary}"))

        # Non-system messages
        for msg in self._messages:
            if not isinstance(msg, SystemMessage):
                result.append(msg)

        return result

    def clear(self) -> None:
        self._messages.clear()
        self._summary = None
        self._unsummarised_count = 0

    @property
    def message_count(self) -> int:
        return len(self._messages)
