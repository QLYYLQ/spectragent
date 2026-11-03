"""``spectragent eval``: a benchmark's offline verifiers judge only the supplied traces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.core.trace.reader import RunTrace
from spectragent.verifiers.cli import Evaluation, evaluate, main, selected_traces
from tests.test_verifiers.conftest import write_trace as _write_trace


def _report(evaluation: Evaluation) -> Any:
    """The batch report, read as the JSON it is written as."""
    return evaluation.report


def write_trace(*args, **kwargs):
    kwargs.setdefault("benchmark", "browsecomp")
    return _write_trace(*args, **kwargs)


def _benchmark_yaml(tmp_path: Path, verifiers: list[dict] | None) -> str:
    """A ``browsecomp`` benchmark YAML whose loaders read the fixture's references."""
    data = tmp_path / "reference"
    data.mkdir(exist_ok=True)
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
    loader = {"benchmark_name": "browsecomp", "subset": "All", "data_dir": str(data)}
    block: dict = {"benchmark_id": "browsecomp", "task_loader": loader, "eval_loader": loader}
    if verifiers is not None:
        block["verifiers"] = verifiers
    path = tmp_path / f"browsecomp_{len(verifiers or [])}.yaml"
    path.write_text(yaml.safe_dump({"benchmark": [block]}))
    return str(path)


def _encrypt(plaintext: str, canary: str) -> str:
    """BrowseComp's obfuscation, the inverse of ``decrypt``."""
    import base64

    from spectragent.benchmarks.browsecomp.verifier import derive_key

    data = plaintext.encode()
    key = derive_key(canary, len(data))
    return base64.b64encode(bytes(a ^ b for a, b in zip(data, key, strict=True))).decode()


@pytest.fixture
def benchmark(tmp_path: Path) -> str:
    return _benchmark_yaml(tmp_path, [{"verifier_id": "exact_match"}])


async def test_only_supplied_runs_are_the_denominator(benchmark, tmp_path):
    traces = tmp_path / "traces"
    first = write_trace(traces, run_id="first", task_id="one", answer="42")
    write_trace(traces, run_id="second", task_id="two", answer="wrong")
    out = tmp_path / "evaluation"

    evaluation = await evaluate(benchmark=benchmark, traces=[traces, first], output=out)

    [verifier] = _report(evaluation)["verifiers"]
    assert verifier["counts"] == {"provided": 2, "judged": 2, "passed": 1, "failed": 1, "errors": 0}
    assert verifier["coverage"] == 1
    assert verifier["success_rate"] == 0.5
    assert len(_report(evaluation)["outcomes"]) == 2
    assert "not-provided" not in (out / "report.json").read_text()
    assert (out / "report.md").is_file()
    assert evaluation.errors == 0


async def test_each_verdict_is_in_its_run_and_the_report_only_points_to_it(benchmark, tmp_path):
    first = write_trace(tmp_path / "traces", run_id="first", task_id="one", answer="42")
    out = tmp_path / "evaluation"

    evaluation = await evaluate(benchmark=benchmark, traces=[first], output=out)

    [row] = _report(evaluation)["outcomes"]
    verdicts = RunTrace(first).verdicts()
    assert list(verdicts) == [row["slug"]]
    assert row["verdict"] == f"verdicts/{row['slug']}.json"
    assert (first / row["verdict"]).is_file()
    # No copy of a verdict in the batch: only the report.
    assert sorted(p.name for p in out.iterdir()) == ["report.json", "report.md"]


async def test_a_rule_and_an_llm_judge_leave_two_verdicts_each_with_its_identity(
    tmp_path, monkeypatch
):
    """A benchmark configured with a rule verifier and an LLM judge: two verdicts in one place."""
    from spectragent.core.llm.providers.openai import ChatOpenAI
    from tests.test_verifiers.conftest import FakeJudge

    judge = FakeJudge(replies=["Explanation.\ncorrect: yes"], model="judge-model")
    monkeypatch.setattr(ChatOpenAI, "ainvoke", lambda self, messages, **kw: judge.ainvoke(messages))
    benchmark = _benchmark_yaml(
        tmp_path,
        [
            {"verifier_id": "exact_match"},
            {
                "verifier_id": "browsecomp",
                "llm": {
                    "model_type": "openai",
                    "provider": {"name": "direct"},
                    "model": "judge-model",
                    "api_key": "test",
                },
            },
        ],
    )
    data = Path(tmp_path / "reference" / "all.json")
    rows = json.loads(data.read_text())
    rows[0]["metadata"] = {"canary": "canary-string"}
    rows[0]["reference"] = {
        "encrypted_question": _encrypt("What is the answer?", "canary-string"),
        "encrypted_answer": _encrypt("42", "canary-string"),
    }
    data.write_text(json.dumps(rows))
    trace = write_trace(tmp_path / "traces", task_id="one", answer="42")

    evaluation = await evaluate(benchmark=benchmark, traces=[trace], output=tmp_path / "eval")

    assert evaluation.errors == 0
    verdicts = RunTrace(trace).verdicts()
    assert len(verdicts) == 2
    by_kind = {verdict.verifier.verifier_id: verdict for verdict in verdicts.values()}
    assert set(by_kind) == {"exact_match", "browsecomp"}
    assert by_kind["exact_match"].verifier.model is None
    assert by_kind["browsecomp"].verifier.model == "judge-model"
    assert by_kind["browsecomp"].verifier.config_signature["llm"] == "judge-model"
    assert all(verdict.label == 1 for verdict in verdicts.values())
    assert [item["verifier"]["verifier_id"] for item in _report(evaluation)["verifiers"]] == [
        "exact_match",
        "browsecomp",
    ]


