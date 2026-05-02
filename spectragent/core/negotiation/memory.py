"""Loop ↔ Memory message protocol negotiation.

A Loop declares the message protocols it requires (e.g. ``user_assistant``,
``tool_call``); a Memory implementation declares the protocols it supports.
Negotiation enforces ``loop_required ⊆ memory_supported`` at prepare time.

See docs/execution_dag.md §M17.
"""

from __future__ import annotations

from typing import Any


class IncompatibleMemoryError(Exception):
    """Raised when Memory does not support all protocols required by Loop."""

    def __init__(
        self,
        memory_id: str,
        loop_id: str,
        missing_protocols: list[str],
    ) -> None:
        self.memory_id = memory_id
        self.loop_id = loop_id
        self.missing_protocols = missing_protocols
        super().__init__(
            f"Memory '{memory_id}' does not support protocols required by "
            f"loop '{loop_id}': {', '.join(missing_protocols)}"
        )


def negotiate_memory(loop: Any, memory: Any) -> None:
    """Validate that *memory* supports every protocol required by *loop*."""
    required = getattr(type(loop), "__required_message_protocols__", frozenset())
    if not required:
        return
    supported = getattr(type(memory), "supports_protocols", frozenset())
    missing = sorted(required - supported)
    if missing:
        raise IncompatibleMemoryError(
            memory_id=getattr(memory, "memory_id", type(memory).__name__),
            loop_id=getattr(
                loop,
                "loop_id",
                getattr(type(loop), "__registry_key__", type(loop).__name__),
            ),
            missing_protocols=missing,
        )
