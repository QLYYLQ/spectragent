"""A benchmark owns its task loader and its reference data (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from spectragent.benchmarks import BenchmarkRegistrar
from spectragent.core.composer import ComposeError, ConfigComposer
from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar


def _data(root: Path, benchmark: str, subset: str, *task_ids: str) -> Path:
    """``<root>/browser/<benchmark>``: one split of tasks, each its own reference."""
    directory = root / "browser" / benchmark
    directory.mkdir(parents=True, exist_ok=True)
    info = directory / "data_info.json"
    split = json.loads(info.read_text())["split"] if info.is_file() else {}
    info.write_text(json.dumps({"split": {**split, subset: f"{subset}.json"}}))
    records = [
        {"task_id": task_id, "task_text": task_id, "expected_answer": f"answer {task_id}"}
        for task_id in task_ids
    ]
    (directory / f"{subset}.json").write_text(json.dumps(records))
    return directory


def _benchmark(**block: object) -> object:
    raw = {"benchmark": [block]}
    return ConfigComposer.for_config(raw).compose(raw).single("benchmark")


def test_each_benchmark_takes_its_own_loaders() -> None:
    """A benchmark writes its complete wiring: its own task loader, and eval loader when it has one."""
    ConfigComposer.for_every_package()
    owned = {
        key: (cls.__wiring__["task_loader"][1], cls.__wiring__.get("eval_loader", (None, []))[1])
        for key, cls in BenchmarkRegistrar.get_all().items()
    }
    assert owned == {
        "androidworld": (["androidworld"], []),
        "browsecomp": (["browsecomp"], ["browsecomp"]),
        "lexbench_browser": (["lexbench_browser"], ["lexbench_browser"]),
        "online_mind2web": (["online_mind2web"], ["online_mind2web"]),
        "trace_branches": (["trace_branches"], []),
    }
    for key, (tasks, references) in owned.items():
        assert TaskLoaderRegistrar.get(tasks[0]) is not None, key
        for reference in references:
            assert EvalLoaderRegistrar.get(reference) is not None, key


def test_a_benchmark_block_needs_its_task_loader() -> None:
    raw = {"benchmark": [{"benchmark_id": "online_mind2web"}]}
    with pytest.raises(ValidationError) as refused:
        ConfigComposer.for_config(raw).validate(raw)
    (error,) = refused.value.errors()
    assert (error["type"], error["loc"][-1]) == ("missing", "task_loader")


def test_another_benchmarks_loader_is_refused() -> None:
    raw = {
        "benchmark": [
            {"benchmark_id": "online_mind2web", "task_loader": {"benchmark_name": "browsecomp"}}
        ]
    }
    with pytest.raises(ValidationError) as refused:
        ConfigComposer.for_config(raw).validate(raw)
    (error,) = refused.value.errors()
    assert error["type"] == "literal_error" and "online_mind2web" in error["msg"]


def test_an_unknown_benchmark_is_refused_before_anything_is_imported() -> None:
    raw = {"benchmark": [{"benchmark_id": "online_mindweb", "task_loader": {}}]}
    with pytest.raises(ComposeError) as refused:
        ConfigComposer.for_config(raw)
    assert refused.value.issue is not None
    assert refused.value.issue.code == "unknown_benchmark_package"
    assert "benchmark[0].benchmark_id" in str(refused.value)


def test_the_references_are_the_eval_loader_block(tmp_path: Path) -> None:
    directory = _data(tmp_path, "online_mind2web", "Hard", "t1")
    loader = {"benchmark_name": "online_mind2web", "subset": "Hard", "data_dir": str(directory)}
    benchmark = _benchmark(benchmark_id="online_mind2web", task_loader=loader, eval_loader=loader)
    references = benchmark.reference_loader()  # type: ignore[attr-defined]
    assert references is benchmark.eval_loader  # type: ignore[attr-defined]
    assert references.get("t1").expected_answer == "answer t1"


def test_without_a_block_the_references_are_the_registered_loader_for_the_task_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path))
    _data(tmp_path, "online_mind2web", "All", "a1")
    _data(tmp_path, "online_mind2web", "Hard", "h1")
    benchmark = _benchmark(
        benchmark_id="online_mind2web",
        task_loader={"benchmark_name": "online_mind2web", "subset": "Hard"},
    )
    assert benchmark.eval_loader is None  # type: ignore[attr-defined]
    references = benchmark.reference_loader()  # type: ignore[attr-defined]
    assert (references.subset, references.task_ids()) == ("Hard", ["h1"])


def test_a_branch_sweep_has_no_references(tmp_path: Path) -> None:
    benchmark = _benchmark(
        benchmark_id="trace_branches",
        task_loader={
            "benchmark_name": "trace_branches",
            "data_dir": str(tmp_path),
            "step_selector": {"selector_id": "all"},
        },
    )
    assert benchmark.reference_loader() is None  # type: ignore[attr-defined]
    assert benchmark.task_loader.task_ids() == []  # type: ignore[attr-defined]
