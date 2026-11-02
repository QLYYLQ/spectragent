"""A task's own step budget: ReAct's optional Loop input ``max_steps``.

``docs/architecture_design.md`` §4.1 "Loop 输入": a task loader may hand ReAct
a step budget of the task's own in ``TaskContext.loop`` (AndroidWorld gives
each task ``int(10 * complexity)`` steps). The configured ``max_steps`` stays
the ceiling: the run takes at most the smaller of the two. ``prepare()``
refuses a budget that is not a positive integer, before any trace opens. The
Loop stops at the budget (``step_budget``), the step-info fragment tells it to
the agent every step, and the trace records it (``TraceConfig.max_steps``).
The agent's seed limits never carry it: a branch and the training export pin
the step-info fragment to the source run's, and a seed cannot be pinned.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import BranchLoop, ReactLoop
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.validation import IncompatibleLoopInputError
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.fragments import StepInfoObs
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.restoration import Checkpoint
from tests._helpers.assembly import AGENT_ID, StartSpy, make_env, make_run


class _Cap(CapabilityBase):
    """One action, ``wait``, that never ends a run."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "step_budget_cap"
    __wiring__ = {"action": ("action", ["step_budget_cap.wait"])}

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name="step_budget_cap.wait", description="Wait")]

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        return ActionResult(success=True)


class _Env(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "step_budget_env"


class _Agent:
    """Waits every step, so only the budget ends its run; notes what it is told."""

    __execution_limit_fields__ = frozenset({"max_actions_per_step"})
    __wiring__ = {
        "action": ("action", ["step_budget_cap.wait"]),
        "observation": ("observation", ["step_info"]),
    }

    def __init__(self) -> None:
        self.limits: list[ExecutionLimits] = []
        self.told: list[tuple[int, int]] = []

    def prepare(
        self, *, task: str, action_space: ActionSpace, execution_limits: ExecutionLimits
    ) -> list[Any]:
        self.limits.append(execution_limits)
        return []

    async def build_react_prompt(self, obs: Any, *, context: ReactPromptContext) -> list[Any]:
        self.told.append((obs.step_info.step, obs.step_info.max_steps))
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, raw: Any) -> list[Action]:
        return [Action(name="step_budget_cap.wait")]


class _LLM:
    inference_kind = "chat"
    model = name = "step-budget-model"

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        class _Completion:
            completion = "wait"
            usage = None

        return _Completion()

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, msg: Any) -> None:
        self.messages.append(msg)

    def add_messages(self, msgs: Any) -> None:
        self.messages.extend(msgs)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


def _runtime(loop: Any, agent: _Agent, *, hooks: list[Any]) -> AgentRuntime:
    env = make_env(_Env, {"step_budget_cap": _Cap()})
    run = make_run(agent=agent, loop=loop, env=env, llm=_LLM(), memory=_Memory(), hooks=hooks)
    return AgentRuntime.for_agent(run, AGENT_ID)


async def _run(
    tmp_path: Any, *, configured: int, loop_inputs: dict[str, object], task_id: str = "t-1"
) -> tuple[Any, _Agent, RunTrace]:
    """A run of ``ReactLoop(max_steps=configured)`` on a task with these Loop inputs."""
    agent = _Agent()
    writer = TraceWriter(trace_dir=str(tmp_path / task_id))
    runtime = _runtime(ReactLoop(max_steps=configured), agent, hooks=[writer])
    result = await runtime.execute("wait it out", task_id=task_id, loop_inputs=loop_inputs)
    assert writer.run_dir is not None
    return result, agent, RunTrace(writer.run_dir)


