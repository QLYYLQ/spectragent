"""Typed execution limits supplied by a Loop and consumed by an Agent.

Beside them, the optional check of the run's settings that
``AgentRuntime.prepare()`` makes (:class:`RunSettingsCheck`), and the refusal
an agent whose prompt states the Loop's action cap shares
(:func:`actions_per_step`).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, fields
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecutionLimits:
    """Execution policy an agent may need to describe in its domain prompt, or check.

    Each limit is the Loop's setting. ``None`` means the Loop does not provide
    that limit, unless ``unbounded`` names it: then the Loop provides it and
    sets no bound (ReAct's ``max_consecutive_parse_errors: null``). The
    consuming agent declares the fields it needs in
    ``__execution_limit_fields__``; an explicit empty declaration says none of
    this Loop's limits apply to it.
    """

    max_actions_per_step: int | None = None
    parse_retries_per_step: int | None = None
    max_consecutive_parse_errors: int | None = None
    unbounded: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in self.limit_names():
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or None")
        unset = {name for name in self.limit_names() if getattr(self, name) is None}
        if not self.unbounded <= unset:
            stray = ", ".join(sorted(self.unbounded - unset))
            raise ValueError(f"unbounded names only limits left None, not {stray}")

    @classmethod
    def limit_names(cls) -> frozenset[str]:
        """Every limit a Loop may provide."""
        return frozenset(field.name for field in fields(cls)) - {"unbounded"}

    def provided(self) -> frozenset[str]:
        """The limits this Loop provides: the ones it sets, and the unbounded ones."""
        set_ones = {name for name in self.limit_names() if getattr(self, name) is not None}
        return frozenset(set_ones) | self.unbounded


@runtime_checkable
class RunSettingsCheck(Protocol):
    """Optional, for an Agent: refuse in ``AgentRuntime.prepare()`` run settings it cannot work with.

    An Agent that only works under some settings of its run (M3A: the
    Loop's ``max_actions_per_step`` at 2 or more, and a memory that keeps
    one message) is handed the Loop's execution limits right after they are
    checked against its ``__execution_limit_fields__``, and reads its own
    configured parts (its memory) off itself. A misfit is then refused before
    anything is negotiated, before a trace is opened and before the
    environment is set up (AGENTS.md §2.3), so ``spectragent config check``
    and ``spectragent bench``'s pre-flight refuse it too, not
    ``seed_messages`` at the start of every run. An Agent without it is not
    asked.
    """

    def check_run_settings(self, execution_limits: ExecutionLimits) -> None:
        """Raise ``ValueError`` naming the setting, its value and what this Agent needs.

        Reads only the limits its ``__execution_limit_fields__`` declares,
        and its own configuration; writes no state.
        """
        ...


def actions_per_step(execution_limits: ExecutionLimits, *, agent: str) -> int:
    """The Loop's ``max_actions_per_step``, for *agent*, whose prompt states it.

    The refusal its ``check_run_settings`` makes in ``AgentRuntime.prepare()``,
    and that its ``prepare`` seed slot makes again for the number it writes.
    A cap below 1 is the Loop's configuration to refuse (ReAct's field is at
    least 1); a Loop that provides the cap unbounded is its code, which no
    configuration schema sees.

    Raises:
        ValueError: The Loop provides the cap unbounded: a prompt cannot
            promise a number of actions per reply that is no number.
    """
    limit = execution_limits.max_actions_per_step
    if limit is None:
        raise ValueError(
            f"{agent}'s prompt states how many actions a reply may run, so it needs a Loop that "
            f"sets max_actions_per_step; this one leaves it unbounded"
        )
    return limit


class IncompatibleExecutionLimitsError(Exception):
    """The agent's declared consumption does not match the Loop's limits."""

    def __init__(self, *, agent_name: str, loop_id: str, detail: str) -> None:
        self.agent_name = agent_name
        self.loop_id = loop_id
        super().__init__(f"Agent {agent_name!r} and loop {loop_id!r} execution limits: {detail}")


def validate_execution_limits(agent: Any, loop_id: str, limits: ExecutionLimits) -> bool:
    """Check the explicit consumption contract; return whether ``prepare`` takes the limits.

    An agent consuming any fields reads them in its ``prepare`` seed slot,
    which then names an ``execution_limits`` parameter, or in its
    ``check_run_settings`` (:class:`RunSettingsCheck`), or both.
    ``**kwargs`` cannot stand in for the declaration or parameter. An agent
    with an explicit empty declaration may keep a narrow ``prepare``
    signature because it deliberately consumes no limits.
    """
    agent_name = type(agent).__name__

    def incompatible(detail: str) -> IncompatibleExecutionLimitsError:
        return IncompatibleExecutionLimitsError(
            agent_name=agent_name, loop_id=loop_id, detail=detail
        )

    if not isinstance(limits, ExecutionLimits):
        raise incompatible("execution_limits() must return ExecutionLimits")
    names = limits.limit_names()
    provided = limits.provided()
    declared = getattr(type(agent), "__execution_limit_fields__", None)
    if declared is None:
        if provided:
            raise incompatible(
                "declare __execution_limit_fields__ explicitly; use an empty frozenset "
                "when these limits do not apply"
            )
        return False
    if not isinstance(declared, (set, frozenset)) or any(
        not isinstance(name, str) for name in declared
    ):
        raise incompatible("__execution_limit_fields__ must be a set of field names")
    unknown = declared - names
    if unknown:
        raise incompatible(f"unknown consumed fields: {', '.join(sorted(unknown))}")
    missing = declared - provided
    if missing:
        raise incompatible(f"loop does not provide: {', '.join(sorted(missing))}")
    if not declared:
        return False
    prepare = getattr(agent, "prepare", None)
    parameter = (
        inspect.signature(prepare).parameters.get("execution_limits") if callable(prepare) else None
    )
    takes = parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )
    if not takes and not isinstance(agent, RunSettingsCheck):
        raise incompatible(
            "prepare must explicitly accept execution_limits by keyword, or the agent must "
            "check them in check_run_settings"
        )
    return takes
