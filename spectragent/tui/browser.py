"""Browse committed trace snapshots through the shared inspection repository."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

if TYPE_CHECKING:
    from spectragent.inspection.repository import RunRepository


def _terminal_value(value: Any) -> Any:
    """Keep image payloads out of terminal text while preserving all other details."""
    if isinstance(value, str) and value.startswith("data:image/"):
        return f"[inline image, {len(value):,} encoded characters; view in dashboard]"
    if isinstance(value, dict):
        return {key: _terminal_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_terminal_value(item) for item in value]
    return value


class BrowserApp(App[None]):
    """A keyboard-friendly experiment index and step inspector; never executes a trace."""

    TITLE = "All4One · Recorded experiments"
    CSS = """
    #search { dock: top; }
    #runs { height: 2fr; min-height: 6; }
    #detail { height: 3fr; }
    #steps { width: 2fr; }
    #content { width: 3fr; border-left: solid $primary; }
    #status { height: auto; max-height: 3; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("q", "quit", "Close"),
        Binding("ctrl+c", "quit", "Close", priority=True),
        Binding("r", "refresh_runs", "Refresh"),
        Binding("slash", "search", "Search"),
    ]

    def __init__(self, repository: RunRepository) -> None:
        super().__init__()
        self.repository = repository
        self._rows: dict[str, dict[str, Any]] = {}
        self._selected: str | None = None
        self._query: str | None = None
        self._list_generation = 0
        self._detail_generation = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Input(placeholder="Filter task, model, agent or run", id="search")
            yield DataTable(id="runs", cursor_type="row")
            yield Static(
                "Select a run, then a step. Reading recorded evidence never replays actions.",
                id="status",
                markup=False,
            )
            with Horizontal(id="detail"):
                yield DataTable(id="steps", cursor_type="row")
                yield RichLog(id="content", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#runs", DataTable).add_columns(
            "Task", "Agent / model", "Agent ending", "Trace", "Policy steps", "Known tokens"
        )
        self.query_one("#steps", DataTable).add_columns("Step", "Origin", "Actions", "Error")
        self.action_refresh_runs()
        self.query_one("#runs", DataTable).focus()

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed) -> None:
        if event.value != self._query:
            self._request_runs(event.value, debounce=True)

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def _show(self, value: Any) -> None:
        content = self.query_one("#content", RichLog)
        content.clear()
        content.write(
            Text(json.dumps(_terminal_value(value), indent=2, ensure_ascii=False, default=str))
        )

    def action_refresh_runs(self) -> None:
        self._request_runs(self.query_one("#search", Input).value, debounce=False)

    def _request_runs(self, query: str, *, debounce: bool) -> None:
        self._query = query
        self._list_generation += 1
        self.query_one("#status", Static).update("Loading recorded runs…")
        self.run_worker(
            self._load_runs(query, self._list_generation, debounce=debounce),
            group="run-list",
            exclusive=True,
            exit_on_error=False,
        )

    async def _load_runs(self, query: str, generation: int, *, debounce: bool) -> None:
        if debounce:
            await asyncio.sleep(0.2)
        try:
            rows = await asyncio.to_thread(self.repository.list_runs, search=query)
        except Exception as exc:
            if self.is_mounted and generation == self._list_generation:
                self.query_one("#status", Static).update(f"Cannot read experiments: {exc}")
            return
        if not self.is_mounted or generation != self._list_generation:
            return
        table = self.query_one("#runs", DataTable)
        self._rows = {row["key"]: row for row in rows}
        table.clear()
        for row in rows:
            table.add_row(
                Text(str(row.get("task_id") or row.get("run_id") or row["key"])),
                Text(f"{row.get('agent_id') or '—'} / {row.get('model') or '—'}"),
                str(row.get("agent_done") or "—"),
                str(row.get("trace_status") or "unreadable"),
                str(row.get("policy_steps", "—")),
                str(row.get("tokens") if row.get("tokens") is not None else "unknown"),
                key=row["key"],
            )
        self.query_one("#status", Static).update(
            f"{len(rows)} recorded runs · writing means an open trace; process liveness is unknown."
        )
        if self._selected in self._rows:
            self._select_run(self._selected)
        elif self._selected is not None:
            self._selected = None
            self._detail_generation += 1
            self.workers.cancel_group(self, "run-detail")
            self.query_one("#steps", DataTable).clear()
            self.query_one("#content", RichLog).clear()

    @on(DataTable.RowSelected, "#runs")
    def run_selected(self, event: DataTable.RowSelected) -> None:
        self._select_run(str(event.row_key.value))

    def _select_run(self, key: str) -> None:
        self._selected = key
        self._detail_generation += 1
        self.query_one("#steps", DataTable).clear()
        self._show({"loading_run": key})
        self.run_worker(
            self._load_run(key, self._detail_generation),
            group="run-detail",
            exclusive=True,
            exit_on_error=False,
        )

    async def _load_run(self, key: str, generation: int) -> None:
        try:
            detail = await asyncio.to_thread(self.repository.run, key)
        except Exception as exc:
            if self._detail_is_current(key, generation):
                self._show({"error": str(exc)})
            return
        if not self._detail_is_current(key, generation):
            return
        table = self.query_one("#steps", DataTable)
        for item in detail.get("steps", []):
            record = item.get("record", item)
            table.add_row(
                str(record["step"]),
                str(record.get("origin", "policy")),
                Text(", ".join(action.get("name", "?") for action in record.get("actions", []))),
                Text(str(record.get("error") or "")),
                key=str(record["step"]),
            )
        self._show({key: value for key, value in detail.items() if key != "steps"})

    @on(DataTable.RowSelected, "#steps")
    def step_selected(self, event: DataTable.RowSelected) -> None:
        if self._selected is None:
            return
        self._detail_generation += 1
        index = int(str(event.row_key.value))
        self._show({"loading_step": index, "run": self._selected})
        self.run_worker(
            self._load_step(self._selected, index, self._detail_generation),
            group="run-detail",
            exclusive=True,
            exit_on_error=False,
        )

    def _detail_is_current(self, key: str, generation: int) -> bool:
        return self.is_mounted and generation == self._detail_generation and key == self._selected

    async def _load_step(self, key: str, index: int, generation: int) -> None:
        try:
            detail = await asyncio.to_thread(self.repository.step, key, index)
        except Exception as exc:
            if self._detail_is_current(key, generation):
                self._show({"error": str(exc)})
        else:
            if self._detail_is_current(key, generation):
                self._show(detail)
