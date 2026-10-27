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

    def add_message(self, message: BaseMessage) -> None:
        """Append a single message to the memory store."""
        ...

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        """Append multiple messages to the memory store."""
        ...

    def get_context_messages(self) -> list[BaseMessage]:
        """Return messages suitable for LLM context, possibly truncated."""
        ...

    def clear(self) -> None:
        """Remove all messages from the store."""
        ...