async def test_an_unjudgeable_trace_is_separate_from_a_judged_wrong_one(benchmark, tmp_path):
    traces = tmp_path / "traces"
    write_trace(traces, run_id="first", task_id="one", answer="42")
    missing = write_trace(traces, run_id="missing", task_id="absent", answer="42")

    evaluation = await evaluate(benchmark=benchmark, traces=[traces], output=tmp_path / "eval")

    [verifier] = _report(evaluation)["verifiers"]
    assert verifier["counts"] == {"provided": 2, "judged": 1, "passed": 1, "failed": 0, "errors": 1}
    assert verifier["coverage"] == 0.5
    assert verifier["success_rate"] == 1
    assert [row["trace"] for row in _report(evaluation)["outcomes"] if row["error"]] == [
        str(missing)
    ]
    assert RunTrace(missing).verdicts() == {}


async def test_no_judged_verdict_has_no_success_rate(benchmark, tmp_path):
    source = write_trace(tmp_path / "traces", task_id="absent")

    evaluation = await evaluate(benchmark=benchmark, traces=[source], output=tmp_path / "eval")

    [verifier] = _report(evaluation)["verifiers"]
    assert verifier["success_rate"] is None
    assert verifier["coverage"] == 0


async def test_dry_run_judges_and_writes_nothing(benchmark, tmp_path):
    source = write_trace(tmp_path / "traces", task_id="one")
    out = tmp_path / "eval"

    evaluation = await evaluate(benchmark=benchmark, traces=[source], output=out, dry_run=True)

    assert _report(evaluation)["dry_run"]
    assert _report(evaluation)["verifiers"][0]["verifier_id"] == "exact_match"
    assert not out.exists()
    assert RunTrace(source).verdicts() == {}


async def test_existing_batch_is_never_replaced(benchmark, tmp_path):
    out = tmp_path / "eval"
    out.mkdir()
    marker = out / "keep"
    marker.write_text("existing")
    with pytest.raises(ValueError, match="already exists"):
        await evaluate(benchmark=benchmark, traces=[], output=out)
    assert marker.read_text() == "existing"


async def test_a_benchmark_with_no_offline_verifier_is_refused(tmp_path):
    source = write_trace(tmp_path / "traces", task_id="one")
    benchmark = _benchmark_yaml(tmp_path, None)

    with pytest.raises(ValueError, match="configures no verifier"):
        await evaluate(benchmark=benchmark, traces=[source], output=tmp_path / "eval")


async def test_branch_uses_source_reference_without_losing_branch_identity(benchmark, tmp_path):
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

    evaluation = await evaluate(benchmark=benchmark, traces=[source], output=tmp_path / "eval")

    [row] = _report(evaluation)["outcomes"]
    assert row["task_id"] == "source/s2/b0"
    [verdict] = RunTrace(source).verdicts().values()
    assert verdict.label == 1
    assert verdict.evidence["source_reference"] == {
        "benchmark": "browsecomp",
        "subset": "All",
        "task_id": "one",
    }


async def test_a_task_variant_branch_is_never_judged_against_its_source_reference(
    benchmark, tmp_path
):
    """The variant ran another task; its source task's reference would judge the wrong one.

    The answer equals the source reference's (``42``), so judging it would
    have produced a confident "correct": it is an error instead, outside the
    judged denominator, and no verdict lands in the trace.
    """
    source = write_trace(
        tmp_path / "traces",
        task_id="source/s2/vwarranty/b0",
        benchmark="trace_branches",
        task="How long is the warranty?",
        answer="42",
    )
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
        "branch_group_id": "source/s2/vwarranty",
        "task_variant": {
            "variant_id": "warranty",
            "task_text": "How long is the warranty?",
            "source_task_text": "What is the answer?",
            "note": None,
        },
    }
    path.write_text(json.dumps(raw))

    evaluation = await evaluate(benchmark=benchmark, traces=[source], output=tmp_path / "eval")

    [verifier] = _report(evaluation)["verifiers"]
    assert verifier["counts"] == {"provided": 1, "judged": 0, "passed": 0, "failed": 0, "errors": 1}
    assert verifier["success_rate"] is None and verifier["coverage"] == 0
    (row,) = _report(evaluation)["outcomes"]
    assert "task variant 'warranty': no reference for this task" in row["error"]
    assert "browsecomp/All/one" in row["error"]
    assert RunTrace(source).verdicts() == {}


def test_cli_exit_code_distinguishes_a_failed_task_from_an_unjudged_one(benchmark, tmp_path):
    judged = write_trace(tmp_path / "traces", run_id="judged", task_id="one", answer="wrong")
    args = ["--benchmark", benchmark, "--traces", str(judged), "--out", str(tmp_path / "a")]
    assert main(args) == 0  # a failed task is still a verdict
    assert main(args) == 2  # the output directory exists now
    absent = write_trace(tmp_path / "other", run_id="absent", task_id="absent")
    args = ["--benchmark", benchmark, "--traces", str(absent), "--out", str(tmp_path / "b")]
    assert main(args) == 1


def test_empty_selection_is_refused(tmp_path):
    with pytest.raises(ValueError, match="No trace manifests"):
        selected_traces([tmp_path])


async def test_same_task_id_from_another_benchmark_is_not_judged(benchmark, tmp_path):
    source = write_trace(tmp_path / "traces", task_id="one", benchmark="another_benchmark")

    evaluation = await evaluate(benchmark=benchmark, traces=[source], output=tmp_path / "eval")

    assert _report(evaluation)["verifiers"][0]["counts"]["judged"] == 0
    assert "benchmark_name" in _report(evaluation)["outcomes"][0]["error"]
