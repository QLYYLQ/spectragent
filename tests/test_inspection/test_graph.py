"""The graph exposes committed execution facts without loading bulky evidence."""

from pathlib import Path

import pytest

from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection.graph import run_graph
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import step_record


def test_graph_keeps_committed_indices_and_origins_without_opening_calls_or_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wal = WALWriter(tmp_path, "graph")
    wal.init_run(TraceConfig())
    wal.commit_step(0, step_record(0).model_copy(update={"origin": "restored"}))
    wal.commit_step(3, step_record(3))
    wal.commit_step(4, StepRecord(step=4, origin="terminal"))
    (wal.run_dir / "steps" / "099.json").write_text(step_record(99).model_dump_json())

    def forbid(*args: object, **kwargs: object) -> None:
        pytest.fail("The graph must not open screenshots, prompts or archives")

    for method in ("llm_messages", "screenshot", "iter_archive", "archive_files"):
        monkeypatch.setattr(RunTrace, method, forbid)
    graph = run_graph(RunTrace(wal.run_dir))
    assert [(step["step"], step["origin"]) for step in graph["steps"]] == [
        (0, "restored"),
        (3, "policy"),
        (4, "terminal"),
    ]
    assert graph["restored_steps"] == 1
    assert graph["steps"][2]["actions"] == []
    assert "llm_output" not in graph["steps"][1]


def test_graph_action_status_does_not_invent_success_for_skipped_or_unreturned_actions(
    tmp_path: Path,
) -> None:
    wal = WALWriter(tmp_path, "actions")
    wal.init_run(TraceConfig())
    action = Action(name="browser.click_coordinate", params={"x": 4, "y": 8})
    wal.commit_step(
        0,
        StepRecord(
            step=0,
            actions=[action, action],
            action_results=[ActionResult(success=False)],
            skipped_actions=1,
        ),
    )
    wal.commit_step(1, StepRecord(step=1, actions=[action], error="raised", error_phase="act"))
    graph = run_graph(RunTrace(wal.run_dir))
    assert [action["status"] for action in graph["steps"][0]["actions"]] == ["failed", "skipped"]
    assert graph["steps"][1]["actions"][0]["status"] == "no_result"
    assert graph["steps"][0]["actions"][1]["index"] == 1
    assert graph["steps"][1]["error"] == "raised"


def test_graph_declares_omitted_trajectory_instead_of_claiming_no_actions(tmp_path: Path) -> None:
    wal = WALWriter(tmp_path, "light")
    wal.init_run(TraceConfig(recording=RecordingPlan(trajectory=False)))
    wal.commit_step(0, StepRecord(step=0, action_count=2))
    graph = run_graph(RunTrace(wal.run_dir))
    assert graph["recording_trajectory"] is False
    assert graph["steps"][0]["actions"] == []


def test_graph_refuses_a_damaged_committed_step(tmp_path: Path) -> None:
    wal = WALWriter(tmp_path, "damaged")
    wal.init_run(TraceConfig())
    wal.commit_step(0, step_record(0))
    (wal.run_dir / "steps" / "000.json").write_text("{")
    with pytest.raises(TraceSchemaError):
        run_graph(RunTrace(wal.run_dir))
