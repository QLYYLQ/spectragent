"""Session scheduling remains truthful across repeated tasks and interrupted runs."""

from spectragent.monitoring import ProgressState
from spectragent.schemas.step_record import StepRecord


def test_duplicate_task_occurrences_have_independent_rows_and_outcomes():
    state = ProgressState()
    state.set_tasks(["repeated", "repeated", "last"])
    state.start_task("repeated")
    state.finish_task("repeated", {"agent_done": "done"})
    state.start_task("repeated")
    state.finish_task("repeated", {"agent_done": "timeout"})
    snapshot = state.snapshot()
    assert snapshot["tasks_total"] == 3
    assert snapshot["tasks_completed"] == 2
    assert [row.get("agent_done") for row in snapshot["tasks"]] == ["done", "timeout", None]
    assert len({row["row_key"] for row in snapshot["tasks"]}) == 3


def test_interruption_settles_active_occurrence_and_keeps_queue():
    state = ProgressState()
    state.set_tasks(["repeated", "repeated"])
    state.start_task("repeated")
    state.finish("cancelled")
    snapshot = state.snapshot()
    assert snapshot["status"] == "cancelled"
    assert [row["status"] for row in snapshot["tasks"]] == ["interrupted", "queued"]
    assert snapshot["tasks_completed"] == 0


def test_task_that_fails_before_runtime_never_shows_previous_trace_or_steps():
    state = ProgressState()
    state.set_tasks(["first", "unloadable"])
    state.start_task("first")
    state.set_trace_locator(lambda: "/traces/first")
    state.run_started(task_id="first", agent_id="known", max_steps=10)
    state.recorded(StepRecord(step=0))
    state.finish_task("first", {"trace_dir": "/traces/first", "agent_done": "done"})
    state.start_task("unloadable")
    state.finish_task("unloadable", {"trace_dir": None, "error": "load failed"})
    snapshot = state.snapshot()
    assert snapshot["trace_path"] is None
    assert snapshot["steps"] == []
    assert snapshot["identity"] == {}
    assert snapshot["max_steps"] is None


def test_nested_public_snapshot_cannot_mutate_monitor_state():
    state = ProgressState()
    state.start_task("one")
    state.finish_task("one", {"details": {"values": ["saved"]}})
    state.recorded(
        StepRecord.model_validate(
            {"step": 0, "actions": [{"name": "browser.click", "params": {"index": 7}}]}
        )
    )
    snapshot = state.snapshot()
    snapshot["tasks"][0]["details"]["values"].append("changed")
    snapshot["steps"][0]["detail"]["actions"][0]["params"]["index"] = 99
    fresh = state.snapshot()
    assert fresh["tasks"][0]["details"]["values"] == ["saved"]
    assert fresh["steps"][0]["detail"]["actions"][0]["params"]["index"] == 7
