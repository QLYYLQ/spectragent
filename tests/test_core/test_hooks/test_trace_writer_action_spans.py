"""``StepRecord.action_spans``: when each ``env.act`` call of a step ran.

``TraceWriter`` times every ``env.act`` call from ``around`` advice and hands
the spans to the step executing it, the way it attributes model calls and
``duration_ms`` (``docs/architecture_design.md`` §5.3). Wall clock, so a reader
can join them with the environment evidence's own capture times. The runs
below go through the real runtime on the fake site of ``branch_fakes``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import BranchLoop, ReactLoop
from spectragent.core.loops.errors import RunAbort
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.step_record import ActionSpan, StepRecord
from tests._helpers.archive_fakes import ContractArchiveLLM, ContractArchiveMemory
from tests._helpers.assembly import AGENT_ID, make_run
from tests._helpers.branch_fakes import (
    CLICK,
    DONE,
    TASK,
    BranchFakeAgent,
    BranchFakeCap,
    build_run,
    make_cap,
    record_source,
    run_branch,
)


def _loop(**settings: Any) -> ReactLoop:
    return ReactLoop(max_steps=10, max_actions_per_step=2, today_override="2001-02-03", **settings)


async def _react_run(
    tmp_path: Path,
    replies: list[str],
    *,
    cap: BranchFakeCap | None = None,
    recording: RecordingPlan | None = None,
    terminal: bool = False,
    extra_hooks: tuple[Any, ...] = (),
) -> tuple[RunTrace, Any]:
    """One ReAct run on the fake site; the trace and what ``execute`` returned or raised."""
    run, writer, _ = build_run(
        tmp_path,
        loop=_loop(terminal_observation=terminal),
        replies=replies,
        agent=BranchFakeAgent(),
        cap=cap if cap is not None else make_cap(),
        extra_hooks=extra_hooks,
    )
    if recording is not None:
        writer.recording = recording
    try:
        outcome: Any = await AgentRuntime.for_agent(run, AGENT_ID).execute(
            TASK, task_id="spans", run_id="spans", timeout=30
        )
    except RunAbort as veto:
        outcome = veto
    assert writer.run_dir is not None
    return RunTrace(writer.run_dir), outcome


def _spans(record: StepRecord | None) -> list[ActionSpan]:
    assert record is not None and record.action_spans is not None
    return record.action_spans


def _in_call_order(spans: list[ActionSpan], *, before: float) -> None:
    edges = [edge for span in spans for edge in (span.started_at, span.ended_at)]
    assert edges == sorted(edges)
    assert edges[-1] <= before


class TestPolicySteps:
    async def test_each_call_of_a_step_has_a_span_in_call_order(self, tmp_path: Path) -> None:
        started = time.time()
        trace, result = await _react_run(tmp_path, ["click 100 101", "done ok"], terminal=True)
        assert result.agent_done.value == "done"
        first, second, terminal = trace.step_records

        # The agent wrote ``click``: the span names what ``env.act`` received.
        assert [(s.action, s.outcome) for s in _spans(first)] == [
            (CLICK, "returned"),
            (CLICK, "returned"),
        ]
        assert _spans(first)[0].started_at >= started
        _in_call_order(_spans(first), before=first.timestamp)
        assert [(s.action, s.outcome) for s in _spans(second)] == [(DONE, "returned")]
        assert _spans(second)[0].started_at >= _spans(first)[-1].ended_at
        assert terminal.origin == "terminal" and terminal.action_spans == []

    async def test_the_call_that_raised_has_the_last_span(self, tmp_path: Path) -> None:
        cap = make_cap()
        execute = cap.execute_action

        async def second_click_breaks(action: Action) -> ActionResult:
            if cap.clicks:
                raise RuntimeError("the page went away")
            return await execute(action)

        cap.execute_action = second_click_breaks
        trace, result = await _react_run(tmp_path, ["click 100 101"], cap=cap)
        assert result.agent_done.value == "error"
        (record,) = trace.step_records
        assert record.error_phase == "act" and "the page went away" in (record.error or "")
        assert len(record.action_results) == 1
        assert [s.outcome for s in _spans(record)] == ["returned", "raised"]
        _in_call_order(_spans(record), before=record.timestamp)

    async def test_a_vetoed_call_has_a_span_and_the_veto_reaches_the_caller(
        self, tmp_path: Path
    ) -> None:
        cap = make_cap()
        cap.veto_act_at = 1
        trace, outcome = await _react_run(tmp_path, ["click 100 101"], cap=cap)
        assert isinstance(outcome, RunAbort) and "trap" in str(outcome)
        (record,) = trace.step_records
        assert [s.outcome for s in _spans(record)] == ["returned", "raised"]

    async def test_a_guard_on_act_vetoes_inside_the_call_it_refused(self, tmp_path: Path) -> None:
        """``before`` advice runs within the ``around`` chain: the refused call has a span."""

        class _RefuseSecondClick(HookBase):
            __registry_key__ = "spans_refuse_second_click"
            __intercepts__: ClassVar[list[Intercept]] = [
                Intercept("act", role="env", advice="before")
            ]

            async def before_act(self, *, args: tuple, kwargs: dict, **ctx: Any) -> None:
                if args[0].params.get("index") == 101:
                    raise RunAbort("not that one", source=self.hook_id)

        cap = make_cap()
        trace, outcome = await _react_run(
            tmp_path, ["click 100 101"], cap=cap, extra_hooks=(_RefuseSecondClick(),)
        )
        assert isinstance(outcome, RunAbort) and str(outcome).endswith("not that one")
        assert cap.clicks == [0], "the refused click never reached the capability"
        (record,) = trace.step_records
        assert [(s.action, s.outcome) for s in _spans(record)] == [
            (CLICK, "returned"),
            (CLICK, "raised"),
        ]

    async def test_a_step_without_an_action_has_no_span(self, tmp_path: Path) -> None:
        """An unparseable reply executes nothing: measured, and empty."""
        trace, _ = await _react_run(tmp_path, ["hmm", "done ok"])
        unparsed, done = trace.step_records
        assert unparsed.error_phase == "parse" and unparsed.action_spans == []
        assert [s.action for s in _spans(done)] == [DONE]


class TestWhatTheRecordingPlanSelects:
    async def test_without_metrics_nothing_is_measured(self, tmp_path: Path) -> None:
        trace, _ = await _react_run(
            tmp_path, ["click 100", "done ok"], recording=RecordingPlan(metrics=False)
        )
        assert [(r.duration_ms, r.action_spans) for r in trace.step_records] == [
            (None, None),
            (None, None),
        ]

    async def test_metrics_without_the_trajectory_keep_the_spans(self, tmp_path: Path) -> None:
        trace, _ = await _react_run(
            tmp_path, ["click 100 101", "done ok"], recording=RecordingPlan(trajectory=False)
        )
        first, second = trace.step_records
        assert first.actions == [] and first.duration_ms is not None
        assert [s.action for s in _spans(first)] == [CLICK, CLICK]
        assert [s.action for s in _spans(second)] == [DONE]


@pytest.mark.parametrize("history_source", ["source", "restored"])
async def test_restored_steps_carry_the_calls_restoration_made(
    tmp_path: Path, history_source: Any
) -> None:
    source = await record_source(tmp_path, ["click 100 101", "done source"], loop=_loop())
    out = await run_branch(
        tmp_path,
        source,
        1,
        ["done ok"],
        loop=BranchLoop(
            history_source=history_source, max_actions_per_step=2, today_override="2001-02-03"
        ),
    )
    assert out.run_dir is not None
    restored, policy, terminal = RunTrace(out.run_dir).step_records
    assert restored.origin == "restored"
    assert [(s.action, s.outcome) for s in _spans(restored)] == [
        (CLICK, "returned"),
        (CLICK, "returned"),
    ]
    _in_call_order(_spans(restored), before=restored.timestamp)
    assert policy.origin == "policy" and [s.action for s in _spans(policy)] == [DONE]
    assert terminal.origin == "terminal" and terminal.action_spans == []


async def test_an_env_less_run_prepares_and_records_empty_spans(tmp_path: Path) -> None:
    """The ``env.act`` interception is declared only when the run has an environment.

    Otherwise the Weaver would refuse it as unable to fire, and an env-less
    run that records could not even prepare.
    """

    class _ThinkOnlyLoop(ReactLoop):
        __registry_key__ = "spans_think_only_loop"
        __requires_env__ = False

        async def execute_step(self, run: Any, step_idx: int, **kwargs: Any) -> Any:
            record = StepRecord(step=step_idx, timestamp=time.time())
            run.step_records.append(record)
            await self.surrender(run.recorder, record, None)
            if step_idx == 1:
                return self.build_result(run, agent_done="done", env_status="success")
            return None

    class _TaskOnlyAgent:
        __execution_limit_fields__ = frozenset()
        __wiring__ = {"observation": ("observation", ["loop.task"])}

        async def build_react_prompt(self, obs: Any, *, context: ReactPromptContext) -> list:
            return [{"role": "user", "content": "go"}]

        def parse_react_output(self, out: Any) -> list[Action]:
            return []

    writer = TraceWriter(trace_dir=str(tmp_path))
    run = make_run(
        agent=_TaskOnlyAgent(),
        loop=_ThinkOnlyLoop(max_steps=2),
        env=None,
        llm=ContractArchiveLLM(),
        memory=ContractArchiveMemory(),
        hooks=[writer],
    )
    result = await AgentRuntime.for_agent(run, AGENT_ID).execute("t", timeout=10)
    assert result.agent_done.value == "done"
    assert "env" not in {item.role for item in writer.__intercepts__}
    assert writer.run_dir is not None
    trace = RunTrace(writer.run_dir)
    assert [(r.duration_ms is not None, r.action_spans) for r in trace.step_records] == [
        (True, []),
        (True, []),
    ]
    writer.intercept_environment(True)
    assert writer.__intercepts__ == TraceWriter.__intercepts__


class TestTheAdvice:
    """The handler alone: what passes through it is never changed."""

    @staticmethod
    async def _one_step(writer: TraceWriter, act: Any) -> Any:
        """One ``env.act`` call inside step 0, then the step handed over."""
        outcome: Any = None

        async def execution() -> None:
            nonlocal outcome
            try:
                outcome = await writer.around_act(
                    proceed=act, args=(Action(name=CLICK, params={}),), kwargs={}
                )
            except BaseException as exc:
                outcome = exc
            await writer.around_record_step(
                proceed=_sink, args=(StepRecord(step=0, timestamp=time.time()),), kwargs={}
            )

        await writer.around_execute_step(proceed=execution, args=(None, 0), kwargs={})
        await writer.flush()
        return outcome

    @pytest.fixture()
    async def writer(self, tmp_path: Path) -> TraceWriter:
        writer = TraceWriter(trace_dir=str(tmp_path))
        await writer.on_run_start(agent=None, env=None, task="t", config={})
        return writer

    async def test_the_result_is_the_one_returned(self, writer: TraceWriter) -> None:
        returned = ActionResult(success=True)

        async def act() -> ActionResult:
            return returned

        assert await self._one_step(writer, act) is returned
        assert [s.outcome for s in _spans(_read(writer, 0))] == ["returned"]

    @pytest.mark.parametrize(
        ("error", "outcome"),
        [
            (RuntimeError("click failed"), "raised"),
            (RunAbort("vetoed", source="test_guard"), "raised"),
            (asyncio.CancelledError("stopped"), "cancelled"),
        ],
        ids=["error", "veto", "cancellation"],
    )
    async def test_the_exception_is_the_one_raised(
        self, writer: TraceWriter, error: BaseException, outcome: str
    ) -> None:
        async def act() -> ActionResult:
            await asyncio.sleep(0.01)
            raise error

        assert await self._one_step(writer, act) is error
        (span,) = _spans(_read(writer, 0))
        assert (span.action, span.outcome) == (CLICK, outcome)
        assert span.ended_at - span.started_at >= 0.005

    async def test_a_recording_fault_costs_only_the_span(self, writer: TraceWriter) -> None:
        returned = ActionResult(success=True)

        async def act() -> ActionResult:
            return returned

        async def execution() -> Any:
            # Not an ``Action``: the span cannot be named, the call still runs.
            result = await writer.around_act(proceed=act, args=(object(),), kwargs={})
            await writer.around_record_step(
                proceed=_sink, args=(StepRecord(step=0, timestamp=time.time()),), kwargs={}
            )
            return result

        assert await writer.around_execute_step(proceed=execution, args=(None, 0), kwargs={}) is (
            returned
        )
        await writer.flush()
        assert _read(writer, 0).action_spans == []
        assert [(p.channel, p.phase) for p in writer.health.problems] == [("timing", "capture")]


async def _sink() -> None:
    return None


def _read(writer: TraceWriter, step: int) -> StepRecord:
    assert writer.run_dir is not None
    record = RunTrace(writer.run_dir).step(step)
    assert record is not None
    return record
