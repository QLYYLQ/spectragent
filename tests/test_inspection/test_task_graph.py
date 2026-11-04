"""One task's runs placed as a git-like graph from recorded provenance, never from names."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection import open_repository
from spectragent.inspection.graph import EXPANDED_RUNS, run_graph, task_graph, task_keys
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.restoration import BranchProvenance, RestorationCheck
from spectragent.schemas.step_record import StepRecord
from spectragent.viewer import create_app
from tests._helpers.trace_builder import agent_result, step_record

TASK = '["web", "L1", "118"]'


def _live(key: str, created: str = "01", **fields: Any) -> dict[str, Any]:
    return {
        "key": key,
        "task_key": TASK,
        "benchmark": "web",
        "subset": "L1",
        "task_id": "118",
        "task": "Find a video",
        "created_at": f"2026-11-01T00:00:{created}",
        "agent_id": "a",
        "model": "m",
        "config_hash": "h",
        "missing_steps": [],
        "verdicts": [],
        **fields,
    }


def _branch(key: str, source: str, checkpoint: int, created: str = "09", **fields: Any) -> dict:
    branch = {
        "source_run_id": source,
        "source_benchmark": "web",
        "source_subset": "L1",
        "source_task_id": "118",
        "checkpoint_step": checkpoint,
    }
    row = _live(key, created, task_id=f"{source}/s{checkpoint}", branch=branch)
    return {**row, "source_key": source, "source_resolution": "found", **fields}


def _placed(graph: dict[str, Any]) -> dict[str, tuple[Any, Any, int]]:
    return {
        line["key"]: (
            (line["parent"]["key"], line["parent"]["step"]) if line["parent"] else None,
            line["detached"],
            line["depth"],
        )
        for line in graph["lines"]
    }


def test_a_branch_leaves_the_commit_before_its_checkpoint_and_follows_its_source() -> None:
    rows = [
        _live("live"),
        _branch("at-two", "live", 2),
        _branch("at-zero", "live", 0, created="10"),
    ]
    graph = task_graph(rows, TASK)
    assert _placed(graph) == {
        "live": (None, None, 0),
        "at-zero": (("live", -1), None, 1),
        "at-two": (("live", 1), None, 1),
    }
    # drawn top-down: the branch leaving the start comes before the one leaving step 1
    assert [line["key"] for line in graph["lines"]] == ["live", "at-zero", "at-two"]
    assert [(fork["parent"], fork["step"], fork["branches"]) for fork in graph["forks"]] == [
        ("live", -1, ["at-zero"]),
        ("live", 1, ["at-two"]),
    ]


def test_a_step_in_a_source_branchs_restored_prefix_leaves_the_run_that_decided_it() -> None:
    rows = [_live("live"), _branch("first", "live", 4), _branch("nested", "first", 3, "10")]
    placed = _placed(task_graph(rows, TASK))
    # step 2 of "first" was restored from "live", which decided it
    assert placed["nested"] == (("live", 2), None, 1)
    rows[2]["branch"]["checkpoint_step"] = 6
    assert _placed(task_graph(rows, TASK))["nested"] == (("first", 5), None, 2)


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"source_resolution": "missing", "source_key": None}, "找不到源运行"),
        ({"source_resolution": "ambiguous", "source_key": None}, "多份记录"),
        ({"source_resolution": "outside_root", "source_key": None}, "根目录"),
        ({"source_key": "orphan"}, "来源引用了运行自身"),
    ],
)
def test_a_source_that_cannot_be_confirmed_draws_no_edge(patch: dict, reason: str) -> None:
    rows = [_live("live"), _branch("orphan", "live", 2, **patch)]
    line = next(line for line in task_graph(rows, TASK)["lines"] if line["key"] == "orphan")
    assert line["parent"] is None and reason in line["detached"]
    # a line without an edge comes after the connected ones
    assert task_graph(rows, TASK)["lines"][-1]["key"] == "orphan"


def test_a_parent_commit_that_is_a_gap_draws_no_edge() -> None:
    rows = [_live("live", missing_steps=[1]), _branch("child", "live", 2)]
    line = task_graph(rows, TASK)["lines"][1]
    assert line["parent"] is None and "第 1 步没有提交" in line["detached"]


def test_cyclic_provenance_is_placed_once_without_an_edge() -> None:
    rows = [_branch("a", "b", 3), _branch("b", "a", 3, "10")]
    graph = task_graph(rows, TASK)
    assert sorted(line["key"] for line in graph["lines"]) == ["a", "b"]
    assert any("循环" in (line["detached"] or "") for line in graph["lines"])


def test_labels_bold_what_differs_and_number_runs_that_differ_in_nothing() -> None:
    rows = [_live("one"), _live("two", "02"), _live("three", "03", agent_id="b")]
    graph = task_graph(rows, TASK)
    assert graph["differs"] == ["agent_id"]
    assert {line["key"]: line["ordinal"] for line in graph["lines"]} == {
        "one": 1,
        "two": 2,
        "three": None,
    }


def test_a_crowded_task_folds_its_fork_points_with_counts_from_the_list_alone() -> None:
    def judged(label: int | None) -> list[dict[str, Any]]:
        return [{"slug": "v", "status": "judged", "label": label, "score": 1.0}]

    branches = [
        _branch(
            f"b{i}",
            "live",
            1,
            f"{10 + i}",
            agent_done="done" if i else "max_steps",
            restoration={"prefix": {"ok": 1, "warn": 0}, "checkpoint": {"verdict": "warn"}}
            if i == 3
            else {"prefix": {"ok": 1}, "checkpoint": {"verdict": "ok"}},
            verdicts=judged(1 if i % 2 else 0),
        )
        for i in range(EXPANDED_RUNS)
    ]
    graph = task_graph([_live("live"), *branches], TASK)
    assert graph["collapse"]
    (fork,) = graph["forks"]
    assert fork["summary"]["restoration"] == {"ok": EXPANDED_RUNS - 1, "warn": 1}
    assert fork["summary"]["ended"] == {"max_steps": 1, "done": EXPANDED_RUNS - 1}
    assert fork["summary"]["verdicts"] == [
        {"slug": "v", "outcome": "失败", "count": 4},
        {"slug": "v", "outcome": "成功", "count": 4},
    ]
    assert not task_graph([_live("live"), *branches[:-1]], TASK)["collapse"]


def test_an_incomplete_task_identity_joins_only_a_resolved_source() -> None:
    rows = [
        _live("a", task_key=None),
        _live("b", task_key=None),
        {**_branch("child", "a", 0), "task_key": None},
        {**_branch("lost", "a", 0), "task_key": None, "source_resolution": "ambiguous"},
    ]
    task_keys(rows)
    keys = {row["key"]: (row["task_key"], row["task_incomplete"]) for row in rows}
    assert keys == {
        "a": ("run:a", True),
        "b": ("run:b", True),
        "child": ("run:a", True),
        "lost": ("run:lost", True),
    }


def test_an_unknown_task_is_refused() -> None:
    with pytest.raises(LookupError):
        task_graph([_live("live")], '["other", "", "1"]')


# ── through recorded traces ───────────────────────────────────────────


def _provenance(source: str, checkpoint: int) -> BranchProvenance:
    return BranchProvenance(
        source_run_id=source,
        source_trace=source,
        source_benchmark="web",
        source_subset="L1",
        source_task_id="118",
        checkpoint_step=checkpoint,
        branch_index=0,
        branch_group_id="g",
    )


def _check(step: int, verdict: str, *reasons: str) -> RestorationCheck:
    first = reasons[0] if reasons else None
    return RestorationCheck(step=step, verdict=verdict, reason=first, reasons=list(reasons))  # type: ignore[arg-type]


def _record(root: Path, name: str, config: TraceConfig, records: list[StepRecord]) -> None:
    wal = WALWriter(root, name)
    wal.init_run(config)
    for record in records:
        wal.commit_step(record.step, record)
    wal.write_result(agent_result())
    wal.finalize(TraceStatus.COMPLETE)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    live = TraceConfig(benchmark="web", subset="L1", task_id="118")
    _record(tmp_path, "live", live, [step_record(i) for i in range(3)])
    restored = step_record(0).model_copy(
        update={"origin": "restored", "restoration": _check(0, "ok")}
    )
    checkpoint = step_record(1).model_copy(
        update={"restoration": _check(1, "warn", "action_failed")}
    )
    _record(
        tmp_path,
        "branch",
        TraceConfig(branch=_provenance("live", 1), resolved={"variant": "branch"}),
        [restored, checkpoint, StepRecord(step=2, origin="terminal")],
    )
    return tmp_path


def test_rows_carry_the_task_key_and_a_branchs_restoration_tally(root: Path) -> None:
    rows = {row["key"]: row for row in open_repository(root).list_runs()}
    assert rows["live"]["task_key"] == rows["branch"]["task_key"] == TASK
    assert rows["live"]["restoration"] is None
    assert rows["branch"]["restoration"] == {
        "restored": 1,
        "prefix": {"ok": 1, "warn": 0, "diverged": 0},
        "checkpoint": {"verdict": "warn", "reasons": ["action_failed"]},
    }


def test_the_run_graph_counts_calls_only_when_metrics_were_recorded(tmp_path: Path) -> None:
    for name, metrics in (("measured", True), ("unmeasured", False)):
        recording = RecordingPlan(metrics=metrics)
        check = step_record(0).model_copy(
            update={"restoration": _check(0, "warn", "retarget_not_found")}
        )
        _record(tmp_path, name, TraceConfig(recording=recording), [check])
    measured = run_graph(RunTrace(tmp_path / "measured"))["steps"][0]
    assert measured["calls"] == 0
    assert measured["restoration_reasons"] == ["retarget_not_found"]
    assert run_graph(RunTrace(tmp_path / "unmeasured"))["steps"][0]["calls"] is None


def test_the_task_graph_endpoint_keeps_a_filtered_source_as_context(root: Path) -> None:
    branch_hash = RunTrace(root / "branch").manifest.config_hash
    client = TestClient(create_app(root, config_hash=branch_hash), base_url="http://127.0.0.1")
    assert [row["key"] for row in client.get("/api/runs").json()] == ["branch"]
    (graph,) = client.get("/api/task-graph", params={"task": TASK}).json()
    assert [(line["key"], line["context"]) for line in graph["lines"]] == [
        ("live", True),
        ("branch", False),
    ]
    assert graph["lines"][1]["parent"] == {"key": "live", "step": 0}
    # a task with no readable run is left out; the others are still placed
    gone = client.get("/api/task-graph", params={"task": ['["x", "", "1"]', TASK]})
    assert [graph["task"]["key"] for graph in gone.json()] == [TASK]
    assert client.get("/api/task-graph").status_code == 422
