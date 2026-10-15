"""ReactLoop must not absorb a veto.

Everything a Loop catches is turned into a terminal ``AgentResult``: a
dead browser, a 429, an action that raised. That is right for a failure
and wrong for a *veto*. A safety guard woven onto ``act`` that spots a
risk-control response is not reporting that the click failed — it is
refusing to let the run continue — and the blanket ``except Exception``
around ``act_fn`` made the two indistinguishable, so the guard's refusal
came back as an ordinary ``agent_done="error"`` and the caller moved on
to the next task. Observed live on Xiaohongshu.

``RunAbort`` is the one exception every ``except Exception`` in the Loop
has to let through. These tests pin that on every phase a guard can be
woven onto — observe, prompt, llm, parse, act — and pin that an ordinary
exception still behaves exactly as it did.

Escaping is only half of it. A veto that propagated cleanly but left no
step behind would throw away the one thing worth having: the observation,
and the reply, the guard fired on. So every phase here is asserted twice —
the exception is the *same object* on the way out, and the interrupted
step reached the sink before it left. ``test_abort_handler_hygiene.py``
pins the shape at the source level; this file pins the behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest

from spectragent.core.loops.errors import ParseError, RunAbort, carried_outcome
from spectragent.core.loops.react import ReactLoop
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation import build_observation_model

# A loop-sourced fragment on purpose: these tests are about control flow,
# and ``loop.task`` is registered by ``schemas/observation`` itself, so
# they do not drag in a capability just to have an observation model.
_ACTIVE = frozenset({"loop.task"})


class _Completion:
    def __init__(self, text: str = "out") -> None:
        self.completion = text
        self.usage = None


class _LLM:
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

    def clear(self) -> None:
        self.messages.clear()


class _Negotiated:
    def __init__(self, active: frozenset[str]) -> None:
        self.active_fragments = active
        self.loop_sourced = active
        self.env_sourced = frozenset()


class _Env:
    def __init__(self, raises: BaseException | None = None) -> None:
        self._model = build_observation_model(_ACTIVE)
        self._raises = raises
        self.observe_calls = 0

    async def observe(self, negotiated: Any = None) -> Any:
        self.observe_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._model()


class _Agent:
    """Emits one click per step, forever."""

    __execution_limit_fields__ = frozenset()

    async def build_react_prompt(self, obs: Any, *, context) -> list:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Action]:
        return [Action(name="browser.click", params={"index": 1})]

    def get_react_output_model(self) -> None:
        return None


def _ctx(*, env: Any, llm: Any, act_fn: Any, recorder: Any, memory: Any = None) -> dict:
    return {
        "env": env,
        "llm": llm,
        "memory": memory if memory is not None else _Memory(),
        "act_fn": act_fn,
        "negotiated_obs": _Negotiated(_ACTIVE),
        "step_recorder": recorder,
        "task_id": "t-1",
    }


def _act_raising(exc: BaseException):
    async def act(action: Action) -> ActionResult:
        raise exc

    return act


async def _act_ok(action: Action) -> ActionResult:
    return ActionResult(success=True)


async def _act_done(action: Action) -> ActionResult:
    return ActionResult(success=True, is_done=True, extracted_content="the answer")


class TestAbortEscapesTheActGuard:
    async def test_run_abort_from_act_propagates(self) -> None:
        """The defect, stated directly."""
        loop = ReactLoop(max_steps=3)
        abort = RunAbort("risk-control interstitial", source="xhs_guard")

        with pytest.raises(RunAbort) as caught:
            await loop._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=_Env(),
                    llm=_LLM(),
                    act_fn=_act_raising(abort),
                    recorder=StepRecorder(),
                ),
            )

        # The *same* object, not a re-wrap: the guard's reason and source
        # are what a trace reader needs.
        assert caught.value is abort
        assert caught.value.reason == "risk-control interstitial"
        assert caught.value.source == "xhs_guard"

    async def test_the_vetoed_step_still_reaches_the_sink(self) -> None:
        """The evidence the guard fired on is the point of the trace.

        Propagating *before* surrendering the step would throw away the
        observation and the action that triggered the veto — precisely the
        one step anybody debugging the abort wants to look at.
        """
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=3)

        with pytest.raises(RunAbort):
            await loop._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=_Env(),
                    llm=_LLM(),
                    act_fn=_act_raising(RunAbort("stop", source="guard")),
                    recorder=recorder,
                ),
            )

        assert len(recorder.records) == 1
        rec = recorder.records[0]
        assert rec.error_phase == "act"
        assert "stop" in (rec.error or "")
        # The action that provoked it is on the record, with its LLM turn.
        assert [a.name for a in rec.actions] == ["browser.click"]
        assert rec.llm_output == "out"
        assert recorder.last_observation is not None

    async def test_abort_stops_the_run_immediately(self) -> None:
        """No further observe/LLM round trip after the veto."""
        env = _Env()
        llm = _LLM()
        loop = ReactLoop(max_steps=10)

        with pytest.raises(RunAbort):
            await loop._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=env,
                    llm=llm,
                    act_fn=_act_raising(RunAbort("stop")),
                    recorder=StepRecorder(),
                ),
            )

        assert env.observe_calls == 1
        assert llm.calls == 1

    async def test_a_guard_subclass_is_still_an_abort(self) -> None:
        """Guards will subclass it to carry their own payload."""

        class RiskControlDetected(RunAbort):
            def __init__(self, page: str) -> None:
                self.page = page
                super().__init__("risk control", source="xhs_guard")

        loop = ReactLoop(max_steps=3)
        with pytest.raises(RiskControlDetected) as caught:
            await loop._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=_Env(),
                    llm=_LLM(),
                    act_fn=_act_raising(RiskControlDetected("/captcha")),
                    recorder=StepRecorder(),
                ),
            )
        assert caught.value.page == "/captcha"


class TestOrdinaryFailuresAreUnchanged:
    """Not absorbing a veto is a *distinction*, not a new blanket re-raise."""

    async def test_action_failure_still_returns_a_terminal_result(self) -> None:
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=2)
        result = await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=_Env(),
                llm=_LLM(),
                act_fn=_act_raising(RuntimeError("browser died mid-click")),
                recorder=recorder,
            ),
        )
        assert result.agent_done.value == "error"
        assert result.env_status.value == "failed"
        assert recorder.records[0].error_phase == "act"

    async def test_observe_failure_still_returns_a_terminal_result(self) -> None:
        loop = ReactLoop(max_steps=2)
        result = await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=_Env(raises=RuntimeError("CDP disconnected")),
                llm=_LLM(),
                act_fn=_act_ok,
                recorder=StepRecorder(),
            ),
        )
        assert result.agent_done.value == "error"
        assert result.env_status.value == "failed"

    async def test_llm_failure_still_returns_a_terminal_result(self) -> None:
        loop = ReactLoop(max_steps=2)
        result = await loop._execute(
            _Agent(),
            "t",
            ctx=_ctx(
                env=_Env(),
                llm=_LLM(raises=RuntimeError("429 rate limited")),
                act_fn=_act_ok,
                recorder=StepRecorder(),
            ),
        )
        assert result.agent_done.value == "error"
        assert result.env_status.value == "success"


class TestAbortOnEveryPhase:
    """A guard can be woven onto observe or ainvoke just as easily."""

    async def test_abort_from_observe_propagates_and_is_recorded(self) -> None:
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=2)
        with pytest.raises(RunAbort):
            await loop._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=_Env(raises=RunAbort("banned page", source="dom_guard")),
                    llm=_LLM(),
                    act_fn=_act_ok,
                    recorder=recorder,
                ),
            )
        assert len(recorder.records) == 1
        assert recorder.records[0].error_phase == "observe"
        assert "banned page" in (recorder.records[0].error or "")

    async def test_abort_from_llm_propagates_and_is_recorded(self) -> None:
        recorder = StepRecorder()
        loop = ReactLoop(max_steps=2)
        with pytest.raises(RunAbort):
            await loop._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=_Env(),
                    llm=_LLM(raises=RunAbort("budget exhausted", source="cost_guard")),
                    act_fn=_act_ok,
                    recorder=recorder,
                ),
            )
        assert len(recorder.records) == 1
        assert recorder.records[0].error_phase == "llm"
        assert "cost_guard" in (recorder.records[0].error or "")

    async def test_abort_from_the_parser_is_not_a_parse_error(self) -> None:
        """``except ParseError`` must not widen into a veto sink either.

        ``parse_retries_per_step=5``, so this also proves the in-step retry
        loop does not absorb the veto and re-ask five times.
        """

        class _AbortingAgent(_Agent):
            def parse_react_output(self, out: Any) -> list[Action]:
                raise RunAbort("agent refuses to continue", source="self_check")

        llm = _LLM()
        loop = ReactLoop(max_steps=2, parse_retries_per_step=5)
        with pytest.raises(RunAbort):
            await loop._execute(
                _AbortingAgent(),
                "t",
                ctx=_ctx(
                    env=_Env(),
                    llm=llm,
                    act_fn=_act_ok,
                    recorder=StepRecorder(),
                ),
            )
        assert llm.calls == 1

    async def test_the_vetoed_parse_step_is_recorded_with_the_reply(self) -> None:
        """A parser veto is the one abort whose evidence is the reply.

        ``parse_react_output`` is an agent method, so it is woven like any
        other and an agent that self-checks the model's answer vetoes from
        exactly there. Escaping unrecorded would lose both the observation
        and the text that was refused — which is the whole reason
        ``RunAbort`` derives from ``Exception`` rather than
        ``BaseException``.
        """
        recorder = StepRecorder()

        class _AbortingAgent(_Agent):
            def parse_react_output(self, out: Any) -> list[Action]:
                raise RunAbort("refuses to continue", source="self_check")

        loop = ReactLoop(max_steps=2)
        with pytest.raises(RunAbort):
            await loop._execute(
                _AbortingAgent(),
                "t",
                ctx=_ctx(
                    env=_Env(),
                    llm=_LLM(),
                    act_fn=_act_ok,
                    recorder=recorder,
                ),
            )

        assert len(recorder.records) == 1
        rec = recorder.records[0]
        assert rec.error_phase == "parse"
        assert "self_check" in (rec.error or "")
        # The refused reply, and the observation it was an answer to.
        assert rec.llm_output == "out"
        assert recorder.last_observation is not None

    async def test_abort_from_the_prompt_builder_is_recorded(self) -> None:
        """``build_react_prompt`` is woven too, and used to sit outside
        every ``try`` — a veto there propagated with no step at all."""
        recorder = StepRecorder()

        class _AbortingAgent(_Agent):
            __execution_limit_fields__ = frozenset()

            async def build_react_prompt(self, obs: Any, *, context) -> list:
                raise RunAbort("banned page in the DOM", source="dom_guard")

        llm = _LLM()
        loop = ReactLoop(max_steps=2)
        with pytest.raises(RunAbort):
            await loop._execute(
                _AbortingAgent(),
                "t",
                ctx=_ctx(
                    env=_Env(),
                    llm=llm,
                    act_fn=_act_ok,
                    recorder=recorder,
                ),
            )

        assert len(recorder.records) == 1
        assert recorder.records[0].error_phase == "prompt"
        assert "dom_guard" in (recorder.records[0].error or "")
        # Vetoed before the model was ever asked.
        assert llm.calls == 0
        assert recorder.last_observation is not None

    async def test_an_ordinary_prompt_builder_bug_still_propagates_raw(self) -> None:
        """No blanket handler was added around the builder.

        An exception out of the agent's own prompt code is a bug in the
        agent, not an infrastructure failure, and turning it into a
        terminal ``AgentResult`` would hide it. Only ``RunAbort`` is
        caught there.
        """

        class _BrokenAgent(_Agent):
            __execution_limit_fields__ = frozenset()

            async def build_react_prompt(self, obs: Any, *, context) -> list:
                raise KeyError("template placeholder missing")

        loop = ReactLoop(max_steps=2)
        with pytest.raises(KeyError):
            await loop._execute(
                _BrokenAgent(),
                "t",
                ctx=_ctx(
                    env=_Env(),
                    llm=_LLM(),
                    act_fn=_act_ok,
                    recorder=StepRecorder(),
                ),
            )


class _FailingRecorder(StepRecorder):
    """A sink that takes the step, then cannot write it.

    What ``TraceWriter``'s blocking ``after`` advice on ``record_step`` does
    when the step's archive collection fails or the disk is full: the
    recorder saw the step, the commit raised. ``fail_at=None`` fails every
    step.
    """

    def __init__(self, error: Exception, *, fail_at: int | None = None) -> None:
        super().__init__()
        self.error = error
        self.fail_at = fail_at

    async def record_step(self, record: Any, *, observation: Any = None) -> None:
        await super().record_step(record, observation=observation)
        if self.fail_at is None or record.step == self.fail_at:
            raise self.error


class _VetoingParser(_Agent):
    def parse_react_output(self, out: Any) -> list[Action]:
        raise RunAbort("the reply shows a captcha wall", source="self_check")


class _VetoingPromptBuilder(_Agent):
    __execution_limit_fields__ = frozenset()

    async def build_react_prompt(self, obs: Any, *, context) -> list:
        raise RunAbort("banned page in the DOM", source="dom_guard")


class _Unparseable(_Agent):
    def parse_react_output(self, out: Any) -> list[Action]:
        raise ParseError("no action in the reply")


class TestSinkGuard:
    async def test_a_veto_from_the_sink_itself_still_propagates(self) -> None:
        """Returning the recorder's failure never swallows a veto it raised."""
        veto = RunAbort("the recorder refuses", source="record_guard")
        with pytest.raises(RunAbort) as caught:
            await ReactLoop(max_steps=3)._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=_Env(raises=ConnectionError("CDP websocket closed")),
                    llm=_LLM(),
                    act_fn=_act_ok,
                    recorder=_FailingRecorder(veto),
                ),
            )
        assert caught.value is veto


