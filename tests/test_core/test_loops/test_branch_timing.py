"""Restoration and policy share the real step boundary without sharing their clocks."""

import asyncio
from typing import Literal

import pytest

import spectragent.core.hooks.trace_writer as recording
from spectragent.core.loops.branch import BranchLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.restoration import Checkpoint
from tests._helpers.assembly import AGENT_ID
from tests._helpers.branch_fakes import (
    TASK,
    BranchFakeAgent,
    ScriptedLLM,
    build_run,
    make_cap,
    record_source,
    run_branch,
)


class Clock:
    def __init__(self):
        self.value = 10.0

    def read(self):
        return self.value

    def add(self, seconds):
        self.value += seconds


def timed_recording(monkeypatch, clock):
    monkeypatch.setattr(recording.time, "perf_counter", clock.read)
    encode = recording.encode_observation

    def slow_capture(*args, **kwargs):
        clock.add(7)
        return encode(*args, **kwargs)

    monkeypatch.setattr(recording, "encode_observation", slow_capture)


def timed_capability(clock, *, failure=None):
    cap = make_cap()
    observe = cap.resolve_fragment
    execute = cap.execute_action

    async def capture(fragment_key):
        clock.add(2)
        if failure is not None:
            raise failure
        return await observe(fragment_key)

    async def act(action):
        clock.add(3)
        return await execute(action)

    cap.resolve_fragment = capture
    cap.execute_action = act
    return cap


@pytest.mark.parametrize("history_source", ["source", "restored"])
async def test_restoration_and_checkpoint_capture_are_timed_once(
    tmp_path, monkeypatch, history_source: Literal["source", "restored"]
):
    source = await record_source(tmp_path, ["click 100", "done source"])
    clock = Clock()
    timed_recording(monkeypatch, clock)
    invoke = ScriptedLLM.ainvoke

    async def slow_model(self, *args, **kwargs):
        clock.add(5)
        return await invoke(self, *args, **kwargs)

    monkeypatch.setattr(ScriptedLLM, "ainvoke", slow_model)
    out = await run_branch(
        tmp_path,
        source,
        1,
        ["done ok"],
        cap=timed_capability(clock),
        loop=BranchLoop(history_source=history_source),
    )
    assert out.run_dir is not None
    trace = RunTrace(out.run_dir)
    records = trace.step_records
    assert [(record.origin, record.duration_ms) for record in records] == [
        ("restored", 5000),  # actual observation and action
        ("policy", 10000),  # checkpoint observation + model + action
        ("terminal", 2000),
    ]
    assert all((trace.recording_timing(record.step) or 0) >= 7000 for record in records)
    assert len(out.llm.prompts) == 1
    assert out.cap.cycle == 3


@pytest.mark.parametrize("cancelled", [False, True])
async def test_interrupted_restoration_surrenders_its_measured_step(
    tmp_path, monkeypatch, cancelled
):
    source = await record_source(tmp_path, ["click 100", "done source"])
    clock = Clock()
    timed_recording(monkeypatch, clock)
    failure = asyncio.CancelledError() if cancelled else RuntimeError("restore capture failed")
    call = run_branch(
        tmp_path,
        source,
        1,
        ["done unused"],
        cap=timed_capability(clock, failure=failure),
        run_id="interrupted",
        raise_on_error=False,
    )
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        out = await call
        assert out.result is not None and out.result.agent_done.value == "error"
        assert out.llm.prompts == []
    record = RunTrace(tmp_path / "branches" / "interrupted").step(0)
    assert record is not None
    assert record.origin == "restored" and record.error_phase == "restore"
    assert record.duration_ms == 2000


@pytest.mark.parametrize("history_source", ["source", "restored"])
async def test_restoration_uses_the_same_execution_boundary_without_a_recorder(
    tmp_path, history_source: Literal["source", "restored"]
):
    source = await record_source(tmp_path, ["click 100", "done source"])
    loop = BranchLoop(history_source=history_source)
    calls = []
    execute_step = loop.execute_step

    async def observed_step(run, step_idx, **kwargs):
        calls.append((step_idx, run.restoration_iterator is not None))
        return await execute_step(run, step_idx, **kwargs)

    loop.execute_step = observed_step
    cap = make_cap()
    run, _, llm = build_run(
        tmp_path / "unused",
        loop=loop,
        replies=["done ok"],
        agent=BranchFakeAgent(),
        cap=cap,
    )
    run.by_layer.pop("hook")
    result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
        TASK, loop_inputs={"checkpoint": Checkpoint(trace_dir=str(source.resolve()), step=1)}
    )
    assert result.agent_done.value == "done"
    assert result.metrics.llm_calls is None and result.metrics.usage is None
    assert calls == [(0, True), (1, True), (1, False)]
    assert len(llm.prompts) == 1 and cap.cycle == 3
