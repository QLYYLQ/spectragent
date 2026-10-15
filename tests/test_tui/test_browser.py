"""Keyboard browsing reads the same committed records as the dashboard."""

import asyncio
import threading
from typing import cast

import pytest
from textual.widgets import DataTable, Input, RichLog, Static

from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection.repository import RunRepository, open_repository
from spectragent.schemas.restoration import BranchProvenance
from spectragent.schemas.step_record import StepRecord
from spectragent.tui import browse
from spectragent.tui.browser import BrowserApp
from spectragent.tui.presentation import terminal_value
from tests._helpers.trace_builder import agent_result, build_reference_trace


async def test_select_run_then_step_and_search(tmp_path):
    build_reference_trace(tmp_path)
    app = BrowserApp(open_repository(tmp_path))
    async with app.run_test(size=(110, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        runs = app.query_one("#runs", DataTable)
        assert runs.row_count == 1
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        steps = app.query_one("#steps", DataTable)
        assert steps.row_count == 2
        steps.focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        rendered = "\n".join(line.text for line in app.query_one("#content", RichLog).lines)
        assert '"record"' in rendered
        assert '"calls"' in rendered
        app.query_one("#search", Input).value = "does not exist"
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert runs.row_count == 0


def test_terminal_does_not_expand_inline_images_into_megabytes():
    image = "data:image/png;base64," + "x" * 10000
    original = {"messages": [{"text": "prompt", "url": image}]}
    rendered = terminal_value(original)
    assert rendered["messages"][0]["text"] == "prompt"
    assert len(rendered["messages"][0]["url"]) < 100
    assert original["messages"][0]["url"] == image


async def _until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


class DelayedRepository:
    def __init__(self):
        self.search_started = threading.Event()
        self.search_released = threading.Event()
        self.search_finished = threading.Event()
        self.detail_started = threading.Event()
        self.detail_released = threading.Event()
        self.detail_finished = threading.Event()
        self.queries = []

    def list_runs(self, search=""):
        self.queries.append(search)
        if search == "slow":
            self.search_started.set()
            self.search_released.wait(5)
            self.search_finished.set()
            # An obsolete failure must not replace the newest query's status.
            raise OSError("obsolete search failed")
        return [{"key": search or "initial", "task_id": search or "initial"}]

    def run(self, key):
        if key == "slow":
            self.detail_started.set()
            self.detail_released.wait(5)
            self.detail_finished.set()
        return {"summary": {"run_id": key}, "steps": []}

    def step(self, key, index):
        return {"record": {"step": index}, "run_id": key}


async def test_slow_search_leaves_keyboard_responsive_and_latest_query_wins():
    repository = DelayedRepository()
    app = BrowserApp(cast(RunRepository, repository))
    try:
        async with app.run_test(size=(110, 40)) as pilot:
            await app.workers.wait_for_complete()
            search = app.query_one("#search", Input)
            search.value = "slow"
            await _until(repository.search_started.is_set)
            # This key must be handled while the disk query is still blocked.
            await pilot.press("slash")
            assert app.focused is search
            search.value = "fast"
            await _until(lambda: "fast" in app._rows)
            assert not repository.search_finished.is_set()
            repository.search_released.set()
            await _until(repository.search_finished.is_set)
            await pilot.pause()
            assert list(app._rows) == ["fast"]
            assert "obsolete" not in str(app.query_one("#status", Static).render())
    finally:
        repository.search_released.set()


async def test_search_is_debounced_and_old_detail_cannot_replace_new_selection():
    repository = DelayedRepository()
    app = BrowserApp(cast(RunRepository, repository))
    try:
        async with app.run_test(size=(110, 40)) as pilot:
            await app.workers.wait_for_complete()
            search = app.query_one("#search", Input)
            for query in ("f", "fa", "fas", "fast"):
                search.value = query
                await asyncio.sleep(0.02)
            await _until(lambda: "fast" in app._rows)
            assert repository.queries == ["", "fast"]
            app._select_run("slow")
            await _until(repository.detail_started.is_set)
            app._select_run("fast")
            await _until(
                lambda: (
                    '"run_id": "fast"'
                    in "\n".join(line.text for line in app.query_one("#content", RichLog).lines)
                )
            )
            await pilot.pause()
            assert not repository.detail_finished.is_set()
            repository.detail_released.set()
            await _until(repository.detail_finished.is_set)
            await pilot.pause()
            text = "\n".join(line.text for line in app.query_one("#content", RichLog).lines)
            assert '"run_id": "fast"' in text
            assert '"run_id": "slow"' not in text
    finally:
        repository.detail_released.set()


def _branch_trace(root, restored=None):
    writer = WALWriter(root, "branch")
    writer.init_run(
        TraceConfig(
            branch=BranchProvenance(
                source_run_id="source",
                source_trace="source",
                checkpoint_step=1,
                branch_index=0,
                branch_group_id="source/s1",
            )
        )
    )
    writer.commit_step(0, restored or StepRecord(step=0, origin="restored"))
    writer.commit_step(1, StepRecord(step=1, origin="policy"))
    writer.write_result(agent_result())
    writer.finalize(TraceStatus.COMPLETE)
    return writer.run_dir


@pytest.mark.parametrize("filtered", [False, True])
async def test_initial_run_and_source_checkpoint_keep_the_selected_root(tmp_path, filtered):
    build_reference_trace(tmp_path, "source")
    branch = _branch_trace(tmp_path)
    repository = open_repository(
        tmp_path, config_hash=RunTrace(branch).manifest.config_hash if filtered else None
    )
    app = BrowserApp(repository, initial_run="branch")
    async with app.run_test(size=(110, 40)) as pilot:
        await _until(
            lambda: (
                app._run_detail is not None
                and app._selected == "branch"
                and len(app._rows) == (1 if filtered else 2)
            )
        )
        assert app.query_one("#runs", DataTable).row_count == (1 if filtered else 2)
        assert app._run_detail is not None and app._run_detail["source_key"] == "source"
        await pilot.press("s")
        await _until(
            lambda: (
                app._selected == "source"
                and '"record"'
                in "\n".join(line.text for line in app.query_one("#content", RichLog).lines)
            )
        )
        await pilot.pause()
        assert app.repository is repository
        assert app._selected_step == 1
        assert app.query_one("#steps", DataTable).cursor_coordinate.row == 1
        assert app.query_one("#runs", DataTable).row_count == (1 if filtered else 2)
        # Refreshing the filtered list must not eject an explicitly opened
        # source that belongs to the root but not to this configuration.
        generation = app._detail_generation
        app.action_refresh_runs()
        await _until(
            lambda: (
                app._detail_generation > generation
                and app._run_detail is not None
                and app._selected == "source"
            )
        )
        assert app._selected_step == 1


async def test_source_unavailable_under_single_run_root_has_an_explicit_reason(tmp_path):
    build_reference_trace(tmp_path, "source")
    directory = _branch_trace(tmp_path)
    app = BrowserApp(open_repository(directory), initial_run=".")
    async with app.run_test(size=(110, 40)) as pilot:
        await _until(lambda: app._run_detail is not None and app._selected == ".")
        await pilot.press("s")
        await pilot.pause()
        text = "\n".join(line.text for line in app.query_one("#content", RichLog).lines)
        assert "unavailable under this root" in " ".join(text.split())
        assert app._selected == "."


@pytest.mark.parametrize(
    "resolution,reason",
    [
        ("outside_root", "outside this root"),
        ("ambiguous", "Multiple runs"),
        ("missing", "unavailable under this root"),
        ("none", "no recorded source relationship"),
    ],
)
async def test_unresolved_source_navigation_explains_the_recorded_resolution(resolution, reason):
    class Repository(DelayedRepository):
        def run(self, key):
            return {
                "summary": {},
                "steps": [],
                "execution": {
                    "source_resolution": resolution,
                    "source_key": None,
                    "source_run_id": "recording",
                    "checkpoint": None,
                },
            }

    app = BrowserApp(cast(RunRepository, Repository()), initial_run="initial")
    async with app.run_test(size=(110, 40)) as pilot:
        await _until(lambda: app._run_detail is not None)
        await pilot.press("s")
        await pilot.pause()
        text = "\n".join(line.text for line in app.query_one("#content", RichLog).lines)
        assert reason in " ".join(text.split())
        assert app._selected == "initial"


async def test_history_restoration_separates_source_success_from_actual_failure(tmp_path):
    record = StepRecord.model_validate(
        {
            "step": 0,
            "origin": "restored",
            "actions": [{"name": "browser.click", "params": {"index": 9}}],
            "action_results": [{"success": False, "error": "current restore failed"}],
            "source_history": {
                "source_run_id": "source",
                "source_step": 0,
                "source_reply": "recorded source reply",
                "source_actions": [{"name": "browser.click", "params": {"index": 1}}],
            },
            "restoration": {
                "step": 0,
                "verdict": "warn",
                "reason": "action_failed",
                "reasons": ["action_failed"],
                "actions": [
                    {
                        "recorded": {"name": "browser.click", "params": {"index": 1}},
                        "executed": {"name": "browser.click", "params": {"index": 9}},
                        "method": "geometry_signature",
                        "recorded_success": True,
                        "live_success": False,
                        "result": {"success": False, "error": "current restore failed"},
                    }
                ],
            },
        }
    )
    _branch_trace(tmp_path, restored=record)
    repository = open_repository(tmp_path)
    app = BrowserApp(repository, initial_run="branch")
    async with app.run_test(size=(110, 40)) as pilot:
        await _until(lambda: app._run_detail is not None)
        steps = app.query_one("#steps", DataTable)
        assert "source: browser.click; actual: browser.click (failed)" in str(
            steps.get_row_at(0)[2]
        )
        steps.focus()
        await pilot.press("enter")
        await _until(
            lambda: (
                '"actual_action_results"'
                in "\n".join(line.text for line in app.query_one("#content", RichLog).lines)
            )
        )
        text = "\n".join(line.text for line in app.query_one("#content", RichLog).lines)
        assert '"actual_restoration_actions"' in text
        assert '"live_success": false' in text
        assert '"source_reply": "recorded source reply"' in text
        assert repository.step("branch", 0)["record"]["action_results"] == [
            {
                "success": False,
                "error": "current restore failed",
                "is_done": False,
                "extracted_content": None,
                "typed_into_sensitive_field": None,
            }
        ]


async def test_plain_replay_source_navigation_needs_no_branch_provenance(tmp_path):
    source = build_reference_trace(tmp_path, "source")
    writer = WALWriter(tmp_path, "replay")
    writer.init_run(
        TraceConfig.model_validate(
            {
                "task_setup": {
                    "context": {"replay_archive": str(source.run_dir.resolve())},
                    "redactions": {},
                }
            }
        )
    )
    writer.write_result(agent_result())
    writer.finalize(TraceStatus.COMPLETE)
    app = BrowserApp(open_repository(tmp_path), initial_run="replay")
    async with app.run_test(size=(110, 40)) as pilot:
        await _until(lambda: app._run_detail is not None)
        assert app._run_detail is not None and app._run_detail["summary"]["branch"] is None
        await pilot.press("s")
        await _until(lambda: app._selected == "source" and app._run_detail is not None)
        assert app._selected_step is None
        assert app.query_one("#runs", DataTable).row_count == 2


def test_browse_initial_focus_must_match_config_filter(tmp_path, monkeypatch):
    build_reference_trace(tmp_path, "source")
    branch = _branch_trace(tmp_path)
    monkeypatch.setattr(
        BrowserApp, "run", lambda self: pytest.fail("mismatched selection opened a terminal")
    )
    with pytest.raises(ValueError, match="does not match"):
        browse(tmp_path, initial_run="source", config_hash=RunTrace(branch).manifest.config_hash)
