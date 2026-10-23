"""``ReactLoop.terminal_observation``: one more look at the world after the run ended.

``docs/architecture_design.md`` §4.1 "终止观测". Off by default, and then a
run's records are exactly what they were. On, the run's result is decided
first; one more ``env.observe`` (one more cycle, env-source fragments only)
is recorded as an ``origin="terminal"`` step numbered after the last record
— on done, ``max_steps`` and the last tolerated parse error, never on an
infrastructure failure, a failed action or a veto. A failed terminal observe
is recorded and the outcome stands; a veto on it outranks the outcome; a
failure to record it is appended to a failing result only; a cancellation
of it carries the outcome.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, Literal

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops.errors import ParseError, RunAbort, carried_outcome
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import ObservationNegotiationResult
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.observation._registrar import FragmentBase
from tests._helpers.assembly import make_env

_FRAG = "test_terminal_frame"


class _Frame(FragmentBase):
    __registry_key__ = _FRAG
    __source__: ClassVar[Literal["env", "loop"]] = "env"
    #: Which observe cycle sampled this frame.
    cycle: int


class _CycleCap(CapabilityBase):
    """Serves each fragment once per cycle, like the browser capability.

    ``cycles`` counts the observe cycles the environment announced; a test
    asserts on it rather than on an ``observe`` call count.
    """

    __registry_key__ = "terminal_cycle_test_cap"
    __wiring__ = {"observation": ("observation", [_FRAG])}

    cycles: int
    fail_from: int | None
    raise_from: BaseException | None

    async def begin_observation(self, cycle: int) -> None:
        self.cycles = cycle

    async def resolve_fragment(self, frag_key: str, **kw: Any) -> Any:
        if (
            self.raise_from is not None
            and self.fail_from is not None
            and self.cycles >= self.fail_from
        ):
            raise self.raise_from
        return _Frame(cycle=self.cycles)


def _env(*, fail_from: int | None = None, raises: BaseException | None = None) -> Any:
    cap = _CycleCap()
    cap.cycles = 0
    cap.fail_from = fail_from
    cap.raise_from = raises
    env = make_env(EnvironmentBase, {"terminal_cycle_test_cap": cap})
    return env, cap


_NEGOTIATED = ObservationNegotiationResult(
    active_fragments=frozenset({_FRAG, "loop.task"}),
    required_fragments=frozenset({_FRAG}),
    env_sourced=frozenset({_FRAG}),
    loop_sourced=frozenset({"loop.task"}),
)


class _Completion:
    def __init__(self, text: str = "out") -> None:
        self.completion = text
        self.usage = None


class _LLM:
    inference_kind = "chat"
    model = "test-model"

    def __init__(self, raises: BaseException | None = None) -> None:
        self._raises = raises
        self.calls = 0

    async def ainvoke(self, messages: Any, **kw: Any) -> Any:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return _Completion()


class _Memory:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, m: Any) -> None:
        self.messages.append(m)

    def add_messages(self, ms: Any) -> None:
        self.messages.extend(ms)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)


class _Agent:
    """Clicks on every step; the act function decides when the run is done."""

    def __init__(self) -> None:
        self.seen_cycles: list[int] = []

    __execution_limit_fields__ = frozenset()

    async def build_react_prompt(self, obs: Any, *, context) -> list:
        self.seen_cycles.append(obs.test_terminal_frame.cycle)
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Action]:
        return [Action(name="browser.click", params={"index": 1})]


class _Unparseable(_Agent):
    def parse_react_output(self, out: Any) -> list[Action]:
        raise ParseError("no action in the reply")


async def _act_ok(action: Action) -> ActionResult:
    return ActionResult(success=True)


async def _act_done(action: Action) -> ActionResult:
    return ActionResult(success=True, is_done=True, extracted_content="the answer")


def _act_raising(exc: BaseException):
    async def act(action: Action) -> ActionResult:
        raise exc

    return act


class _Recorder(StepRecorder):
    """Keeps every observation handed over, beside its record."""

    def __init__(self) -> None:
        super().__init__()
        self.observations: list[Any] = []

    async def record_step(self, record: Any, *, observation: Any = None) -> None:
        await super().record_step(record, observation=observation)
        self.observations.append(observation)


class _FailingTerminalRecorder(_Recorder):
    """Records every step, then cannot write the terminal one."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    async def record_step(self, record: Any, *, observation: Any = None) -> None:
        await super().record_step(record, observation=observation)
        if record.origin == "terminal":
            raise self.error


