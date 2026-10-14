"""Independent evaluation composes real YAML and scores only supplied traces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator.cli import evaluate, main, selected_traces
from tests.test_evaluator.conftest import write_trace as _write_trace


def write_trace(*args, **kwargs):
    kwargs.setdefault("benchmark", "browsecomp")
    return _write_trace(*args, **kwargs)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    data = tmp_path / "reference"
    data.mkdir()
    (data / "data_info.json").write_text(json.dumps({"split": {"All": "all.json"}}))
    (data / "all.json").write_text(
        json.dumps(
            [
                {"task_id": "one", "expected_answer": "42"},
                {"task_id": "two", "expected_answer": "Paris"},
                {"task_id": "not-provided", "expected_answer": "unused"},
            ]
        )
    )
    path = tmp_path / "evaluation.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "evaluator": [{"evaluator_id": "exact_match"}],
                "eval_loader": [
                    {"benchmark_name": "browsecomp", "subset": "All", "data_dir": str(data)}
                ],
            }
        )
    )
    return path


async def test_only_supplied_runs_are_the_denominator(config, tmp_path):
    traces = tmp_path / "traces"
    first = write_trace(traces, run_id="first", task_id="one", answer="42", benchmark="browsecomp")
    write_trace(traces, run_id="second", task_id="two", answer="wrong", benchmark="browsecomp")
    out = tmp_path / "evaluation"
    report = await evaluate(config=config, traces=[traces, first], output=out)
    assert report["counts"] == {"provided": 2, "graded": 2, "passed": 1, "failed": 1, "errors": 0}
    assert report["coverage"] == 1
    assert report["success_rate"] == 0.5
    assert len(report["outcomes"]) == 2
    assert "not-provided" not in (out / "report.json").read_text()
    assert RunTrace(first).evals()
    assert (out / "report.md").is_file()


async def test_unscorable_trace_is_separate_from_graded_wrong(config, tmp_path):
    traces = tmp_path / "traces"
    write_trace(traces, run_id="first", task_id="one", answer="42")
    write_trace(traces, run_id="missing", task_id="absent", answer="42")
    report = await evaluate(config=config, traces=[traces], output=tmp_path / "eval")
    assert report["counts"] == {"provided": 2, "graded": 1, "passed": 1, "failed": 0, "errors": 1}
    assert report["coverage"] == 0.5
    assert report["success_rate"] == 1
    assert len([row for row in report["outcomes"] if row["error"]]) == 1


async def test_no_valid_verdict_has_no_success_rate(config, tmp_path):
    source = write_trace(tmp_path / "traces", task_id="absent")
    report = await evaluate(config=config, traces=[source], output=tmp_path / "eval")
    assert report["success_rate"] is None
    assert report["coverage"] == 0


async def test_dry_run_and_read_only_keep_trace_evaluations_empty(config, tmp_path):
    source = write_trace(tmp_path / "traces", task_id="one")
    out = tmp_path / "eval"
    report = await evaluate(config=config, traces=[source], output=out, dry_run=True)
    assert report["dry_run"]
    assert not out.exists()
    assert not RunTrace(source).evals()
    await evaluate(config=config, traces=[source], output=out, write_into_trace=False)
    assert not RunTrace(source).evals()
    assert (out / "report.json").is_file()


async def test_existing_batch_is_never_replaced(config, tmp_path):
    out = tmp_path / "eval"
    out.mkdir()
    marker = out / "keep"
    marker.write_text("existing")
    with pytest.raises(ValueError, match="already exists"):
        await evaluate(config=config, traces=[], output=out)
    assert marker.read_text() == "existing"


async def test_branch_uses_source_reference_without_losing_branch_identity(config, tmp_path):
    source = write_trace(tmp_path / "traces", task_id="source/s2/b0", benchmark="trace_branches")
    path = source / "config.json"
    raw = json.loads(path.read_text())
    raw["branch"] = {
        "source_run_id": "source",
        "source_trace": "source",
        "source_benchmark": "browsecomp",
        "source_subset": "All",
        "source_task_id": "one",
        "checkpoint_step": 2,
        "branch_index": 0,
        "branch_group_id": "source/s2",
    }
    path.write_text(json.dumps(raw))
    report = await evaluate(config=config, traces=[source], output=tmp_path / "eval")
    result = report["outcomes"][0]["result"]
    assert result["task_id"] == "source/s2/b0"
    assert result["predicted_label"] == 1
    assert result["evaluation_details"]["benchmark_details"]["source_reference"]["task_id"] == "one"


def test_cli_exit_code_distinguishes_wrong_answer_from_evaluation_error(config, tmp_path):
    source = write_trace(tmp_path / "traces", task_id="one", answer="wrong")
    args = ["--config", str(config), "--traces", str(source), "--out", str(tmp_path / "eval")]
    assert main(args) == 0
    assert main(args) == 2


def test_empty_selection_is_refused(tmp_path):
    with pytest.raises(ValueError, match="No trace manifests"):
        selected_traces([tmp_path])


async def test_same_task_id_from_another_benchmark_is_not_judged(config, tmp_path):
    source = write_trace(tmp_path / "traces", task_id="one", benchmark="another_benchmark")
    report = await evaluate(config=config, traces=[source], output=tmp_path / "eval")
    assert report["counts"]["graded"] == 0
    assert "benchmark_name" in report["outcomes"][0]["error"]
