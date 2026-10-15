"""A batch's selected tasks survive veto, cancellation and pre-flight refusal."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from spectragent.core.benchmark_runner import BenchmarkRunner, PreflightError, TaskOutcome
from spectragent.core.benchmark_session import BatchSession
from spectragent.core.composer import ComposedRun


def make_runner(tmp_path):
    session = BatchSession(tmp_path / ".sessions", benchmark="fixture", subset="test")
    runner = BenchmarkRunner(
        loader=SimpleNamespace(task_ids=lambda: ["first", "second", "third"]),
        compose=lambda: ComposedRun(by_layer={}),
        session=session,
    )
    return runner, session


async def test_veto_preserves_the_selected_but_unstarted_tasks(tmp_path, monkeypatch):
    runner, session = make_runner(tmp_path)
    monkeypatch.setattr(runner, "preflight", lambda task_id: None)

    async def veto(task_id, **kwargs):
        return TaskOutcome(task_id=task_id, vetoed="secret-not-for-session")

    monkeypatch.setattr(runner, "run_task", veto)
    summary = await runner.run()
    assert summary.was_aborted
    document = json.loads(session.path.read_text())
    assert document["format"] == "af-session-1"
    assert document["stop_reason"] == "vetoed"
    assert [row["status"] for row in document["tasks"]] == [
        "finished",
        "not_started",
        "not_started",
    ]
    assert document["tasks"][0]["agent_done"] == "vetoed"
    assert "secret-not-for-session" not in session.path.read_text()


async def test_cancellation_records_interrupted_task_and_preserves_pending_tasks(
    tmp_path, monkeypatch
):
    runner, session = make_runner(tmp_path)
    monkeypatch.setattr(runner, "preflight", lambda task_id: None)
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def wait(task_id, **kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(runner, "run_task", wait)
    task = asyncio.create_task(runner.run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    document = json.loads(session.path.read_text())
    assert document["stop_reason"] == "cancelled"
    assert [row["status"] for row in document["tasks"]] == [
        "interrupted",
        "not_started",
        "not_started",
    ]


async def test_session_write_failure_cannot_replace_cancellation(tmp_path, monkeypatch):
    runner, session = make_runner(tmp_path)
    monkeypatch.setattr(runner, "preflight", lambda task_id: None)

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    def failed_finish(reason):
        raise OSError("disk unavailable")

    monkeypatch.setattr(runner, "run_task", cancel)
    monkeypatch.setattr(session, "finish", failed_finish)
    with pytest.raises(asyncio.CancelledError):
        await runner.run()


async def test_preflight_refusal_records_no_started_tasks(tmp_path, monkeypatch):
    runner, session = make_runner(tmp_path)

    def refuse(task_id):
        raise PreflightError(task_id, ValueError("private configuration value"))

    monkeypatch.setattr(runner, "preflight", refuse)
    with pytest.raises(PreflightError):
        await runner.run()
    document = json.loads(session.path.read_text())
    assert document["stop_reason"] == "configuration_refused"
    assert {row["status"] for row in document["tasks"]} == {"not_started"}
    assert "private configuration value" not in session.path.read_text()


def test_repeated_task_ids_keep_their_selection_order(tmp_path):
    session = BatchSession(tmp_path / ".sessions", benchmark="fixture", subset="")
    session.start(["same", "same"])
    session.start_task("same")
    session.finish_task("same", {"agent_done": "done", "error": "not persisted"})
    session.start_task("same")
    session.finish("cancelled")
    document = json.loads(session.path.read_text())
    assert [row["status"] for row in document["tasks"]] == ["finished", "interrupted"]
    assert "not persisted" not in session.path.read_text()
