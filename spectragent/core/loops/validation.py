"""Compatibility validation — checks Agent satisfies Loop's required slots.

Slot validation is implemented in ``AgentLoopBase._validate_slots()``.
This module provides the ``IncompatibleError`` type and a thin
``validate_compatibility`` wrapper for callers that prefer a free-function
interface.

See docs/execution_dag.md M03 "兼容性验证".
"""

from __future__ import annotations

from typing import Any


class IncompatibleError(Exception):
    """Raised when an Agent is missing slots required by a Loop."""

    def __init__(
        self,
        agent_name: str,
        loop_id: str,
        missing_slots: list[str],
    ) -> None:
        self.agent_name = agent_name
        self.loop_id = loop_id
        self.missing_slots = missing_slots
        super().__init__(
            f"Agent '{agent_name}' is missing slots required by "
            f"loop '{loop_id}': {', '.join(missing_slots)}"
        )


def validate_compatibility(agent: Any, loop: Any) -> None:
    """Thin wrapper — delegates to ``loop._validate_slots(agent)``.

    Raises:
        IncompatibleError: If the agent is missing required slots.
    """
    loop._validate_slots(agent)
