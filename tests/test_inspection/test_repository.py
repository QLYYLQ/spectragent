"""The UI shares the trace's commit, accounting and containment boundaries."""

import json
from pathlib import Path

import pytest

from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.trace.encoding import PendingLLMCall, encode_call_metrics
from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.verdicts import write_verdict
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection import RepositoryError, open_repository
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.step_record import StepRecord
from spectragent.schemas.task import SetupTaskText
from spectragent.schemas.verdict import Verdict
from tests._helpers.trace_builder import (
    PNG_BYTES,
    agent_result,
    build_reference_trace,
    cancelled_call,
    screenshot,
    step_record,
    verdict,
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


def test_review_can_defer_prompt_images_without_losing_replies(tmp_path: Path) -> None:
    build_reference_trace(tmp_path)
    repo = open_repository(tmp_path)
    full = repo.step("run-test", 0)
    preview = repo.step("run-test", 0, include_messages=False)
    assert len(preview["calls"]) == len(full["calls"])
    for small, original in zip(preview["calls"], full["calls"], strict=True):
        assert original["messages"]
        assert "messages" not in small
        assert small == {
            **{key: value for key, value in original.items() if key != "messages"},
            "messages_omitted": True,
        }
    assert preview["record"] == full["record"]


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


def _run_with_setup_text(base: Path, run_id: str, *, record: bool) -> None:
    """A run whose setup was to complete its task text, with its record or with it lost."""
    wal = WALWriter(base, run_id)
    wal.init_run(
        TraceConfig(task="Follow the instructions.", task_id="t-1", task_text_setter="android")
    )
    if record:
        wal.commit_task_text(
            SetupTaskText(capability_id="android", text="Follow the instructions: Click Submit.")
        )
    wal.commit_step(0, step_record(0))
    wal.write_result(agent_result(task_id="t-1"))


def test_a_run_shows_the_task_text_its_setup_completed_and_refuses_a_lost_one(
    tmp_path: Path,
) -> None:
    """Never the loader's half for a run whose agent was given the whole text (§4.10)."""
    _run_with_setup_text(tmp_path, "made", record=True)
    _run_with_setup_text(tmp_path, "lost", record=False)
    rows = {row["key"]: row for row in open_repository(tmp_path).list_runs()}
    assert rows["made"]["task"] == "Follow the instructions: Click Submit."
    assert rows["lost"]["trace_status"] == "unreadable"
    assert "has no task_text.json" in rows["lost"]["read_error"]


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
    fake = {
        "format": "af-independent-evaluation-2",
        "benchmark": "fixture",
        "provided": 2,
        "verifiers": [{"slug": verdict().slug, "coverage": 0.5, "success_rate": None}],
        "outcomes": [],
    }
    (fixture.run_dir / "report.json").write_text(json.dumps(fake))
    report_dir = tmp_path / "evaluation"
    report_dir.mkdir()
    (report_dir / "report.json").write_text(json.dumps(fake))
    (tmp_path / "private.json").write_text('{"secret": "not a report"}')
    repo = open_repository(tmp_path)
    report = repo.report("evaluation/report.json")
    assert report["kind"] == "evaluation"
    assert report["data"]["verifiers"][0]["success_rate"] is None
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


def test_a_broken_verdict_does_not_hide_an_intact_trace(tmp_path: Path) -> None:
    fixture = build_reference_trace(tmp_path)
    (fixture.run_dir / "verdicts").mkdir()
    (fixture.run_dir / "verdicts" / "broken.json").write_text("{")
    repo = open_repository(tmp_path)
    row = repo.list_runs()[0]
    assert row["trace_status"] == "complete"
    assert row["read_error"] is None
    assert row["verdict_error"]
    assert row["verdicts"] == []
    run = repo.run("run-test")
    assert len(run["steps"]) == 2
    assert run["verdict_error"]
    assert run["verdicts"] == {}
    assert repo.step("run-test", 0)["calls"]


def test_a_run_lists_each_verdict_by_slug_and_a_comparison_lines_them_up(tmp_path: Path) -> None:
    judged = build_reference_trace(tmp_path, "judged")
    build_reference_trace(tmp_path, "unjudged")
    live = verdict("androidworld", label=None, score=0.5, attachment="live")
    write_verdict(judged.run_dir, live)
    repo = open_repository(tmp_path)
    rows = {row["key"]: row for row in repo.list_runs()}
    assert rows["judged"]["verdicts"] == [
        {
            "slug": live.slug,
            "verifier_id": "androidworld",
            "attachment": "live",
            "status": "judged",
            "label": None,
            "score": 0.5,
        }
    ]
    assert rows["judged"]["verdict_error"] is None
    assert rows["unjudged"]["verdicts"] == []
    assert repo.run("judged")["verdicts"] == {live.slug: live.model_dump(mode="json")}
    comparison = repo.compare(["judged", "unjudged"])
    assert comparison["verifier_comparisons"] == [
        {"slug": live.slug, "verdicts": [live.model_dump(mode="json"), None]}
    ]


def test_group_comparison_matches_tasks_and_refuses_cherry_picking(tmp_path: Path) -> None:
    def make(group: str, task: str, found: Verdict | None, suffix: str = "") -> str:
        wal = WALWriter(tmp_path, f"{group}-{task}{suffix}")
        config = TraceConfig(task_id=task, benchmark="fixture", resolved={"variant": group})
        wal.init_run(config)
        wal.write_result(agent_result(task_id=task))
        wal.finalize(TraceStatus.COMPLETE)
        if found is not None:
            write_verdict(wal.run_dir, found)
        return config.configuration_digest()

    passed, failed = verdict(label=1), verdict(label=0)
    left = make("left", "different", passed)
    right = make("right", "different", failed)
    make("left", "unknown", None)
    make("right", "unknown", failed)
    make("left", "repeated", passed)
    make("left", "repeated", failed, "-retry")
    make("right", "repeated", failed)
    make("left", "missing", passed)
    # A verdict that judged nothing, or found a score and no pass or fail, is
    # never a failed task.
    make("left", "timed-out", verdict(status="not_judged", label=None, reason="timeout"))
    make("right", "timed-out", passed)
    make("left", "scored", verdict(label=None, score=0.4))
    make("right", "scored", passed)
    comparison = open_repository(tmp_path).compare_groups(left, right, passed.slug)
    assert comparison["verifier"] == passed.slug
    assert comparison["counts"]["left_only_passed"] == 1
    assert comparison["counts"]["unjudged"] == 3
    assert comparison["counts"]["ambiguous"] == 1
    assert comparison["counts"]["unpaired"] == 1
    assert sum(comparison["counts"].values()) == 6


def test_different_judge_configurations_are_not_aggregated(tmp_path: Path) -> None:
    """Two settings of one verifier are two slugs: neither side's verdict pairs with the other."""
    hashes, slugs = [], []
    for name in ("left", "right"):
        wal = WALWriter(tmp_path, name)
        config = TraceConfig(task_id="same", resolved={"variant": name})
        hashes.append(config.configuration_digest())
        wal.init_run(config)
        wal.write_result(agent_result())
        wal.finalize(TraceStatus.COMPLETE)
        slugs.append(write_verdict(wal.run_dir, verdict(config_signature={"llm": name})).stem)
    assert slugs[0] != slugs[1]
    report = open_repository(tmp_path).compare_groups(hashes[0], hashes[1], slugs[0])
    assert report["counts"]["unjudged"] == 1
    assert report["counts"]["both_passed"] == 0


def test_one_verifier_of_two_versions_is_not_aggregated(tmp_path: Path) -> None:
    """One slug, two versions of the verifier behind it: not one judge, so never paired."""
    hashes = []
    for name, version in (("left", "v1"), ("right", "v2")):
        wal = WALWriter(tmp_path, name)
        config = TraceConfig(task_id="same", resolved={"variant": name})
        hashes.append(config.configuration_digest())
        wal.init_run(config)
        wal.write_result(agent_result())
        wal.finalize(TraceStatus.COMPLETE)
        judged = verdict("online_mind2web", label=1)
        judged = judged.model_copy(
            update={"verifier": judged.verifier.model_copy(update={"version": version})}
        )
        slug = write_verdict(wal.run_dir, judged).stem
    report = open_repository(tmp_path).compare_groups(hashes[0], hashes[1], slug)
    assert report["counts"]["incompatible_verifier"] == 1
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


def test_a_run_states_once_the_facts_no_checked_step_measured(tmp_path: Path) -> None:
    from spectragent.inspection.restoration import unmeasured_everywhere

    build_reference_trace(tmp_path, "source")
    _branch_of(tmp_path, "fork", "source")
    repo = open_repository(tmp_path)
    fork = repo.run("fork")
    assert fork["unmeasured_everywhere"] == {
        "facts": ["Restoration check not recorded"],
        "checked_steps": 1,
    }
    # The step's own gap keeps the fact; only the run states it once.
    assert fork["steps"][0]["gap"]["unmeasured"] == ["Restoration check not recorded"]
    assert repo.run("source")["unmeasured_everywhere"] == {"facts": [], "checked_steps": 0}
    varying = [
        {"step": 0, "origin": "restored", "restoration": {"verdict": "ok", "evidence": []}},
        {"step": 1, "origin": "restored"},
    ]
    # Facts that differ between checked steps are not stated for the run.
    assert unmeasured_everywhere(varying) == {"facts": [], "checked_steps": 2}
