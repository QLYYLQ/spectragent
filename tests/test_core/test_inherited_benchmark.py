"""``inherited_benchmark``: a browser branch is composed and judged as its source benchmark's.

``docs/architecture_design.md`` §4.10.1 "分叉任务继承源基准落地了什么". Through
the real ``ConfigComposer`` on the shipped flywheel branch configuration: the
source ran ``browsecomp`` from a benchmark YAML of its own that lists
``exact_match``, a verifier judging against the task's reference row. The
branch task's benchmark is that block, read again from the source's record;
its offline verifier judges the branch's trace against the source task's
row, and a verifier the benchmark has no reference rows for stops the sweep
with the same words, ``--no-eval`` hint included, as the selected
benchmark's would.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from spectragent.benchmarks.browsecomp.benchmark import BrowseCompBenchmark
from spectragent.benchmarks.selection import read_benchmark, spec_of
from spectragent.core.benchmark_runner import inherited_benchmark
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.benchmark_spec import BenchmarkSpec
from spectragent.schemas.restoration import BranchProvenance
from spectragent.verifiers.runner import VerificationRunner
from tests._helpers.branch_sources import write_source

CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def _browsecomp(tmp_path: Path) -> BenchmarkSpec:
    """The source's record of selecting a ``browsecomp`` YAML that lists ``exact_match``."""
    data = tmp_path / "reference"
    data.mkdir()
    (data / "data_info.json").write_text(json.dumps({"split": {"All": "all.json"}}))
    rows = [{"task_id": "one", "expected_answer": "42"}, {"task_id": "two", "expected_answer": "7"}]
    (data / "all.json").write_text(json.dumps(rows))
    loader = {"benchmark_name": "browsecomp", "subset": "All", "data_dir": str(data)}
    block = {
        "benchmark_id": "browsecomp",
        "task_loader": loader,
        "eval_loader": loader,
        "verifiers": [{"verifier_id": "exact_match"}],
    }
    path = tmp_path / "my_browsecomp.yaml"
    path.write_text(yaml.safe_dump({"benchmark": [block]}))
    return spec_of(str(path), read_benchmark(str(path)))


def _sweep() -> tuple[dict[str, Any], dict[str, Any]]:
    """The shipped branch run configuration and its branches YAML's task source block."""
    configuration = yaml.safe_load(
        (CONFIGS / "flywheel" / "branch_ui_tars_archive.yaml").read_text()
    )
    return configuration, read_benchmark(str(CONFIGS / "flywheel" / "branches_all_steps.yaml"))


async def test_its_offline_verifier_judges_the_branch_against_its_source_row(
    tmp_path: Path,
) -> None:
    configuration, task_source = _sweep()

    built = inherited_benchmark(configuration, task_source, _browsecomp(tmp_path))

    assert [verifier.verifier_id for verifier in built.verifiers] == ["exact_match"]
    assert built.reference_loader is not None
    assert built.reference_loader.benchmark_name == "browsecomp"
    assert isinstance(built.primary_rule, BrowseCompBenchmark)
    stack = built.compose()
    assert [b.benchmark_id for b in stack.instances("benchmark")] == [
        "trace_branches",
        "browsecomp",
    ]
    branch = write_source(
        tmp_path / "branches",
        "src-s1-b0",
        steps=2,
        archivers=(),
        benchmark="trace_branches",
        subset="All",
        task_id="src/s1/b0",
        branch=BranchProvenance(
            source_run_id="src",
            source_trace="src",
            source_benchmark="browsecomp",
            source_subset="All",
            source_task_id="one",
            checkpoint_step=1,
            branch_index=0,
            branch_group_id="src/s1",
        ),
    )
    judging = VerificationRunner(verifiers=built.verifiers, reference_loader=built.reference_loader)

    report = await judging.run([branch])

    assert report.errors == []
    [verdict] = RunTrace(branch).verdicts().values()
    assert (verdict.verifier.verifier_id, verdict.label) == ("exact_match", 1)
    assert verdict.evidence["source_reference"] == {
        "benchmark": "browsecomp",
        "subset": "All",
        "task_id": "one",
    }


def test_without_evaluation_it_asks_for_no_verifier_and_no_reference(tmp_path: Path) -> None:
    configuration, task_source = _sweep()

    built = inherited_benchmark(configuration, task_source, _browsecomp(tmp_path), evaluate=False)

    assert (list(built.verifiers), built.reference_loader) == ([], None)


def test_a_verifier_it_has_no_reference_rows_for_is_refused_with_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selected benchmark's refusal, in the same words: the sweep stops before it runs."""
    monkeypatch.setattr(BrowseCompBenchmark, "reference_loader", lambda self: None)
    configuration, task_source = _sweep()
    spec = _browsecomp(tmp_path)

    with pytest.raises(ValueError) as refused:
        inherited_benchmark(configuration, task_source, spec)

    message = str(refused.value)
    assert "exact_match" in message and repr(spec.selection) in message
    assert "no reference data" in message and "--no-eval" in message
