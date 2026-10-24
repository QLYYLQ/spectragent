"""The UI shares the trace's commit, accounting and containment boundaries."""

import json
from pathlib import Path

import pytest

from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.trace.encoding import PendingLLMCall, encode_call_metrics
from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection import RepositoryError, open_repository
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import (
    PNG_BYTES,
    agent_result,
    build_reference_trace,
    cancelled_call,
    eval_result,
    screenshot,
    step_record,
)


def test_full_inspection_keeps_ledgers_retries_and_trailing_calls(tmp_path: Path) -> None:
    build_reference_trace(tmp_path)
    repo = open_repository(tmp_path)
    row = repo.list_runs()[0]
    assert row["agent_done"] == "timeout"
    assert row["trace_status"] == "complete"
    assert row["tokens"] == 45
    assert row["usage_missing"] == 1
    assert row["llm_calls"] == 4
    run = repo.run(row["key"])
    assert run["trailing_calls"][0]["status"] == "cancelled"
    step = repo.step(row["key"], 0)
    assert [call["status"] for call in step["calls"]] == ["failed", "completed"]
    assert step["output_schemas"]
    assert step["fragments"]["browser.browser_meta"]["error"] == "TimeoutError"
    assert step["fragments"]["browser.screenshot"]["image_bytes"] == {"bytes": len(PNG_BYTES)}
    assert repo.screenshot(row["key"], 0) == PNG_BYTES
    json.dumps(run)
    json.dumps(step)


def test_refresh_reads_new_commits_without_exposing_orphans(tmp_path: Path) -> None:
    wal = WALWriter(tmp_path, "live")
    wal.init_run(TraceConfig())
    repo = open_repository(tmp_path)
    assert repo.run("live")["steps"] == []
    wal.commit_step(0, step_record(0), fragments=[screenshot(0)])
    (wal.run_dir / "steps" / "001.json").write_text(step_record(1).model_dump_json())
    assert len(repo.run("live")["steps"]) == 1
    assert repo.run("live")["summary"]["trace_status"] == "writing"
    assert "running" not in repo.run("live")["summary"]
    with pytest.raises(FileNotFoundError, match="committed"):
        repo.step("live", 1)


def test_missing_usage_is_unknown_and_restored_steps_do_not_use_budget(tmp_path: Path) -> None:
    wal = WALWriter(tmp_path, "unknown")
    wal.init_run(TraceConfig())
    wal.commit_step(0, step_record(0).model_copy(update={"origin": "restored"}))
    wal.commit_trailing(llm_calls=[cancelled_call(1, 0, "pending")])
    row = open_repository(tmp_path).list_runs()[0]
    assert row["tokens"] is None
    assert row["usage_missing"] == 1
    assert row["policy_steps"] == 0
    assert row["total_steps"] == 1


def test_unreadable_trace_stays_visible_and_does_not_hide_healthy_runs(tmp_path: Path) -> None:
    fixture = build_reference_trace(tmp_path, "broken")
    (fixture.run_dir / "steps" / "000.json").write_text("{")
    build_reference_trace(tmp_path, "healthy")
    rows = {row["key"]: row for row in open_repository(tmp_path).list_runs()}
    assert rows["broken"]["trace_status"] == "unreadable"
    assert "000.json" in rows["broken"]["read_error"]
    assert rows["healthy"]["trace_status"] == "complete"


@pytest.mark.parametrize("key", ["../outside", "/etc", "run/../../escape", "..\\outside"])
def test_relative_keys_cannot_leave_root(tmp_path: Path, key: str) -> None:
    repo = open_repository(tmp_path)
    with pytest.raises(RepositoryError):
        repo.run(key)


def test_symlinked_runs_cannot_leave_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    build_reference_trace(outside)
    (root / "escape").symlink_to(outside / "run-test", target_is_directory=True)
    repo = open_repository(root)
    assert repo.list_runs() == []
    with pytest.raises(RepositoryError):
        repo.run("escape")


def test_single_run_root_and_search(tmp_path: Path) -> None:
    fixture = build_reference_trace(tmp_path)
    repo = open_repository(fixture.run_dir)
    assert repo.list_runs("TEST_AGENT")[0]["key"] == "."
    assert repo.list_runs("no such model") == []
    assert repo.run(".")["summary"]["run_id"] == "run-test"