def _ctx(env: Any, *, llm: Any = None, act_fn: Any = _act_done, recorder: Any = None) -> dict:
    return {
        "env": env,
        "llm": llm if llm is not None else _LLM(),
        "memory": _Memory(),
        "act_fn": act_fn,
        "action_space": ActionSpace(),
        "negotiated_obs": _NEGOTIATED,
        "step_recorder": recorder,
        "task_id": "t-1",
    }


async def _run(
    loop: ReactLoop,
    *,
    agent: Any = None,
    env: Any = None,
    llm: Any = None,
    act_fn: Any = _act_done,
    recorder: Any = None,
) -> tuple[AgentResult, _Recorder]:
    recorder = recorder if recorder is not None else _Recorder()
    if env is None:
        env, _ = _env()
    result = await loop._execute(
        agent if agent is not None else _Agent(),
        "t",
        ctx=_ctx(env, llm=llm, act_fn=act_fn, recorder=recorder),
    )
    return result, recorder


class TestOffByDefault:
    def test_the_default_is_off(self) -> None:
        assert ReactLoop().terminal_observation is False

    async def test_records_are_unchanged(self) -> None:
        env, cap = _env()
        result, recorder = await _run(ReactLoop(max_steps=3), env=env)
        assert [r.origin for r in recorder.records] == ["policy"]
        assert cap.cycles == 1
        assert result.metrics.steps == 1


class TestWhenItFires:
    async def test_on_done(self) -> None:
        env, cap = _env()
        result, recorder = await _run(ReactLoop(max_steps=3, terminal_observation=True), env=env)
        assert result.agent_done.value == "done"
        assert [(r.step, r.origin) for r in recorder.records] == [(0, "policy"), (1, "terminal")]
        terminal = recorder.records[-1]
        assert terminal.actions == [] and terminal.llm_output == ""
        assert terminal.token_usage is None
        assert terminal.error_phase == ""
        # One more cycle, and only that one.
        assert cap.cycles == 2
        assert recorder.observations[-1].test_terminal_frame.cycle == 2

    async def test_on_max_steps(self) -> None:
        env, _ = _env()
        result, recorder = await _run(
            ReactLoop(max_steps=2, terminal_observation=True), env=env, act_fn=_act_ok
        )
        assert result.agent_done.value == "max_steps"
        assert [(r.step, r.origin) for r in recorder.records] == [
            (0, "policy"),
            (1, "policy"),
            (2, "terminal"),
        ]

    async def test_at_the_end_of_a_parse_error_streak(self) -> None:
        env, _ = _env()
        loop = ReactLoop(max_steps=5, max_consecutive_parse_errors=2, terminal_observation=True)
        result, recorder = await _run(loop, agent=_Unparseable(), env=env)
        assert result.agent_done.value == "error"
        assert result.error is not None and result.error.startswith("Parse failed 2")
        assert [r.origin for r in recorder.records] == ["policy", "policy", "terminal"]

    async def test_the_terminal_observation_has_env_fragments_only(self) -> None:
        env, _ = _env()
        _, recorder = await _run(ReactLoop(max_steps=3, terminal_observation=True), env=env)
        policy_obs, terminal_obs = recorder.observations
        assert policy_obs.task is not None
        assert terminal_obs.task is None
        assert terminal_obs.test_terminal_frame is not None

    async def test_metrics_count_policy_steps_only(self) -> None:
        env, _ = _env()
        result, recorder = await _run(
            ReactLoop(max_steps=2, terminal_observation=True), env=env, act_fn=_act_ok
        )
        assert len(recorder.records) == 3
        assert result.metrics.steps == 2
        assert result.metrics.llm_calls is None

    async def test_the_result_is_decided_before_it(self) -> None:
        """The Loop never measures Runtime execution time, even for terminal observation."""

        class _SlowTerminalEnv(EnvironmentBase):
            __wiring__ = {"capabilities": "capability"}
            __registry_key__ = "terminal_slow_env"

            async def observe(self, negotiated: Any) -> Any:
                observation = await super().observe(negotiated)
                if self._observation_cycle == 2:
                    await asyncio.sleep(0.3)
                return observation

        cap = _CycleCap()
        cap.cycles, cap.fail_from, cap.raise_from = 0, None, None
        env = make_env(_SlowTerminalEnv, {"terminal_cycle_test_cap": cap})
        result, recorder = await _run(ReactLoop(max_steps=3, terminal_observation=True), env=env)
        assert recorder.records[-1].origin == "terminal"
        assert result.metrics.execution_ms is None


