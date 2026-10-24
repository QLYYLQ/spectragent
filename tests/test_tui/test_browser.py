"""Keyboard browsing reads the same committed records as the dashboard."""

import asyncio
import threading
from typing import cast

import pytest
from textual.widgets import DataTable, Input, Static, TextArea

from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.inspection.repository import RunRepository, open_repository
from spectragent.schemas.restoration import BranchProvenance
from spectragent.schemas.step_record import StepRecord
from spectragent.tui import browse
from spectragent.tui.browser import BrowserApp
from spectragent.tui.presentation import terminal_value
from spectragent.tui.widgets import FactSheet
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
        rendered = app.query_one("#content", TextArea).text
        assert '"record"' in rendered
        assert '"calls"' in rendered
        app.query_one("#search", Input).value = "does not exist"
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert runs.row_count == 0


async def test_details_can_be_selected_and_copied_without_closing(tmp_path):
    build_reference_trace(tmp_path)
    app = BrowserApp(open_repository(tmp_path), initial_run="run-test")
    async with app.run_test(size=(110, 40)) as pilot:
        await _until(lambda: app._run_detail is not None)
        content = app.query_one("#content", TextArea)
        content.focus()
        await pilot.press("f7", "ctrl+c")
        assert content.selected_text == content.text
        assert app.clipboard == content.text
        assert app.is_running
        assert '"summary"' in app.clipboard
        before = content.text
        await pilot.press("x", "backspace")
        assert content.text == before
        app.query_one("#runs", DataTable).focus()
        await pilot.press("f8")
        assert app.clipboard == before


async def test_search_selection_ctrl_c_copies_instead_of_closing(tmp_path):
    app = BrowserApp(open_repository(tmp_path))
    async with app.run_test() as pilot:
        search = app.query_one("#search", Input)
        search.value = "history"
        search.focus()
        await pilot.press("ctrl+shift+a", "ctrl+c")
        assert app.clipboard == "history"
        assert app.is_running


async def test_ctrl_c_from_run_table_still_closes_history_view(tmp_path):
    app = BrowserApp(open_repository(tmp_path))
    async with app.run_test() as pilot:
        app.query_one("#runs", DataTable).focus()
        await pilot.press("ctrl+c")
        assert not app.is_running


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
            await _until(lambda: '"run_id": "fast"' in app.query_one("#content", TextArea).text)
            await pilot.pause()
            assert not repository.detail_finished.is_set()
            repository.detail_released.set()
            await _until(repository.detail_finished.is_set)
            await pilot.pause()
            text = app.query_one("#content", TextArea).text
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
                app._selected == "source" and '"record"' in app.query_one("#content", TextArea).text
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
        text = app.query_one("#content", TextArea).text
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
        text = app.query_one("#content", TextArea).text
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
        titles = [str(column.label) for column in steps.ordered_columns]
        actions = str(steps.get_row_at(0)[titles.index("Actions")])
        assert "actual: browser.click (failed)" in actions
        assert "source: browser.click" in actions
        steps.focus()
        await pilot.press("enter")
        await _until(lambda: '"actual_action_results"' in app.query_one("#content", TextArea).text)
        text = app.query_one("#content", TextArea).text
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


