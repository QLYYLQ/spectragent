"""Typed execution limits supplied by a Loop and consumed by an Agent."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True)
class ExecutionLimits:
    """Execution policy an agent may need to describe in its domain prompt.

    ``None`` means the Loop does not provide that limit. The consuming agent
    declares the fields it needs in ``__execution_limit_fields__``; an explicit
    empty declaration says none of this Loop's limits apply to its prompts.
    """

    max_actions_per_step: int | None = None

    def __post_init__(self) -> None:
        value = self.max_actions_per_step
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("max_actions_per_step must be a non-negative integer or None")


class IncompatibleExecutionLimitsError(Exception):
    """The agent's declared consumption does not match the Loop's limits."""

    def __init__(self, *, agent_name: str, loop_id: str, detail: str) -> None:
        self.agent_name = agent_name
        self.loop_id = loop_id
        super().__init__(f"Agent {agent_name!r} and loop {loop_id!r} execution limits: {detail}")


def validate_execution_limits(agent: Any, loop_id: str, limits: ExecutionLimits) -> bool:
    """Check the explicit consumption contract; return whether to pass limits.

    A named ``execution_limits`` parameter is required for agents consuming
    any fields. ``**kwargs`` cannot stand in for the declaration or parameter.
    An agent with an explicit empty declaration may keep a narrow ``prepare``
    signature because it deliberately consumes no limits.
    """
    agent_name = type(agent).__name__

    def incompatible(detail: str) -> IncompatibleExecutionLimitsError:
        return IncompatibleExecutionLimitsError(
            agent_name=agent_name, loop_id=loop_id, detail=detail
        )

    if not isinstance(limits, ExecutionLimits):
        raise incompatible("execution_limits() must return ExecutionLimits")
    names = {field.name for field in fields(limits)}
    provided = {name for name in names if getattr(limits, name) is not None}
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
    if not callable(prepare):
        raise incompatible("consuming limits requires a callable prepare method")
    parameter = inspect.signature(prepare).parameters.get("execution_limits")
    if parameter is None or parameter.kind not in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    ):
        raise incompatible("prepare must explicitly accept execution_limits by keyword")
    return True
