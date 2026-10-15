"""Keyboard browsing reads the same committed records as the dashboard."""

import asyncio
import threading
from typing import cast

from textual.widgets import DataTable, Input, RichLog, Static

from spectragent.inspection.repository import RunRepository, open_repository
from spectragent.tui.browser import BrowserApp, _terminal_value
from tests._helpers.trace_builder import build_reference_trace


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
    rendered = _terminal_value(original)
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