async def test_enter_opens_a_run_moves_into_steps_and_escape_walks_back(tmp_path):
    build_reference_trace(tmp_path)
    app = BrowserApp(open_repository(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("enter")
        await _until(lambda: app._run_detail is not None)
        await pilot.pause()
        steps = app.query_one("#steps", DataTable)
        content = app.query_one("#content", TextArea)
        assert app.focused is steps
        assert '"summary"' in content.text
        # Moving the cursor shows the step; Enter is not needed.
        await pilot.press("down")
        await _until(lambda: '"record"' in content.text)
        assert app._selected_step == 1
        assert "Step 1" in app.query_one("#facts", FactSheet).plain_text
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused is content
        await pilot.press("escape")
        assert app.focused is steps
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is app.query_one("#runs", DataTable)
        assert '"summary"' in content.text


async def test_bracket_keys_step_through_a_run_from_its_details(tmp_path):
    build_reference_trace(tmp_path)
    app = BrowserApp(open_repository(tmp_path), initial_run="run-test")
    async with app.run_test(size=(120, 40)) as pilot:
        await _until(lambda: app._run_detail is not None)
        await pilot.press("3")
        await pilot.pause()
        content = app.query_one("#content", TextArea)
        assert app.focused is content
        await pilot.press("right_square_bracket")
        await _until(lambda: app._selected_step == 1 and '"record"' in content.text)
        assert app.focused is content
        await pilot.press("left_square_bracket")
        await _until(lambda: app._selected_step == 0 and '"record"' in content.text)
        assert app.query_one("#steps", DataTable).cursor_coordinate.row == 0


async def test_number_keys_zoom_and_key_help(tmp_path):
    build_reference_trace(tmp_path)
    app = BrowserApp(open_repository(tmp_path), initial_run="run-test")
    async with app.run_test(size=(120, 40)) as pilot:
        await _until(lambda: app._run_detail is not None)
        await pilot.press("1")
        await pilot.pause()
        assert app.focused is app.query_one("#runs", DataTable)
        await pilot.press("2", "z")
        await pilot.pause()
        assert app.screen.maximized is app.query_one("#steps-panel")
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.maximized is None
        assert app.focused is app.query_one("#steps", DataTable)
        await pilot.press("question_mark")
        await pilot.pause()
        assert app.screen.query("HelpPanel")
        await pilot.press("question_mark")
        await pilot.pause()
        assert not app.screen.query("HelpPanel")


async def test_unreadable_runs_stay_listed_with_their_reason(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.json").write_text("not json")
    build_reference_trace(tmp_path)
    app = BrowserApp(open_repository(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _until(lambda: len(app._rows) == 2)
        await pilot.pause()
        runs = app.query_one("#runs", DataTable)
        tasks = [str(runs.get_row_at(index)[0]) for index in range(runs.row_count)]
        # The reason stays in the first column, which no width drops.
        assert any("broken" in task and "does not parse" in task for task in tasks)
        assert "1 unreadable" in str(app.query_one("#runs-panel").border_subtitle)


async def test_source_navigation_marks_the_checkpoint_it_came_from(tmp_path):
    build_reference_trace(tmp_path, "source")
    _branch_trace(tmp_path)
    app = BrowserApp(open_repository(tmp_path), initial_run="branch")
    async with app.run_test(size=(120, 40)) as pilot:
        await _until(
            lambda: (
                app._run_detail is not None and app._selected == "branch" and len(app._rows) == 2
            )
        )
        subtitle = str(app.query_one("#steps-panel").border_subtitle)
        assert "Agent history: unknown" in subtitle and "checkpoint ◆ 1" in subtitle
        await pilot.press("s")
        await _until(lambda: app._selected == "source" and app._run_detail is not None)
        await pilot.pause()
        assert "source of branch" in str(app.query_one("#steps-panel").border_subtitle)
        steps = app.query_one("#steps", DataTable)
        assert "◆" in str(steps.get_row_at(1)[1])
        assert "◆" not in str(steps.get_row_at(0)[1])


async def test_escape_to_the_overview_wins_over_a_pending_step_load(tmp_path):
    build_reference_trace(tmp_path)
    app = BrowserApp(open_repository(tmp_path), initial_run="run-test")
    async with app.run_test(size=(120, 40)) as pilot:
        await _until(lambda: app._run_detail is not None and app._rows)
        await pilot.press("2", "down", "escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._selected_step is None
        assert '"summary"' in app.query_one("#content", TextArea).text
        assert "run overview" in str(app.query_one("#detail-panel").border_subtitle)
