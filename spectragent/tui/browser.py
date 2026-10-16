"""Browse committed trace snapshots through the shared inspection repository."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, Static

from spectragent.tui.presentation import gap_text, step_actions, step_presentation, terminal_value
from spectragent.tui.text import EvidenceText

if TYPE_CHECKING:
    from spectragent.inspection.repository import RunRepository


class BrowserApp(App[None]):
    """A keyboard-friendly experiment index and step inspector; never executes a trace."""

    TITLE = "All4One · Recorded experiments"
    CSS = """
    #search { dock: top; }
    #runs { height: 2fr; min-height: 6; }
    #detail { height: 3fr; }
    #steps { width: 1fr; }
    #content { width: 1fr; border-left: solid $primary; }
    #status { height: auto; max-height: 3; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("q", "quit", "Close"),
        Binding("ctrl+c", "copy_or_quit", "Copy / close", priority=True),
        Binding("f8", "copy_details", "Copy details", priority=True),
        Binding("r", "refresh_runs", "Refresh"),
        Binding("slash", "search", "Search"),
        Binding("s", "source", "Source checkpoint"),
    ]

    def __init__(self, repository: RunRepository, initial_run: str | None = None) -> None:
        super().__init__()
        self.repository = repository
        self._rows: dict[str, dict[str, Any]] = {}
        self._selected: str | None = None
        self._initial_run = initial_run
        self._selected_step: int | None = None
        self._source_selection = False
        self._run_detail: dict[str, Any] | None = None
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
                yield EvidenceText(id="content")
        yield Footer()

    def on_mount(self) -> None:
        for title, width in (
            ("Task", 24),
            ("Execution / history", 27),
            ("Agent ending", 12),
            ("Trace", 12),
            ("Policy steps", 12),
            ("Known tokens", 12),
            ("Agent / model", 28),
            ("Config source", 36),
        ):
            self.query_one("#runs", DataTable).add_column(title, width=width)
        for title, width in (
            ("Step", 4),
            ("Origin", 9),
            ("Gap / verification", 25),
            ("Actions / restoration", 48),
            ("Error", 36),
        ):
            self.query_one("#steps", DataTable).add_column(title, width=width)
        self.action_refresh_runs()
        self.query_one("#runs", DataTable).focus()
        if self._initial_run is not None:
            self._select_run(self._initial_run)

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed) -> None:
        if event.value != self._query:
            self._request_runs(event.value, debounce=True)

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def _show(self, value: Any) -> None:
        content = self.query_one("#content", EvidenceText)
        content.load_text(
            json.dumps(terminal_value(value), indent=2, ensure_ascii=False, default=str)
        )

    async def action_copy_details(self) -> None:
        await self.query_one("#content", EvidenceText).action_copy()

    async def action_copy_or_quit(self) -> None:
        if isinstance(self.focused, EvidenceText):
            await self.focused.action_copy()
        elif isinstance(self.focused, Input) and self.focused.selected_text:
            self.copy_to_clipboard(self.focused.selected_text)
        else:
            await self.action_quit()

    def action_refresh_runs(self) -> None:
        self._request_runs(self.query_one("#search", Input).value, debounce=False)

    def _request_runs(self, query: str, *, debounce: bool) -> None:
        self._query = query
        self._list_generation += 1
        self.query_one("#status", Static).update("Loading recorded runs…")
        self.run_worker(
            partial(self._load_runs, query, self._list_generation, debounce=debounce),
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
                Text(
                    f"{row.get('execution_kind', 'unknown')}"
                    + (
                        f" · {(row.get('strategy') or {}).get('history_source') or '?'} history"
                        if row.get("branch")
                        else ""
                    )
                ),
                str(row.get("agent_done") or "—"),
                str(row.get("trace_status") or "unreadable"),
                str(row.get("policy_steps", "—")),
                str(row.get("tokens") if row.get("tokens") is not None else "unknown"),
                Text(f"{row.get('agent_id') or '—'} / {row.get('model') or '—'}"),
                Text(str((row.get("config_source") or {}).get("path") or "Unknown")),
                key=row["key"],
            )
        self.query_one("#status", Static).update(
            f"{len(rows)} recorded runs · writing means an open trace; process liveness is unknown."
        )
        initial_run, self._initial_run = self._initial_run, None
        if self._selected in self._rows:
            table.move_cursor(row=list(self._rows).index(self._selected))
            self._select_run(
                self._selected, step=self._selected_step, source_navigation=self._source_selection
            )
        elif self._selected is not None:
            if self._selected == initial_run and not query:
                # Keep the explicit run's own read error visible, even if it
                # could not appear in directory discovery.
                return
            if self._source_selection:
                self._select_run(self._selected, step=self._selected_step, source_navigation=True)
                return
            self._selected = None
            self._run_detail = None
            self._detail_generation += 1
            self.workers.cancel_group(self, "run-detail")
            self.query_one("#steps", DataTable).clear()
            self.query_one("#content", EvidenceText).load_text("")

    @on(DataTable.RowSelected, "#runs")
    def run_selected(self, event: DataTable.RowSelected) -> None:
        self._select_run(str(event.row_key.value))

    def _select_run(
        self, key: str, *, step: int | None = None, source_navigation: bool = False
    ) -> None:
        self._selected = key
        self._source_selection = source_navigation
        self._selected_step = step
        self._run_detail = None
        self._detail_generation += 1
        self.query_one("#steps", DataTable).clear()
        self._show({"loading_run": key})
        self.run_worker(
            partial(self._load_run, key, self._detail_generation),
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
        self._run_detail = detail
        runs = self.query_one("#runs", DataTable)
        runs.show_cursor = key in self._rows
        if key in self._rows:
            runs.move_cursor(row=list(self._rows).index(key))
        config_source = (detail.get("summary", {}).get("config_source") or {}).get(
            "path"
        ) or "Unknown"
        scope = (
            " · source outside the current list filter"
            if self._source_selection and key not in self._rows
            else ""
        )
        execution = detail.get("execution") or {}
        strategy = (
            f" · Agent history: {execution.get('history_source') or 'unknown'}"
            f" · verify: {execution.get('verify') or 'unknown'}"
            f" · checkpoint {execution.get('checkpoint')} (*)"
            if execution.get("checkpoint") is not None
            else ""
        )
        self.query_one("#status", Static).update(
            f"Run {key}{strategy}{scope} · config source: {config_source}"
        )
        table = self.query_one("#steps", DataTable)
        for item in detail.get("steps", []):
            record = item.get("record", item)
            table.add_row(
                str(record["step"])
                + ("*" if record["step"] == execution.get("checkpoint") else ""),
                str(record.get("origin", "policy")),
                Text(gap_text(record.get("gap"))),
                Text(step_actions(record)),
                Text(str(record.get("error") or "")),
                key=str(record["step"]),
            )
        self._show({key: value for key, value in detail.items() if key != "steps"})
        if self._selected_step is not None:
            step = self._selected_step
            indices = [item.get("record", item)["step"] for item in detail.get("steps", [])]
            if step in indices:
                table.move_cursor(row=indices.index(step))
                self.call_after_refresh(table.focus)
            await self._load_step(key, step, generation)

    @on(DataTable.RowSelected, "#steps")
    def step_selected(self, event: DataTable.RowSelected) -> None:
        if self._selected is None:
            return
        self._detail_generation += 1
        index = int(str(event.row_key.value))
        self._selected_step = index
        self._show({"loading_step": index, "run": self._selected})
        self.run_worker(
            partial(self._load_step, self._selected, index, self._detail_generation),
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
                self._show(
                    {
                        "agent_history_vs_replay": detail.get("gap"),
                        "history_comparison": detail.get("comparison"),
                        **{
                            name: value
                            for name, value in detail.items()
                            if name not in {"gap", "comparison"}
                        },
                        "record": step_presentation(detail["record"]),
                    }
                )

    def action_source(self) -> None:
        detail = self._run_detail
        if detail is None:
            self._show({"source_navigation": "Select a run and wait for its details to load."})
            return
        execution = detail.get("execution") or {}
        branch = detail.get("summary", {}).get("branch") or {}
        source = execution.get("source_key") or detail.get("source_key")
        checkpoint = execution.get("checkpoint", branch.get("checkpoint_step"))
        resolution = execution.get("source_resolution", "missing" if branch else "none")
        if source is None:
            reasons = {
                "none": "The selected run has no recorded source relationship.",
                "missing": "The recorded source run is unavailable under this root.",
                "ambiguous": "Multiple runs under this root share the recorded source id; no source was chosen.",
                "outside_root": "The recorded source is outside this root; choose a root containing both runs to navigate.",
            }
            self._show(
                {
                    "source_run_id": execution.get("source_run_id", branch.get("source_run_id")),
                    "checkpoint_step": checkpoint,
                    "source_navigation": reasons.get(
                        resolution, "The recorded source could not be resolved under this root."
                    ),
                }
            )
            return
        if self.query_one("#search", Input).value:
            self.query_one("#search", Input).value = ""
            self._request_runs("", debounce=False)
        self._select_run(source, step=checkpoint, source_navigation=True)
