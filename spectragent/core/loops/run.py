"""Run-owned execution facts shared by Loop strategies.

Runtime resolves identity once. Instrumentation owns model-call accounting and
execution clocks; strategy-specific retry state belongs to that strategy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from spectragent.core.loops.identity import RunIdentity
from spectragent.schemas.step_record import StepRecord


@dataclass(kw_only=True)
class LoopRun:
    """Dependencies and execution facts for one invocation, never stored on a Loop."""

    agent: Any
    task: str
    env: Any
    llm: Any
    memory: Any
    act_fn: Any
    negotiated_obs: Any
    recorder: Any
    identity: RunIdentity
    step_executor: Any
    is_branch: bool = False
    step_records: list[StepRecord] = field(default_factory=list)
    action_history: list[str] = field(default_factory=list)
    prev_step_record: StepRecord | None = None
    last_answer: str = ""

    @classmethod
    def from_ctx(
        cls,
        agent: Any,
        task: str,
        ctx: Mapping[str, Any],
        *,
        step_executor: Any,
        **state: Any,
    ) -> Self:
        """Create fresh run state from dependencies captured by ``weave``."""
        return cls(
            agent=agent,
            task=task,
            env=ctx["env"],
            llm=ctx["llm"],
            memory=ctx["memory"],
            act_fn=ctx["act_fn"],
            negotiated_obs=ctx.get("negotiated_obs"),
            recorder=ctx.get("step_recorder"),
            identity=ctx.get("identity", RunIdentity()),
            step_executor=step_executor,
            **state,
        )

    @property
    def policy_steps(self) -> int:
        """Decisions made by this run's agent, excluding restored and terminal steps."""
        return sum(record.origin == "policy" for record in self.step_records)