def test_comparison_reports_config_differences_and_task_mismatch(tmp_path: Path) -> None:
    for name, model, task in [
        ("one", "model-one", "first task"),
        ("two", "model-two", "second task"),
    ]:
        wal = WALWriter(tmp_path, name)
        wal.init_run(TraceConfig(task=task, resolved={"llm": [{"model": model}]}))
        wal.write_result(agent_result())
        wal.finalize(TraceStatus.COMPLETE)
    comparison = open_repository(tmp_path).compare(["one", "two"])
    assert comparison["same_task"] is False
    assert comparison["config_differences"] == [
        {"path": "llm[0].model", "values": ["model-one", "model-two"]}
    ]


def test_reports_do_not_expose_arbitrary_files_or_run_contents(tmp_path: Path) -> None:
    fixture = build_reference_trace(tmp_path)
    fake = {"format": "af-independent-evaluation-1", "coverage": 0.5, "success_rate": None}
    (fixture.run_dir / "report.json").write_text(json.dumps(fake))
    report_dir = tmp_path / "evaluation"
    report_dir.mkdir()
    (report_dir / "report.json").write_text(json.dumps(fake))
    (tmp_path / "private.json").write_text('{"secret": "not a report"}')
    repo = open_repository(tmp_path)
    assert repo.report("evaluation/report.json")["data"]["success_rate"] is None
    with pytest.raises(FileNotFoundError):
        repo.report("private.json")
    with pytest.raises(FileNotFoundError):
        repo.report("run-test/report.json")


def test_corrupt_report_is_visible_with_reason(tmp_path: Path) -> None:
    (tmp_path / "report.json").write_text("{")
    repo = open_repository(tmp_path)
    assert repo.reports()[0]["kind"] == "unreadable"
    with pytest.raises(RepositoryError, match="Cannot read"):
        repo.report("report.json")


def test_uncommitted_output_schema_is_an_inspection_error(tmp_path: Path) -> None:
    fixture = build_reference_trace(tmp_path)
    path = fixture.run_dir / "manifest.json"
    data = json.loads(path.read_text())
    data["output_formats"] = []
    path.write_text(json.dumps(data))
    with pytest.raises(RepositoryError):
        open_repository(tmp_path).step("run-test", 0)


def test_the_run_names_its_actions_with_the_wording_marks_owns(tmp_path: Path) -> None:
    # The step labels' verbs come from ``inspection/marks.py``; the page keeps no table.
    build_reference_trace(tmp_path)
    run = open_repository(tmp_path).run("run-test")
    assert run["action_verbs"] == {"browser.click": "点击"}


def test_broken_evaluation_does_not_hide_an_intact_trace(tmp_path: Path) -> None:
    fixture = build_reference_trace(tmp_path)
    (fixture.run_dir / "evals" / "broken.json").write_text("{")
    repo = open_repository(tmp_path)
    row = repo.list_runs()[0]
    assert row["trace_status"] == "complete"
    assert row["read_error"] is None
    assert row["evaluation_error"]
    run = repo.run("run-test")
    assert len(run["steps"]) == 2
    assert run["evaluation_error"]
    assert run["evaluations"] == {}
    assert repo.step("run-test", 0)["calls"]


def test_group_comparison_matches_tasks_and_refuses_cherry_picking(tmp_path: Path) -> None:
    def make(group: str, task: str, verdict: int | None, suffix: str = "") -> str:
        name = f"{group}-{task}{suffix}"
        wal = WALWriter(tmp_path, name)
        config = TraceConfig(task_id=task, benchmark="fixture", resolved={"variant": group})
        wal.init_run(config)
        wal.write_result(agent_result(task_id=task))
        wal.finalize(TraceStatus.COMPLETE)
        if verdict is not None:
            result = eval_result(task_id=task, run_id=name, predicted_label=verdict)
            (wal.run_dir / "evals" / "judge.json").write_text(result.model_dump_json())
        return config.configuration_digest()

    left = make("left", "different", 1)
    right = make("right", "different", 0)
    make("left", "unknown", None)
    make("right", "unknown", 0)
    make("left", "repeated", 1)
    make("left", "repeated", 0, "-retry")
    make("right", "repeated", 0)
    make("left", "missing", 1)
    comparison = open_repository(tmp_path).compare_groups(left, right, "judge")
    assert comparison["counts"]["left_only_passed"] == 1
    assert comparison["counts"]["ungraded"] == 1
    assert comparison["counts"]["ambiguous"] == 1
    assert comparison["counts"]["unpaired"] == 1
    assert sum(comparison["counts"].values()) == 4


