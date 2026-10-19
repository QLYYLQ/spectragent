"""Live observation follows AOP outcomes without becoming persistence."""

import asyncio
from types import SimpleNamespace

import pytest

from spectragent.core.aop.weaver import Weaver
from spectragent.core.composer import ComposedRun
from spectragent.core.hooks.live_monitor import LiveMonitor
from spectragent.monitoring import ProgressState, attach_progress
from spectragent.schemas.action import Action
from spectragent.schemas.restoration import BranchProvenance, RestorationCheck
from spectragent.schemas.step_record import StepRecord


async def test_call_lifetime_return_failure_and_unknown_usage():
    state = ProgressState()
    monitor = LiveMonitor()
    monitor.bind(state, environment=False)
    entered = asyncio.Event()
    release = asyncio.Event()
    reply = SimpleNamespace(usage=SimpleNamespace(total_tokens=12))

    class Model:
        inference_kind = "chat"

        async def ainvoke(self):
            entered.set()
            await release.wait()
            return reply

    model = Weaver([monitor]).wrap(Model(), "llm")
    task = asyncio.create_task(model.ainvoke())
    await entered.wait()
    assert state.snapshot()["phase"] == "waiting for model"
    assert state.snapshot()["calls"] == 0
    release.set()
    assert await task is reply
    assert state.snapshot()["tokens"] == 12
    assert state.snapshot()["active"] == []

    failure = RuntimeError("provider unavailable")

    async def fail():
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await monitor.around_ainvoke(proceed=fail)
    assert caught.value is failure
    snapshot = state.snapshot()
    assert (snapshot["calls"], snapshot["usage_missing"], snapshot["tokens"]) == (2, 1, 12)
    assert "provider unavailable" in snapshot["events"][-1]


async def test_cancel_and_concurrent_calls_settle_their_own_tokens():
    state = ProgressState()
    hook = LiveMonitor()
    hook.bind(state, environment=False)
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def wait():
        entered.put_nowait(True)
        await release.wait()
        return SimpleNamespace(usage=None)

    first = asyncio.create_task(hook.around_ainvoke(proceed=wait))
    second = asyncio.create_task(hook.around_ainvoke(proceed=wait))
    await entered.get()
    await entered.get()
    assert len(state.snapshot()["active"]) == 2
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(state.snapshot()["active"]) == 1
    release.set()
    await second
    assert state.snapshot()["active"] == []
    assert state.snapshot()["calls"] == state.snapshot()["usage_missing"] == 2


async def test_step_count_distinguishes_policy_and_restoration():
    state = ProgressState()
    hook = LiveMonitor()
    hook.bind(state, environment=False)

    async def record():
        return "unchanged"

    for origin in ("restored", "policy", "terminal"):
        result = await hook.around_record_step(
            proceed=record,
            args=(StepRecord(step=0, origin=origin, actions=[Action(name="browser.click")]),),
            kwargs={},
        )
        assert result == "unchanged"
    assert state.snapshot()["policy_steps"] == 1
    assert [row["origin"] for row in state.snapshot()["steps"]] == [
        "restored",
        "policy",
        "terminal",
    ]


def test_attach_reuses_configured_monitor_and_envless_instances_do_not_share_intercepts():
    configured = LiveMonitor()
    first = ComposedRun(
        by_layer={"hook": {"configured": configured}, "environment": {"env": object()}}
    )
    second = ComposedRun(by_layer={})
    first_state, second_state = ProgressState(), ProgressState()
    attach_progress(first, first_state)
    attach_progress(second, second_state)
    attach_progress(first, first_state)
    assert first.hooks == [configured]
    assert any(item.role == "env" for item in first.hooks[0].__intercepts__)
    assert all(item.role != "env" for item in second.hooks[0].__intercepts__)
    first_state.called(SimpleNamespace(total_tokens=3))
    assert second_state.snapshot()["calls"] == 0
    snapshot = first_state.snapshot()
    snapshot["events"].append("not state")
    assert "not state" not in first_state.snapshot()["events"]


async def test_a_display_sink_failure_cannot_change_the_model_outcome(monkeypatch):
    state = ProgressState()
    monitor = LiveMonitor()
    monitor.bind(state, environment=False)

    def broken(*args, **kwargs):
        raise ValueError("display sink unavailable")

    monkeypatch.setattr(state, "begin", broken)
    monkeypatch.setattr(state, "called", broken)
    reply = object()

    async def complete():
        return reply

    assert await monitor.around_ainvoke(proceed=complete) is reply
    failure = RuntimeError("actual provider failure")

    async def fail():
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await monitor.around_ainvoke(proceed=fail)
    assert caught.value is failure


def test_envless_hook_passes_the_real_weaver_audit():
    run = ComposedRun(by_layer={})
    attach_progress(run, ProgressState())

    async def method(*args, **kwargs):
        pass

    weaver = Weaver(run.hooks)
    weaver.wrap(None, "env")
    weaver.wrap(SimpleNamespace(run=method), "agent")
    weaver.wrap(SimpleNamespace(ainvoke=method), "llm")
    weaver.wrap(SimpleNamespace(record_step=method), "loop")
    weaver.raise_for_unclaimed_roles()
    weaver.audit_intercepts()


def test_trace_location_is_observed_lazily_and_cleared_for_the_next_run():
    writer = SimpleNamespace(run_dir=None)
    state = ProgressState()
    run = ComposedRun(by_layer={"hook": {"writer": writer}})
    attach_progress(run, state)
    assert state.snapshot()["trace_path"] is None
    writer.run_dir = "/recorded/task-one"
    assert state.snapshot()["trace_path"] == "/recorded/task-one"
    attach_progress(ComposedRun(by_layer={}), state)
    assert state.snapshot()["trace_path"] is None


async def test_strict_restore_failure_keeps_zero_policy_steps_and_branch_context():
    state = ProgressState()
    hook = LiveMonitor()
    hook.bind(state, environment=False)
    provenance = BranchProvenance(
        source_run_id="source",
        source_trace="source",
        checkpoint_step=3,
        branch_index=2,
        branch_group_id="source/s3",
    )
    await hook.on_run_start(
        loop=SimpleNamespace(
            max_steps=30,
            continue_steps=0,
            history_source="source",
            restorer=SimpleNamespace(verify="warn"),
        ),
        loop_id="branch",
        branch=provenance,
    )

    async def recorded():
        return None

    check = RestorationCheck(
        step=0, verdict="diverged", reason="url_mismatch", reasons=["url_mismatch"]
    )
    record = StepRecord(
        step=0,
        origin="restored",
        error_phase="restore",
        error="RestorationDivergedError",
        restoration=check,
    )
    await hook.around_record_step(proceed=recorded, args=(record,), kwargs={})
    snapshot = state.snapshot()
    assert snapshot["policy_steps"] == 0
    assert snapshot["calls"] == 0
    assert snapshot["max_steps"] == 30
    assert snapshot["continue_steps"] == 0
    assert snapshot["identity"]["history_source"] == "source"
    assert snapshot["identity"]["verify"] == "warn"
    assert snapshot["branch"] == provenance.model_dump(mode="json")
    assert snapshot["steps"][0]["detail"]["restoration"]["verdict"] == "diverged"