class TestWhenItDoesNot:
    async def test_not_after_an_observe_failure(self) -> None:
        env, cap = _env(fail_from=1, raises=ConnectionError("CDP websocket closed"))
        result, recorder = await _run(ReactLoop(max_steps=3, terminal_observation=True), env=env)
        assert result.agent_done.value == "error"
        assert [r.origin for r in recorder.records] == ["policy"]
        assert cap.cycles == 1

    async def test_not_after_an_llm_failure(self) -> None:
        env, cap = _env()
        result, recorder = await _run(
            ReactLoop(max_steps=3, terminal_observation=True),
            env=env,
            llm=_LLM(raises=RuntimeError("429 rate limited")),
        )
        assert result.agent_done.value == "error"
        assert [r.origin for r in recorder.records] == ["policy"]
        assert cap.cycles == 1

    async def test_not_after_a_failed_action(self) -> None:
        env, cap = _env()
        result, recorder = await _run(
            ReactLoop(max_steps=3, terminal_observation=True),
            env=env,
            act_fn=_act_raising(RuntimeError("browser died mid-click")),
        )
        assert result.agent_done.value == "error"
        assert [r.origin for r in recorder.records] == ["policy"]
        assert cap.cycles == 1

    async def test_not_after_a_veto(self) -> None:
        env, cap = _env()
        with pytest.raises(RunAbort):
            await _run(
                ReactLoop(max_steps=3, terminal_observation=True),
                env=env,
                act_fn=_act_raising(RunAbort("risk-control interstitial", source="guard")),
            )
        assert cap.cycles == 1


class TestTheTerminalObserveCanFail:
    async def test_a_failed_observe_is_recorded_and_the_outcome_stands(self) -> None:
        env, _ = _env(fail_from=2, raises=ConnectionError("tab crashed"))
        result, recorder = await _run(ReactLoop(max_steps=3, terminal_observation=True), env=env)
        assert result.agent_done.value == "done"
        assert result.answer == "the answer"
        assert result.error is None
        terminal = recorder.records[-1]
        assert (terminal.origin, terminal.error_phase) == ("terminal", "observe")
        assert terminal.error is not None and "tab crashed" in terminal.error
        assert recorder.observations[-1] is None

    async def test_a_veto_on_it_propagates_with_its_record(self) -> None:
        """A guard woven onto ``env.observe`` vetoes the terminal look."""
        veto = RunAbort("banned page", source="dom_guard")

        class _GuardedEnv(EnvironmentBase):
            __wiring__ = {"capabilities": "capability"}
            __registry_key__ = "terminal_guarded_env"

            async def observe(self, negotiated: Any) -> Any:
                if self._observation_cycle == 1:
                    raise veto
                return await super().observe(negotiated)

        cap = _CycleCap()
        cap.cycles, cap.fail_from, cap.raise_from = 0, None, None
        env = make_env(_GuardedEnv, {"terminal_cycle_test_cap": cap})
        with pytest.raises(RunAbort) as caught:
            await _run(
                ReactLoop(max_steps=3, terminal_observation=True),
                env=env,
                recorder=(recorder := _Recorder()),
            )
        assert caught.value is veto
        terminal = recorder.records[-1]
        assert (terminal.step, terminal.origin, terminal.error_phase) == (1, "terminal", "observe")
        assert terminal.error == str(veto)


class TestRecordingTheTerminalStep:
    async def test_a_veto_raised_by_the_sink_is_still_a_veto(self) -> None:
        env, _ = _env()
        veto = RunAbort("evidence guard", source="sink")
        with pytest.raises(RunAbort) as caught:
            await _run(
                ReactLoop(max_steps=3, terminal_observation=True),
                env=env,
                recorder=_FailingTerminalRecorder(veto),
            )
        assert caught.value is veto

    async def test_a_cancellation_carries_the_outcome(self) -> None:
        env, _ = _env()
        with pytest.raises(asyncio.CancelledError) as caught:
            await _run(
                ReactLoop(max_steps=3, terminal_observation=True),
                env=env,
                recorder=_FailingTerminalRecorder(asyncio.CancelledError()),
            )
        carried = carried_outcome(caught.value)
        assert isinstance(carried, AgentResult)
        assert carried.agent_done.value == "done"
        assert carried.answer == "the answer"

    async def test_a_cancelled_terminal_observe_carries_the_outcome(self) -> None:
        env, _ = _env(fail_from=2, raises=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError) as caught:
            await _run(ReactLoop(max_steps=3, terminal_observation=True), env=env)
        carried = carried_outcome(caught.value)
        assert isinstance(carried, AgentResult)
        assert carried.agent_done.value == "done"