def test_different_judge_configurations_are_not_aggregated(tmp_path: Path) -> None:
    hashes = []
    for name in ("left", "right"):
        wal = WALWriter(tmp_path, name)
        config = TraceConfig(task_id="same", resolved={"variant": name})
        hashes.append(config.configuration_digest())
        wal.init_run(config)
        wal.write_result(agent_result())
        wal.finalize(TraceStatus.COMPLETE)
        verdict = eval_result(evaluator_config={"judge_model": name})
        (wal.run_dir / "evals" / "judge.json").write_text(verdict.model_dump_json())
    report = open_repository(tmp_path).compare_groups(hashes[0], hashes[1], "judge")
    assert report["counts"]["incompatible_evaluator"] == 1
    assert report["counts"]["both_passed"] == 0


def test_light_recording_stays_inspectable_and_declares_omitted_trajectory(tmp_path: Path) -> None:
    wal = WALWriter(tmp_path, "light")
    wal.init_run(TraceConfig(recording=RecordingPlan(trajectory=False, observations=False)))
    pending = PendingLLMCall(None, capture_trajectory=False)
    pending.complete(ChatInvokeCompletion(completion="done", usage=None))
    wal.commit_step(2, StepRecord(step=2), call_metrics=[encode_call_metrics(2, 0, pending)])
    wal.finalize(TraceStatus.COMPLETE)
    repo = open_repository(tmp_path)
    run = repo.run("light")
    assert run["summary"]["total_steps"] == 1
    assert run["summary"]["llm_calls"] == 1
    assert run["summary"]["missing_steps"] == [0, 1]
    assert run["summary"]["usage_complete"] is False
    step = repo.step("light", 2)
    assert step["calls"] == [] and step["fragments"] == {}
    assert step["recording"]["trajectory"] is False
    assert step["call_metrics"][0]["step"] == 2


def test_disabled_statistics_are_not_shown_as_zero_calls(tmp_path: Path) -> None:
    wal = WALWriter(tmp_path, "unmeasured")
    wal.init_run(TraceConfig(recording=RecordingPlan(metrics=False)))
    wal.finalize(TraceStatus.COMPLETE)
    row = open_repository(tmp_path).list_runs()[0]
    assert row["llm_calls"] is None and row["tokens"] is None
    assert row["usage_measured"] is False


def _branch_of(base: Path, run_id: str, source_run_id: str) -> Path:
    from spectragent.schemas.restoration import BranchProvenance

    writer = WALWriter(base, run_id)
    writer.init_run(
        TraceConfig(
            branch=BranchProvenance(
                source_run_id=source_run_id,
                source_trace=source_run_id,
                checkpoint_step=1,
                branch_index=0,
                branch_group_id=f"{source_run_id}/s1",
            )
        )
    )
    writer.commit_step(0, StepRecord(step=0, origin="restored"))
    writer.write_result(agent_result())
    writer.finalize(TraceStatus.COMPLETE)
    return writer.run_dir


def test_listed_branches_resolve_their_source_against_the_whole_root(tmp_path: Path) -> None:
    build_reference_trace(tmp_path / "records", "source")
    build_reference_trace(tmp_path / "x", "twin")
    build_reference_trace(tmp_path / "y", "twin")
    _branch_of(tmp_path / "forks", "found", "source")
    _branch_of(tmp_path / "forks", "lost", "gone")
    _branch_of(tmp_path / "forks", "unclear", "twin")
    rows = {row["key"]: row for row in open_repository(tmp_path).list_runs()}
    assert (rows["forks/found"]["source_key"], rows["forks/found"]["source_resolution"]) == (
        "records/source",
        "found",
    )
    assert (rows["forks/lost"]["source_key"], rows["forks/lost"]["source_resolution"]) == (
        None,
        "missing",
    )
    assert rows["forks/unclear"]["source_resolution"] == "ambiguous"
    assert "source_resolution" not in rows["records/source"]
    # A list filter narrows the rows, never the root the source is resolved in.
    only = open_repository(tmp_path).list_runs(search="forks/found")
    assert [row["key"] for row in only] == ["forks/found"]
    assert only[0]["source_key"] == "records/source"
    # The resolution is added after the search: it is not searchable text.
    assert open_repository(tmp_path).list_runs(search="ambiguous") == []
