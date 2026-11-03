"""The task text a setup completed: ``task_text.json``, beside the loader's text in ``config.json``.

``docs/architecture_design.md`` §5.3: ``TraceWriter`` keeps what the
environment's ``task_text_from_setup`` returned (``after``, role ``env``, only
when there is an environment) and its worker commits it before any later step,
whatever the recording plan selects; a failed commit is a recording problem on
step 0. ``config.json`` names the capability whose setup was to complete the
text (``task_text_setter``, from ``prepare()``). ``RunTrace.task_text()`` is the
run's task text: the completed one, else the loader's; a run that named a
setter and committed steps but has no record is refused rather than read as
the loader's text.
"""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.trace import RunTrace, WALWriter
from spectragent.core.trace.encoding import TASK_TEXT_FILE, TraceFile
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import TraceConfig
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.task import SetupTaskText
from tests._helpers.trace_builder import agent_result, new_wal, step_record

LOADED = "Follow the instructions shown on the top of the screen."
MADE = SetupTaskText(
    capability_id="android",
    text="Follow the instructions shown on the top of the screen: Click on the Submit button.",
)


def _wal(tmp_path: Path, *, setter: str | None = "android") -> WALWriter:
    wal = WALWriter(tmp_path, "run-test")
    wal.init_run(TraceConfig(agent_id="m3a", loop_id="react", task=LOADED, task_text_setter=setter))
    return wal


class TestTheWAL:
    def test_committed_and_read_back_through_the_manifest(self, tmp_path: Path) -> None:
        wal = _wal(tmp_path)
        wal.commit_task_text(MADE)

        trace = RunTrace(wal.run_dir)
        assert trace.manifest.task_text == TASK_TEXT_FILE == "task_text.json"
        assert trace.setup_task_text() == MADE
        assert trace.task_text() == MADE.text
        assert trace.config.task == LOADED, "the loader's text stays beside it"
        (listing,) = [file for file in trace.committed_files if file.kind == "task_text"]
        assert (listing.path, listing.step) == (TASK_TEXT_FILE, None)
        assert json.loads(trace.read_committed(TASK_TEXT_FILE)) == MADE.model_dump(mode="json")

    def test_a_committed_record_is_immutable(self, tmp_path: Path) -> None:
        wal = _wal(tmp_path)
        wal.commit_task_text(MADE)
        with pytest.raises(ValueError, match="already committed"):
            wal.commit_task_text(MADE.model_copy(update={"text": "Do something else."}))
        assert RunTrace(wal.run_dir).setup_task_text() == MADE

    def test_anything_but_a_record_is_refused_before_a_byte_is_written(
        self, tmp_path: Path
    ) -> None:
        wal = _wal(tmp_path)
        not_a_record: Any = {"capability_id": "android", "text": "x"}
        with pytest.raises(ValueError, match="is a SetupTaskText"):
            wal.commit_task_text(not_a_record)
        assert not (wal.run_dir / TASK_TEXT_FILE).exists()
        assert RunTrace(wal.run_dir).manifest.task_text is None

    def test_no_extra_file_may_take_its_name(self, tmp_path: Path) -> None:
        wal = _wal(tmp_path)
        with pytest.raises(ValueError, match="a name the WAL owns"):
            wal.commit_trailing(extra_files=[TraceFile(TASK_TEXT_FILE, b"{}")])