class TestTheBudget:
    def test_without_a_task_budget_the_configured_one(self) -> None:
        assert ReactLoop(max_steps=30).step_budget({}) == 30

    def test_a_task_budget_lowers_it(self) -> None:
        assert ReactLoop(max_steps=30).step_budget({"max_steps": 12}) == 12

    def test_the_configured_one_is_the_ceiling(self) -> None:
        assert ReactLoop(max_steps=30).step_budget({"max_steps": 120}) == 30

    @pytest.mark.parametrize("budget", [0, -3, True, False])
    def test_a_budget_that_is_not_a_positive_integer_is_refused(self, budget: int) -> None:
        with pytest.raises(IncompatibleLoopInputError) as err:
            ReactLoop().check_loop_inputs({"max_steps": budget})
        assert err.value.refused == {"max_steps": f"must be a positive integer, got {budget!r}"}
        assert f"max_steps must be a positive integer, got {budget!r}" in str(err.value)

    @pytest.mark.parametrize(("budget", "type_name"), [("30", "str"), (12.0, "float")])
    def test_a_mistyped_budget_is_refused(self, budget: object, type_name: str) -> None:
        with pytest.raises(IncompatibleLoopInputError) as err:
            ReactLoop().check_loop_inputs({"max_steps": budget})
        assert err.value.mistyped == {"max_steps": ("int", type_name)}
        assert err.value.refused == {}

    def test_a_branch_runs_its_continuation_within_the_ceiling(self) -> None:
        assert BranchLoop(continue_steps=2, max_steps=30).step_budget({}) == 3
        assert BranchLoop(continue_steps=50, max_steps=30).step_budget({}) == 30

    def test_a_branch_takes_no_budget_from_its_task(self) -> None:
        checkpoint = Checkpoint(trace_dir="/tmp/source-run", step=1)
        with pytest.raises(IncompatibleLoopInputError) as err:
            BranchLoop().check_loop_inputs({"checkpoint": checkpoint, "max_steps": 5})
        assert err.value.unknown == ["max_steps"]


class TestARun:
    async def test_a_task_budget_ends_the_run_and_is_told_received_and_recorded(
        self, tmp_path: Any
    ) -> None:
        result, agent, trace = await _run(tmp_path, configured=30, loop_inputs={"max_steps": 3})
        assert result.agent_done == AgentDoneStatus.MAX_STEPS
        assert result.metrics.steps == 3
        # Every step tells the agent the task's budget, not the configured one;
        # the seed limits are the same whatever the task's budget.
        assert agent.told == [(1, 3), (2, 3), (3, 3)]
        assert agent.limits == [ExecutionLimits(max_actions_per_step=1)]
        assert trace.config.max_steps == 3
        assert [record.step for record in trace.step_records] == [0, 1, 2]
        told = trace.observation_fragment(2, "loop.step_info")
        assert isinstance(told, StepInfoObs) and (told.step, told.max_steps) == (3, 3)

    async def test_without_a_task_budget_the_configured_one_applies(self, tmp_path: Any) -> None:
        result, agent, trace = await _run(tmp_path, configured=2, loop_inputs={})
        assert result.agent_done == AgentDoneStatus.MAX_STEPS
        assert result.metrics.steps == 2
        assert agent.told == [(1, 2), (2, 2)]
        assert agent.limits == [ExecutionLimits(max_actions_per_step=1)]
        assert trace.config.max_steps == 2

    async def test_a_budget_above_the_ceiling_runs_the_ceiling(self, tmp_path: Any) -> None:
        result, agent, trace = await _run(tmp_path, configured=2, loop_inputs={"max_steps": 120})
        assert result.metrics.steps == 2
        assert agent.told == [(1, 2), (2, 2)]
        assert trace.config.max_steps == 2

    async def test_the_budget_is_per_task_not_configuration(self, tmp_path: Any) -> None:
        """Two tasks of one sweep differing only in their budgets share a ``config_hash``."""
        _, _, short = await _run(
            tmp_path, configured=30, loop_inputs={"max_steps": 1}, task_id="short"
        )
        _, _, long = await _run(
            tmp_path, configured=30, loop_inputs={"max_steps": 2}, task_id="long"
        )
        assert (short.config.max_steps, long.config.max_steps) == (1, 2)
        assert short.manifest is not None and long.manifest is not None
        assert short.manifest.config_hash == long.manifest.config_hash

    @pytest.mark.parametrize("budget", [0, -1, True, "12"])
    async def test_prepare_refuses_a_bad_budget_before_any_trace(
        self, tmp_path: Any, budget: object
    ) -> None:
        traces = tmp_path / "traces"
        spy = StartSpy()
        agent = _Agent()
        runtime = _runtime(ReactLoop(), agent, hooks=[spy, TraceWriter(trace_dir=str(traces))])
        with pytest.raises(IncompatibleLoopInputError, match="max_steps"):
            await runtime.execute("t", loop_inputs={"max_steps": budget})
        assert spy.starts == []
        assert agent.limits == []
        assert not traces.exists()
