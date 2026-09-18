"""ReactLoop must not absorb a veto — B5.

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

from typing import Any

import pytest

from spectragent.core.loops.errors import ParseError, RunAbort
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

    def build_react_prompt(self, obs: Any) -> list:
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
    """B5 is a *distinction*, not a new blanket re-raise."""

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
            def build_react_prompt(self, obs: Any) -> list:
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
            def build_react_prompt(self, obs: Any) -> list:
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
