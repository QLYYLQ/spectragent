"""LexBench start pages are completed by its loader, before a run records them (§4.10 "起始页").

Every LexBench split writes its start pages without a scheme
(``www.bilibili.com``); handed to the browser as they were, every task failed
at setup. The loader completes a bare host to ``https://…`` — in the loader,
not the browser, because the trace records the start page setup received and
the flywheel report and the export recognise a run's site by that URL's host,
which a bare host does not have. Text or several sites are left as written and
refused, by name, at ``prepare()`` (``test_browser_setup_check.py`` has the
browser's half). Data that already carries a scheme is not touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import pytest

from spectragent.core.benchmark_runner import BenchmarkRunner, PreflightError
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.cooperation import BrowserCooperationError
from spectragent.core.capabilities.browser.start_page import start_page_problem
from spectragent.core.data_loader.task.lexbench_browser import (
    LexBenchBrowserTaskLoader,
    complete_start_page,
)
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.sources import task_identity
from tests._helpers.archive_fakes import make_archive_run
from tests._helpers.assembly import make_env, make_run
from tests._helpers.browser_archive import OwnedBrowser

#: The seven L1 tasks whose start page is text or several sites (the 2026-10-14 screening).
L1_NOT_A_URL = {"5", "7", "216", "217", "218", "221", "223"}


@pytest.mark.parametrize(
    ("written", "completed"),
    [
        ("www.bilibili.com", "https://www.bilibili.com"),
        ("s.weibo.com/top/summary", "https://s.weibo.com/top/summary"),
        ("bj.58.com", "https://bj.58.com"),
        ("medicare.gov", "https://medicare.gov"),
        ("www.example.com:8080/a?b=c#d", "https://www.example.com:8080/a?b=c#d"),
        ("www.a.com/go?to=https://b.com", "https://www.a.com/go?to=https://b.com"),
    ],
)
def test_a_bare_host_becomes_one_https_url(written: str, completed: str) -> None:
    assert complete_start_page(written) == completed
    assert start_page_problem(completed) is None


@pytest.mark.parametrize(
    "written",
    [
        "https://www.bilibili.com",
        "http://www.example.com/x",
        "www.xiachufang.com或其他美食网站",
        "s.weibo.com/top/summary或其他热搜",
        "mail.163.com或mail.qq.com",
        "jd.com + tmall.com + suning.com",
        "top.baidu.com + s.weibo.com/top/summary",
        "localhost",
        "www.example.com.",
        "javascript:alert(1)",
    ],
)
def test_anything_else_is_left_as_written(written: str) -> None:
    assert complete_start_page(written) == written


def _write_split(root: Path, records: list[dict[str, Any]], *, split: str = "L1") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    name = f"{split.lower()}.jsonl"
    (root / "data_info.json").write_text(json.dumps({"split": {split: name}}))
    (root / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    return root


def _record(task_id: str, start_url: str, **extra: Any) -> dict[str, Any]:
    return {"task_id": task_id, "task_text": f"task {task_id}", "start_url": start_url, **extra}


@pytest.fixture
def lexbench(tmp_path: Path) -> LexBenchBrowserTaskLoader:
    """A LexBench L1 split written the way the shipped one is."""
    root = _write_split(
        tmp_path / "lexbench_browser",
        [
            _record("6", "www.bilibili.com"),
            _record("216", "s.weibo.com/top/summary"),
            _record("5", "www.xiachufang.com或其他美食网站"),
            _record("9", "jd.com + tmall.com + suning.com"),
            _record("42", "https://www.baidu.com"),
            {
                "task_id": "nested",
                "task_text": "nested",
                "setup": {"start_url": "www.ctrip.com", "login_required": False},
            },
        ],
    )
    return LexBenchBrowserTaskLoader(subset="L1", data_dir=str(root))


def _start_url(loader: LexBenchBrowserTaskLoader, task_id: str) -> Any:
    return loader.get_task_context(task_id).env.get("start_url")


class TestTheLoader:
    def test_a_bare_host_reaches_the_run_as_an_https_url(
        self, lexbench: LexBenchBrowserTaskLoader
    ) -> None:
        assert _start_url(lexbench, "6") == "https://www.bilibili.com"
        assert lexbench["6"].setup.start_url == "https://www.bilibili.com"  # type: ignore[union-attr]

    def test_a_host_with_a_path(self, lexbench: LexBenchBrowserTaskLoader) -> None:
        assert _start_url(lexbench, "216") == "https://s.weibo.com/top/summary"

    def test_a_nested_setup_s_start_page_too(self, lexbench: LexBenchBrowserTaskLoader) -> None:
        assert _start_url(lexbench, "nested") == "https://www.ctrip.com"

    def test_data_that_already_has_a_scheme_is_unchanged(
        self, lexbench: LexBenchBrowserTaskLoader
    ) -> None:
        assert _start_url(lexbench, "42") == "https://www.baidu.com"

    def test_text_or_several_sites_are_left_for_the_setup_check_to_refuse(
        self, lexbench: LexBenchBrowserTaskLoader
    ) -> None:
        for task_id, written in (
            ("5", "www.xiachufang.com或其他美食网站"),
            ("9", "jd.com + tmall.com + suning.com"),
        ):
            assert _start_url(lexbench, task_id) == written
            problem = start_page_problem(written)
            assert problem is not None and problem.startswith(f"start_url {written!r}")

    def test_the_upstream_record_shape_is_completed_too(self, tmp_path: Path) -> None:
        root = _write_split(
            tmp_path / "private",
            [
                {
                    "id": 1,
                    "query": "去京东，搜索iPhone 17。",
                    "target_website": "www.jd.com",
                    "login_required": True,
                }
            ],
            split="private",
        )
        loader = LexBenchBrowserTaskLoader(subset="private", data_dir=str(root))
        assert _start_url(loader, "1") == "https://www.jd.com"


@pytest.mark.requires_benchmark_data("browser/lexbench_browser")
def test_every_shipped_l1_start_page_opens_or_is_refused_by_name() -> None:
    loader = LexBenchBrowserTaskLoader(subset="L1")
    refused = set()
    for task_id in loader.task_ids():
        start_url = _start_url(loader, task_id)
        if start_page_problem(start_url) is None:
            assert urlsplit(start_url).scheme == "https" and urlsplit(start_url).hostname
        else:
            refused.add(task_id)
    assert refused == L1_NOT_A_URL
    assert len(loader) - len(refused) == 176


async def test_the_trace_records_the_completed_start_page_and_its_site(
    lexbench: LexBenchBrowserTaskLoader, tmp_path: Path
) -> None:
    """The run's recorded start page has a host, so the site key the report and export use works."""
    traces = tmp_path / "traces"

    def compose() -> Any:
        return make_archive_run(hooks=[TraceWriter(trace_dir=str(traces))])

    summary = await BenchmarkRunner(loader=lexbench, compose=compose, timeout=30).run(
        task_ids=["6"]
    )
    (outcome,) = summary.outcomes
    assert outcome.trace_dir is not None
    config = RunTrace(outcome.trace_dir).config
    assert config.task_setup is not None
    assert config.task_setup.context["start_url"] == "https://www.bilibili.com"
    assert task_identity(config).site == "www.bilibili.com"


