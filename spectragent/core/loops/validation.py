"""Compatibility validation — checks Agent satisfies Loop's required slots.

Slot validation is implemented in ``AgentLoopBase.validate_slots()``.
This module provides the ``IncompatibleError`` type, the loop-input and
checkpoint errors a Loop raises at assembly (``IncompatibleLoopInputError``,
``IncompatibleCheckpointError``), the error for a Loop that needs an
environment the run lacks (``MissingEnvironmentError``), and a thin
``validate_compatibility`` wrapper for callers that prefer a free-function
interface.

See docs/execution_dag.md M03 "兼容性验证".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class IncompatibleError(Exception):
    """Raised when an Agent is missing slots a Loop's user needs.

    At run assembly that is the Loop itself (``__required_slots__``). An
    optional slot is checked by whoever calls it, right before it does
    (arch §4.1 "可选 slot"): the training export needs the ReAct optional
    slots ``format_react_output`` / ``parse_react_thought``, and says so with
    *needed_for*, so a missing optional slot never reads like a Loop's
    required one.

    Attributes:
        agent_name: The agent missing the slots.
        loop_id: The Loop the slots belong to.
        missing_slots: The slot names, in the order given.
        needed_for: The phrase naming who needs them — by default
            ``required by loop '<loop_id>'``.
    """

    def __init__(
        self,
        agent_name: str,
        loop_id: str,
        missing_slots: list[str],
        *,
        needed_for: str | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.loop_id = loop_id
        self.missing_slots = missing_slots
        self.needed_for = needed_for or f"required by loop '{loop_id}'"
        super().__init__(
            f"Agent '{agent_name}' is missing slots {self.needed_for}: {', '.join(missing_slots)}"
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


class MissingEnvironmentError(Exception):
    """The Loop needs an environment (``__requires_env__``) and the run has none.

    Raised by ``AgentRuntime.prepare()`` right after the loop inputs are
    checked, before any negotiation. A Loop declaring
    ``__requires_env__ = False`` runs env-less instead: the missing
    environment then provides nothing, and an agent that needs a fragment or
    an action from one is refused by its negotiation.

    Attributes:
        loop_id: The Loop that needs the environment.
    """

    def __init__(self, loop_id: str) -> None:
        self.loop_id = loop_id
        super().__init__(
            f"Loop {loop_id!r} requires an environment (__requires_env__ = True), but this "
            f"run has none: add an environment block to the configuration, or use a Loop "
            f"that declares __requires_env__ = False."
        )


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
