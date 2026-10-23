"""Run-owned execution facts shared by Loop strategies.

Runtime resolves identity once. Instrumentation owns model-call accounting and
execution clocks; strategy-specific retry state belongs to that strategy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, Self, TypeVar

from spectragent.core.loops.identity import RunIdentity
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace

if TYPE_CHECKING:
    from spectragent.agents.memory.base import BaseMemoryManager
    from spectragent.core.environments.base import Environment
    from spectragent.core.llm.base import BaseChatModel
    from spectragent.core.step_recorder import StepSink
    from spectragent.schemas.agent_result import AgentResult
    from spectragent.schemas.restoration import RestorationCheck
from spectragent.schemas.step_record import StepRecord

RunState = TypeVar("RunState", bound="LoopRun", contravariant=True)


class StepExecutor(Protocol[RunState]):
    """Public boundaries woven by Runtime, parameterized by a strategy's run state."""

    async def execute_step(
        self,
        run: RunState,
        step_idx: int,
        *,
        observation: Any = None,
        restoration: RestorationCheck | None = None,
    ) -> AgentResult | None:
        """Execute one strategy step, returning a decided result when it ends the run."""
        ...

    async def observe_terminal(
        self, env: Environment, negotiated_obs: Any, *, step: int
    ) -> tuple[StepRecord, Any]:
        """Observe the environment after a decided result without calling the policy."""
        ...


@dataclass(kw_only=True)
class LoopRun:
    """Dependencies and execution facts for one invocation, never stored on a Loop."""

    agent: Any
    task: str
    env: Environment
    llm: BaseChatModel
    memory: BaseMemoryManager
    act_fn: Callable[[Action], Awaitable[ActionResult]]
    #: What ``act_fn`` executes: the run's effective action space, the one
    #: the prompt advertises (``AgentRuntime.action_space``).
    action_space: ActionSpace
    negotiated_obs: Any
    recorder: StepSink | None
    identity: RunIdentity
    step_executor: StepExecutor[Self]
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
        step_executor: StepExecutor[Self],
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
            action_space=ctx["action_space"],
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
