"""Compatibility validation — checks Agent satisfies Loop's required slots.

Slot validation is implemented in ``AgentLoopBase._validate_slots()``.
This module provides the ``IncompatibleError`` type, the loop-input and
checkpoint errors a Loop raises at assembly (``IncompatibleLoopInputError``,
``IncompatibleCheckpointError``), and a thin ``validate_compatibility``
wrapper for callers that prefer a free-function interface.

See docs/execution_dag.md M03 "兼容性验证".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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


class IncompatibleLoopInputError(Exception):
    """The task's loop inputs do not fit what the Loop declares in ``__loop_inputs__``.

    Raised by ``AgentLoopBase.check_loop_inputs`` — at ``AgentRuntime.prepare()``
    right after slot validation, before any negotiation, weaving or setup, and
    again by ``weave()`` for a caller that weaves by hand. Every problem is
    reported at once: keys the Loop does not accept (a ``checkpoint`` handed to
    ``react`` must not quietly run the task from scratch), required keys the
    task did not supply, and values of the wrong type.

    Attributes:
        loop_id: The Loop that refused the inputs.
        unknown: Keys the Loop does not declare, sorted.
        missing: Required keys absent from the inputs, sorted.
        mistyped: Key -> (expected type name, actual type name).
    """

    def __init__(
        self,
        loop_id: str,
        *,
        unknown: Iterable[str] = (),
        missing: Iterable[str] = (),
        mistyped: Mapping[str, tuple[str, str]] | None = None,
        accepted: Iterable[str] = (),
    ) -> None:
        self.loop_id = loop_id
        self.unknown = sorted(unknown)
        self.missing = sorted(missing)
        self.mistyped = dict(sorted((mistyped or {}).items()))
        accepted_keys = sorted(accepted)
        parts: list[str] = []
        if self.unknown:
            declared = ", ".join(accepted_keys) if accepted_keys else "none"
            parts.append(f"does not accept {', '.join(self.unknown)} (it declares: {declared})")
        if self.missing:
            parts.append(f"requires {', '.join(self.missing)}, which the task did not supply")
        for key, (expected, actual) in self.mistyped.items():
            parts.append(f"expects {key} to be {expected}, got {actual}")
        super().__init__(f"Loop {loop_id!r} loop inputs: " + "; ".join(parts))


class IncompatibleCheckpointError(Exception):
    """A checkpoint this assembly cannot branch from.

    Raised by ``BranchLoop`` while it is being woven — inside ``prepare()``,
    before ``on_run_start`` and before any environment setup — when the source
    trace cannot be resumed (a step not committed, a run that was itself a
    branch …) or was recorded by a different agent configuration; and at run
    time, before any action, when the seeded system prompt or the replayed
    context differs from the source's. Every problem found is reported at once.

    Attributes:
        source: The source run (its run id, or its directory).
        problems: Why the source cannot be branched, one sentence each.
        mismatches: Name -> (source value, current value) for every recorded
            setting the current assembly does not reproduce.
    """

    def __init__(
        self,
        source: str,
        problems: Iterable[str] = (),
        *,
        mismatches: Mapping[str, tuple[Any, Any]] | None = None,
    ) -> None:
        self.source = source
        self.problems = list(problems)
        self.mismatches = dict(mismatches or {})
        lines = list(self.problems) + [
            f"{name}: source {recorded!r}, current {current!r}"
            for name, (recorded, current) in sorted(self.mismatches.items())
        ]
        super().__init__(f"Cannot branch from {source}: " + "; ".join(lines))


def validate_compatibility(agent: Any, loop: Any) -> None:
    """Thin wrapper — delegates to ``loop._validate_slots(agent)``.

    Raises:
        IncompatibleError: If the agent is missing required slots.
    """
    loop._validate_slots(agent)