class _CutOffRecorder(StepRecorder):
    """A sink a cancellation cuts off while it records the step at *cut_at* (every step: ``None``).

    What the run's own timeout does to ``TraceWriter``'s blocking ``after``
    advice on ``record_step`` when that step's archive collection is slow:
    the recorder saw the step, then the cancellation landed in the
    hand-over. *seen_log* is called at that moment, so a test can tell what
    had already been logged.
    """

    def __init__(
        self, *, cut_at: int | None = None, seen_log: Callable[[], list[str]] | None = None
    ) -> None:
        super().__init__()
        self.cut_at = cut_at
        self.seen_log = seen_log
        self.logged: list[str] = []

    async def record_step(self, record: Any, *, observation: Any = None) -> None:
        await super().record_step(record, observation=observation)
        if self.cut_at is None or record.step == self.cut_at:
            if self.seen_log is not None:
                self.logged = self.seen_log()
            raise asyncio.CancelledError


class TestACancelledRecordingCarriesTheOutcome:
    """The step a failing run ends on may be cut off mid-recording; the outcome rides along.

    A cancellation is not the Loop's to swallow — it may be Ctrl-C — so it
    propagates unchanged. But when it is the run's own timeout, landing in
    the slow archive collection of the very page a guard refused, the run
    had already ended: ``AgentRuntime.execute`` reads the outcome back off
    the cancellation (``carried_outcome``) and ends the run as vetoed, or
    with the result the Loop had built, instead of as a timeout. On every
    other step nothing is carried: the run had not ended.
    """

    @pytest.mark.parametrize(
        ("phase", "agent", "env", "llm", "act_fn"),
        [
            pytest.param(
                "observe",
                _Agent(),
                _Env(raises=RunAbort("banned page", source="dom_guard")),
                _LLM(),
                _act_ok,
                id="observe",
            ),
            pytest.param("prompt", _VetoingPromptBuilder(), _Env(), _LLM(), _act_ok, id="prompt"),
            pytest.param(
                "llm",
                _Agent(),
                _Env(),
                _LLM(raises=RunAbort("budget exhausted", source="cost_guard")),
                _act_ok,
                id="llm",
            ),
            pytest.param("parse", _VetoingParser(), _Env(), _LLM(), _act_ok, id="parse"),
            pytest.param(
                "act",
                _Agent(),
                _Env(),
                _LLM(),
                _act_raising(RunAbort("risk-control interstitial", source="xhs_guard")),
                id="act",
            ),
        ],
    )
    async def test_a_veto_rides_the_cancellation_out(
        self,
        phase: str,
        agent: Any,
        env: Any,
        llm: Any,
        act_fn: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="spectragent.core.loops.react")
        recorder = _CutOffRecorder()
        with pytest.raises(asyncio.CancelledError) as caught:
            await ReactLoop(max_steps=3)._execute(
                agent, "t", ctx=_ctx(env=env, llm=llm, act_fn=act_fn, recorder=recorder)
            )

        assert [record.error_phase for record in recorder.records] == [phase]
        veto = carried_outcome(caught.value)
        assert isinstance(veto, RunAbort)
        assert str(veto) == recorder.records[0].error
        # Carried, not noted: nothing failed to record, the recording was cut off.
        assert not getattr(veto, "__notes__", [])
        # And the log says so, whoever turns out to have cancelled.
        assert (
            f"recording step 0 was cut off by a cancellation; the run had already ended (the veto: {veto})"
            in caplog.text
        )

    async def test_the_act_veto_is_logged_before_its_step_is_handed_over(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Whatever becomes of the hand-over, the log already says a guard said stop."""
        caplog.set_level(logging.WARNING, logger="spectragent.core.loops.react")
        recorder = _CutOffRecorder(seen_log=lambda: [r.getMessage() for r in caplog.records])
        with pytest.raises(asyncio.CancelledError):
            await ReactLoop(max_steps=3)._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=_Env(),
                    llm=_LLM(),
                    act_fn=_act_raising(RunAbort("risk-control interstitial", source="xhs_guard")),
                    recorder=recorder,
                ),
            )

        assert any(
            "run aborted during act at step 0: xhs_guard: risk-control interstitial" in line
            for line in recorder.logged
        )
        assert "recording step 0 was cut off by a cancellation" in caplog.text

    @pytest.mark.parametrize(
        ("loop", "agent", "env", "llm", "act_fn", "env_status", "expected"),
        [
            pytest.param(
                ReactLoop(max_steps=3),
                _Agent(),
                _Env(raises=ConnectionError("CDP websocket closed")),
                _LLM(),
                _act_ok,
                "failed",
                "observe failed at step 0: ConnectionError: CDP websocket closed",
                id="observe",
            ),
            pytest.param(
                ReactLoop(max_steps=3),
                _Agent(),
                _Env(),
                _LLM(raises=RuntimeError("429 rate limited")),
                _act_ok,
                "success",
                "llm failed at step 0: RuntimeError: 429 rate limited",
                id="llm",
            ),
            pytest.param(
                ReactLoop(max_steps=3),
                _Agent(),
                _Env(),
                _LLM(),
                _act_raising(RuntimeError("browser died mid-click")),
                "failed",
                "Action execution failed at step 0: browser died mid-click",
                id="action",
            ),
            pytest.param(
                ReactLoop(max_steps=3, max_consecutive_parse_errors=1, parse_retries_per_step=0),
                _Unparseable(),
                _Env(),
                _LLM(),
                _act_ok,
                "success",
                "Parse failed 1 consecutive times: no action in the reply",
                id="parse",
            ),
        ],
    )
    async def test_a_failing_outcome_rides_the_cancellation_out(
        self,
        loop: ReactLoop,
        agent: Any,
        env: Any,
        llm: Any,
        act_fn: Any,
        env_status: str,
        expected: str,
    ) -> None:
        recorder = _CutOffRecorder()
        with pytest.raises(asyncio.CancelledError) as caught:
            await loop._execute(
                agent, "t", ctx=_ctx(env=env, llm=llm, act_fn=act_fn, recorder=recorder)
            )

        result = carried_outcome(caught.value)
        assert result is not None and not isinstance(result, RunAbort)
        assert result.agent_done.value == "error"
        assert result.env_status.value == env_status
        # The root cause, exactly — the runtime appends what cut it off.
        assert result.error == expected
        assert result.metrics.steps == len(recorder.records) == 1

    @pytest.mark.parametrize(
        ("act_fn", "cut_at"),
        [
            pytest.param(_act_ok, 0, id="a step the run goes on from"),
        ],
    )
    async def test_a_run_that_had_not_failed_carries_nothing(
        self, act_fn: Any, cut_at: int
    ) -> None:
        """An interrupted nonterminal step has no decided outcome to carry."""
        with pytest.raises(asyncio.CancelledError) as caught:
            await ReactLoop(max_steps=3)._execute(
                _Agent(),
                "t",
                ctx=_ctx(
                    env=_Env(), llm=_LLM(), act_fn=act_fn, recorder=_CutOffRecorder(cut_at=cut_at)
                ),
            )
        assert carried_outcome(caught.value) is None

    async def test_a_parse_error_the_run_survives_carries_nothing(self) -> None:
        loop = ReactLoop(max_steps=5, max_consecutive_parse_errors=3, parse_retries_per_step=0)
        with pytest.raises(asyncio.CancelledError) as caught:
            await loop._execute(
                _Unparseable(),
                "t",
                ctx=_ctx(env=_Env(), llm=_LLM(), act_fn=_act_ok, recorder=_CutOffRecorder()),
            )
        assert carried_outcome(caught.value) is None


class TestExceptionShape:
    def test_run_abort_is_an_exception_not_a_base_exception(self) -> None:
        """It must be caught by ``AgentRuntime``'s ``except Exception``.

        That handler is what writes the terminal result, dispatches
        ``on_run_end`` / ``on_run_error`` and *then* re-raises. Deriving
        from ``BaseException`` would make an abort skip the WAL commit —
        losing exactly the trace the abort exists to preserve.
        """
        assert issubclass(RunAbort, Exception)
        assert not issubclass(RunAbort, ParseError)

    def test_source_is_visible_in_the_message(self) -> None:
        assert str(RunAbort("why", source="who")) == "who: why"
        assert str(RunAbort("why")) == "why"