class TestTheRunsTaskText:
    def test_a_run_no_setup_completed_reads_its_loaders_text(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        wal.commit_step(0, step_record(0))

        trace = RunTrace(wal.run_dir)
        assert trace.setup_task_text() is None
        assert trace.task_text() == trace.config.task == "find the answer"
        assert "task_text_setter" not in json.loads((wal.run_dir / "config.json").read_text()), (
            "written only when a setup completes the text"
        )

    def test_a_run_whose_record_is_lost_is_refused_rather_than_misread(
        self, tmp_path: Path
    ) -> None:
        """Its agent ran on the completed text: the loader's half is not what it was given."""
        wal = _wal(tmp_path)
        wal.commit_step(0, step_record(0))

        trace = RunTrace(wal.run_dir)
        assert trace.setup_task_text() is None
        with pytest.raises(
            TraceSchemaError, match="capability 'android' was to complete"
        ) as caught:
            trace.task_text()
        assert caught.value.path == wal.run_dir / "manifest.json"

    def test_a_run_that_never_got_past_setup_reads_its_loaders_text(self, tmp_path: Path) -> None:
        """Its setup failed before the text existed: the loader's text is the task it stands for."""
        trace = RunTrace(_wal(tmp_path).run_dir)
        assert trace.task_text() == LOADED

    @pytest.mark.parametrize(
        "damage",
        [b'{"capability_id": "android"', b"", b'{"capability_id": "android", "text": " "}', None],
        ids=["invalid JSON", "empty", "a blank text", "missing"],
    )
    def test_an_unreadable_record_names_the_file(
        self, tmp_path: Path, damage: bytes | None
    ) -> None:
        wal = _wal(tmp_path)
        wal.commit_task_text(MADE)
        path = wal.run_dir / TASK_TEXT_FILE
        if damage is None:
            path.unlink()
        else:
            path.write_bytes(damage)

        with pytest.raises(TraceSchemaError) as caught:
            RunTrace(wal.run_dir).task_text()
        assert caught.value.path == path


async def _start(tmp_path: Path, recording: RecordingPlan | None = None) -> TraceWriter:
    writer = TraceWriter(trace_dir=str(tmp_path / "traces"), recording=recording)
    await writer.on_run_start(
        agent=None, env=None, task=LOADED, config={}, task_text_setter="android"
    )
    return writer


async def _sink() -> None:
    return None


class TestTheTraceWriter:
    def test_the_interception_is_declared_only_with_an_environment(self) -> None:
        writer = TraceWriter()
        declared = {(i.role, i.handler_name) for i in TraceWriter.__intercepts__}
        assert ("env", "after_task_text_from_setup") in declared
        writer.intercept_environment(False)
        assert all(i.method != "task_text_from_setup" for i in writer.__intercepts__)

    async def test_the_text_is_committed_before_step_0_and_the_config_names_the_setter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        for name in ("commit_task_text", "commit_step"):
            real = getattr(WALWriter, name)

            def spy(self: WALWriter, *args: Any, _real: Any = real, _name: str = name, **kw: Any):
                order.append(_name)
                return _real(self, *args, **kw)

            monkeypatch.setattr(WALWriter, name, spy)

        writer = await _start(tmp_path)
        await asyncio.sleep(0.01)  # the worker is waiting for a segment when step 0 hands one over
        await writer.after_task_text_from_setup(MADE, args=(), kwargs={})

        async def step() -> None:
            await writer.around_record_step(proceed=_sink, args=(step_record(0),), kwargs={})

        await writer.around_execute_step(proceed=step, args=(None, 0), kwargs={})
        await writer.on_run_end(result=agent_result(task=MADE.text))

        assert order[:2] == ["commit_task_text", "commit_step"]
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert (trace.config.task, trace.config.task_text_setter) == (LOADED, "android")
        assert trace.task_text() == MADE.text
        assert trace.manifest.status.value == "complete"

    async def test_a_run_closed_right_after_setup_still_commits_it(self, tmp_path: Path) -> None:
        writer = await _start(tmp_path)
        await writer.after_task_text_from_setup(MADE, args=(), kwargs={})
        await writer.on_run_end(result=agent_result(task=MADE.text))

        assert writer.run_dir is not None
        assert RunTrace(writer.run_dir).setup_task_text() == MADE

    async def test_the_recording_plan_does_not_apply(self, tmp_path: Path) -> None:
        nothing = RecordingPlan(metrics=False, errors=False, trajectory=False, observations=False)
        writer = await _start(tmp_path, recording=nothing)
        await writer.after_task_text_from_setup(MADE, args=(), kwargs={})
        await writer.on_run_end(result=agent_result(task=MADE.text))

        assert writer.run_dir is not None
        assert RunTrace(writer.run_dir).setup_task_text() == MADE

    async def test_a_failed_commit_is_a_problem_on_step_0_so_no_prefix_is_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every step's prompt starts from the text: without it, branching and export refuse."""

        def full_disk(self: WALWriter, record: Any) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(WALWriter, "commit_task_text", full_disk)
        writer = await _start(tmp_path)
        await writer.after_task_text_from_setup(MADE, args=(), kwargs={})

        async def step() -> None:
            await writer.around_record_step(proceed=_sink, args=(step_record(0),), kwargs={})

        await writer.around_execute_step(proceed=step, args=(None, 0), kwargs={})
        await writer.on_run_end(result=agent_result(task=MADE.text))

        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status.value == "incomplete"
        assert trace.manifest.complete_through == -1
        health = trace.result.metrics.recording_health if trace.result else None
        assert health is not None
        assert [(p.step, p.channel, p.phase) for p in health.problems] == [
            (0, "task_text", "commit")
        ]
        with pytest.raises(TraceSchemaError, match="has no task_text.json"):
            trace.task_text()

    async def test_what_is_not_a_record_is_a_recording_problem(self, tmp_path: Path) -> None:
        """Taking the text never raises into the run."""
        writer = await _start(tmp_path)
        await writer.after_task_text_from_setup({"text": "x"}, args=(), kwargs={})
        await writer.on_run_end(result=agent_result(task=LOADED))

        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.setup_task_text() is None
        assert (trace.manifest.incomplete_reason or "").startswith("task_text: TypeError")
