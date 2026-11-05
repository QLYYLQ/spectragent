"""Each browser benchmark supplies the step budget its public protocol states, and only that.

``docs/architecture_design.md`` §4.10.1 "基准的步数预算": Online-Mind2Web's
authors gave each open-source agent 25 steps (its paper, Appendix E), which its
task loader hands the Loop as the Loop input ``max_steps``; BrowseComp and
LexBench-Browser state none, so their tasks hand none and the configured
``max_steps`` applies. The configured one stays the ceiling either way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectragent.core.data_loader.task import (
    BrowseCompTaskLoader,
    LexBenchBrowserTaskLoader,
    OnlineMind2WebTaskLoader,
)
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.core.loops import ReactLoop
from spectragent.schemas.task import TaskContext


def _loader(tmp_path: Path, cls: type[TaskLoaderBase], *records: dict[str, str]) -> TaskLoaderBase:
    """*cls* over a data directory of its own holding *records* as its ``All`` subset."""
    (tmp_path / "data_info.json").write_text(json.dumps({"split": {"All": "All.json"}}))
    (tmp_path / "All.json").write_text(json.dumps(list(records)))
    return cls(data_dir=str(tmp_path))


def _online_mind2web(tmp_path: Path) -> TaskLoaderBase:
    return _loader(
        tmp_path,
        OnlineMind2WebTaskLoader,
        {"task_id": "m2w-1", "task_text": "Find a flight", "start_url": "https://example.com"},
        {"task_id": "m2w-2", "task_text": "Find a hotel", "start_url": "https://example.org"},
    )


def test_each_online_mind2web_task_brings_the_25_steps_its_authors_gave(tmp_path: Path) -> None:
    loader = _online_mind2web(tmp_path)

    for task_id in loader.task_ids():
        context = loader.get_task_context(task_id)
        assert context.loop == {"max_steps": 25}
        assert context.env["start_url"].startswith("https://")
        ReactLoop().check_loop_inputs(context.loop)
    budget = OnlineMind2WebTaskLoader.__step_budget__
    assert budget is not None and budget.steps == "25 steps"
    assert "Appendix E" in budget.source and "arxiv.org/abs/2504.01382v4" in budget.source


@pytest.mark.parametrize(("ceiling", "steps"), [(500, 25), (25, 25), (10, 10)])
def test_the_configured_max_steps_stays_the_ceiling(
    tmp_path: Path, ceiling: int, steps: int
) -> None:
    """The task's 25 lower a higher ceiling and never raise a lower one."""
    context = _online_mind2web(tmp_path).get_task_context("m2w-1")
    assert ReactLoop(max_steps=ceiling).step_budget(context.loop) == steps


def test_an_unknown_task_brings_no_budget(tmp_path: Path) -> None:
    assert _online_mind2web(tmp_path).get_task_context("no-such-task") == TaskContext()


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
    tmp_path: Path, cls: type[TaskLoaderBase], record: dict[str, str]
) -> None:
    """BrowseComp and LexBench-Browser: the configured ``max_steps`` applies to every task."""
    loader = _loader(tmp_path, cls, record)

    context = loader.get_task_context(record["task_id"])
    assert context.loop == {}
    assert cls.__step_budget__ is None
    assert ReactLoop(max_steps=40).step_budget(context.loop) == 40