class _BrowserAgent:
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["screenshot"]),
        "action": ("action", ["done"]),
    }

    def build_react_prompt(self, obs: Any) -> list[dict[str, str]]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Any]:
        return []


class _LLM:
    model = "start-page-llm"


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

    def clear(self) -> None:
        return None


async def test_a_sweep_starting_on_text_stops_at_its_pre_flight(
    lexbench: LexBenchBrowserTaskLoader,
) -> None:
    """``spectragent-bench``'s pre-flight is ``prepare()``: the browser's check names the start page."""
    backend = OwnedBrowser()

    def compose() -> Any:
        cap = BrowserCapability()
        cap.backend = backend  # type: ignore[assignment]
        return make_run(
            agent=_BrowserAgent(),
            loop=ReactLoop(max_steps=1),
            env=make_env(BrowserEnvironment, {"browser": cap}),
            llm=_LLM(),
            memory=_Memory(),
        )

    runner = BenchmarkRunner(loader=lexbench, compose=compose, timeout=30)
    with pytest.raises(PreflightError) as info:
        await runner.run(task_ids=["5", "6"])
    assert info.value.task_id == "5"
    assert isinstance(info.value.error, BrowserCooperationError)
    assert "start_url 'www.xiachufang.com或其他美食网站' ↔ browser setup" in str(info.value)
    assert backend.open_calls == [], "nothing was launched"
