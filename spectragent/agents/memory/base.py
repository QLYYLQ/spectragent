"""BaseMemoryManager Protocol — the public contract for memory implementations."""

from collections.abc import Sequence
from typing import ClassVar, Protocol, runtime_checkable

from spectragent.agents.memory.protocols import MessageProtocol
from spectragent.core.llm.messages import BaseMessage


@runtime_checkable
class BaseMemoryManager(Protocol):
    """Protocol that every memory manager must satisfy.

    Memory managers store and retrieve conversation messages
    for agent reasoning.  Different strategies (sliding window,
    token budget, summary compression) implement this Protocol.
    """

    supports_protocols: ClassVar[frozenset[MessageProtocol]]

    @property
    def memory_type(self) -> str:
        """Identifier for this memory strategy."""
        ...

    def add_message(self, message: BaseMessage) -> None:
        """Append a single message to the memory store."""
        ...

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        """Append multiple messages to the memory store."""
        ...

    def get_messages(self) -> list[BaseMessage]:
        """Return all stored messages (unfiltered)."""
        ...

    def get_context_messages(self, *, max_tokens: int | None = None) -> list[BaseMessage]:
        """Return messages suitable for LLM context, possibly truncated.

        Parameters
        ----------
        max_tokens:
            Optional token budget.  Implementations may ignore this
            if their strategy has its own limit.
        """
        ...

    def clear(self) -> None:
        """Remove all messages from the store."""
        ...

    @property
    def message_count(self) -> int:
        """Number of messages currently stored."""
        ...
