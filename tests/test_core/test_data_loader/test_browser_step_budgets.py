"""Each browser benchmark supplies the step budget its benchmark YAML decides, and only that.

``docs/architecture_design.md`` §4.10.1 "基准的步数预算": Online-Mind2Web's
task loader hands each task its required ``step_budget`` (the shipped YAML sets
the paper's 25, Appendix E) as the Loop input ``max_steps``, and nothing when
it is null; the official 25 stays on the loader as data. BrowseComp and
LexBench-Browser state none, so their tasks hand none and the configured
``max_steps`` applies. The configured one stays the ceiling either way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.composer import ConfigComposer
from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.data_loader.task import (
    BrowseCompTaskLoader,
    LexBenchBrowserTaskLoader,
    OnlineMind2WebTaskLoader,
)
from spectragent.core.loops import ReactLoop
from spectragent.schemas.task import TaskContext

_TASKS = [
    {"task_id": "m2w-1", "task_text": "Find a flight", "start_url": "https://example.com"},
    {"task_id": "m2w-2", "task_text": "Find a hotel", "start_url": "https://example.org"},
]


def _data(tmp_path: Path, *records: dict[str, str]) -> Path:
    """A data directory holding *records* as its ``All`` subset."""
    (tmp_path / "data_info.json").write_text(json.dumps({"split": {"All": "All.json"}}))
    (tmp_path / "All.json").write_text(json.dumps(list(records)))
    return tmp_path


def _composed(tmp_path: Path, **loader: object) -> OnlineMind2WebTaskLoader:
    """The Online-Mind2Web task loader a benchmark block with these task loader settings composes."""
    block = {"benchmark_name": "online_mind2web", "data_dir": str(_data(tmp_path, *_TASKS))}
    raw = {"benchmark": [{"benchmark_id": "online_mind2web", "task_loader": {**block, **loader}}]}
    return ConfigComposer.for_config(raw).compose(raw).single("benchmark").task_loader


@pytest.mark.parametrize("budget", [25, 50])
def test_the_budget_the_benchmark_yaml_sets_reaches_every_task(tmp_path: Path, budget: int) -> None:
    loader = _composed(tmp_path, step_budget=budget)

    for task_id in loader.task_ids():
        context = loader.get_task_context(task_id)
        assert context.loop == {"max_steps": budget}
        assert context.env["start_url"].startswith("https://")
        ReactLoop().check_loop_inputs(context.loop)


def test_null_gives_no_budget(tmp_path: Path) -> None:
    loader = _composed(tmp_path, step_budget=None)
    context = loader.get_task_context("m2w-1")
    assert context.loop == {} and context.env == {"start_url": "https://example.com"}
    assert ReactLoop(max_steps=40).step_budget(context.loop) == 40


def test_a_benchmark_yaml_that_leaves_it_out_is_refused_naming_it(tmp_path: Path) -> None:
    """Required: a default would decide every run's budget without anyone writing it."""
    with pytest.raises(ValidationError) as refused:
        _composed(tmp_path)
    (error,) = refused.value.errors()
    assert error["type"] == "missing"
    assert error["loc"][-2:] == ("task_loader", "step_budget")


@pytest.mark.parametrize("budget", [0, -1, True, "25"])
def test_a_budget_that_is_not_a_positive_integer_is_refused(tmp_path: Path, budget: object) -> None:
    with pytest.raises(ValidationError) as refused:
        _composed(tmp_path, step_budget=budget)
    (error,) = refused.value.errors()
    assert error["loc"][-1] == "step_budget"


def test_the_shipped_yaml_sets_the_papers_25_and_the_loader_keeps_it_as_data() -> None:
    shipped = yaml.safe_load(Path("configs/benchmarks/online_mind2web.yaml").read_text())
    assert shipped["benchmark"][0]["task_loader"]["step_budget"] == 25
    official = OnlineMind2WebTaskLoader.__step_budget__
    assert official is not None and (official.value, official.steps) == (25, "25 steps")
    assert "Appendix E" in official.source and "arxiv.org/abs/2504.01382v4" in official.source


@pytest.mark.parametrize(("ceiling", "steps"), [(500, 25), (25, 25), (10, 10)])
def test_the_configured_max_steps_stays_the_ceiling(
    tmp_path: Path, ceiling: int, steps: int
) -> None:
    """The task's 25 lower a higher ceiling and never raise a lower one."""
    context = _composed(tmp_path, step_budget=25).get_task_context("m2w-1")
    assert ReactLoop(max_steps=ceiling).step_budget(context.loop) == steps


def test_an_unknown_task_brings_no_budget(tmp_path: Path) -> None:
    assert _composed(tmp_path, step_budget=25).get_task_context("no-such-task") == TaskContext()


@pytest.mark.parametrize(
    ("cls", "record"),
    [
        (BrowseCompTaskLoader, {"task_id": "bc-1", "task_text": "(encrypted)"}),
        (
            LexBenchBrowserTaskLoader,
            {"task_id": "lex-1", "task_text": "Search", "start_url": "www.example.com"},
        ),
    ],
)
def test_a_benchmark_whose_protocol_states_no_budget_brings_none(
    tmp_path: Path,
    cls: type[BrowseCompTaskLoader] | type[LexBenchBrowserTaskLoader],
    record: dict[str, str],
) -> None:
    """BrowseComp and LexBench-Browser: the configured ``max_steps`` applies to every task."""
    loader = cls(data_dir=str(_data(tmp_path, record)))

    context = loader.get_task_context(record["task_id"])
    assert context.loop == {}
    assert cls.__step_budget__ is None
    assert ReactLoop(max_steps=40).step_budget(context.loop) == 40


def test_a_loader_built_only_to_read_its_tasks_needs_no_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch source's setup context and an export's decontamination read tasks, never run them."""
    data = tmp_path / "browser" / "online_mind2web"
    data.mkdir(parents=True)
    _data(data, *_TASKS)
    monkeypatch.setenv("SPECTRAGENT_BENCHMARK_DATA", str(tmp_path))

    loader = OnlineMind2WebTaskLoader.for_reading("All")
    assert loader.step_budget is None and loader.task_ids() == ["m2w-1", "m2w-2"]
    assert loader.get_task_context("m2w-1").loop == {}
    assert OnlineMind2WebTaskLoader.for_reading().subset == "All"
    with pytest.raises(BenchmarkDataError, match="no subset 'Easy'"):
        OnlineMind2WebTaskLoader.for_reading("Easy")
