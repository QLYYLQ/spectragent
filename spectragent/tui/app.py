"""Live terminal display with one explicitly owned operation task."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Print
from textual.widgets import DataTable, Footer, Static

from spectragent.monitoring.state import ProgressState
from spectragent.tui.presentation import (
    GLYPH,
    STEP_COLUMNS,
    TASK_COLUMNS,
    Sheet,
    checked_steps,
    fact,
    message_sheet,
    run_facts,
    step_facts,
    step_presentation,
    step_row,
    task_facts,
    task_row,
    terminal_value,
    uniform_unmeasured,
    uniform_unmeasured_parts,
)
from spectragent.tui.style import styled, tone_styles
from spectragent.tui.summary import summary_sheet
from spectragent.tui.text import EvidenceText, copy_text
from spectragent.tui.widgets import Caption, Column, FactScroll, FactSheet, KeyTable, Panel

T = TypeVar("T")

TASKS_HELP = """
**Tasks** the command selected, in order. `Enter` or moving the cursor shows one in Details.

*Ending* is how the agent stopped (`done` is not a task verdict); *Trace* is the recording's
state. A queued task has not started; an interrupted one stopped with the session.
"""

STEPS_HELP = """
**Observed steps** of the current task, as this process saw them. They are not yet
verified as committed: `o` reads the committed trace.

- The cursor follows the newest step while it is on the last row; move up to stay put.
- *Origin*: `● policy` the agent decided it, `↺ restored` re-executed from the source run,
  `■ terminal` the final observation. `◆` marks the branch checkpoint.
- *Check* is the restorer's tolerance verdict; *Measured* counts `≠` measured differences
  and `?` facts it did not measure. Nothing unmeasured is drawn as zero.
- A fact unmeasured at every step observed so far is stated once above the table instead
  of on every row; each step's details still list it.
"""

DETAILS_HELP = """
**Details** of the task or step under the cursor: a summary, then the full JSON.

Select JSON with the mouse or `Shift`+arrows, `F7` selects all, `Ctrl+C` copies the
selection (or all of the JSON) here and never cancels. `F8` copies the whole JSON and `F9`
the summary as plain text, from any panel. `z` gives the panel the whole screen; `Esc`
returns.

Outside the text areas `Ctrl+C`, like `q` (Close TUI), cancels a run in progress and waits
for its close-out before the terminal closes.
"""

LOG_HELP = """
**Log** lines from this session. While text is selected the log stops updating, so the
selection stays put; clear the selection to resume. `Ctrl+C` copies here.
"""


class TaskTable(KeyTable):
    BINDINGS = [Binding("enter", "select_cursor", "Details")]


class StepTable(KeyTable):
    BINDINGS = [Binding("enter", "select_cursor", "Details")]


COPY_KEYS = Binding.Group("Copy JSON / summary")

TASK_SPEC = [
    Column(TASK_COLUMNS[0], None, min_width=18, weight=1),
    Column(TASK_COLUMNS[1], 13),
    Column(TASK_COLUMNS[2], 9),
    Column(TASK_COLUMNS[3], 10),
    Column(TASK_COLUMNS[4], 9, hide_below=72),
    Column(TASK_COLUMNS[5], 7, hide_below=80),
    Column(TASK_COLUMNS[6], None, min_width=12, weight=2, hide_below=96),
]
STEP_SPEC = [
    Column(STEP_COLUMNS[0], 4),
    Column(STEP_COLUMNS[1], 12),
    Column(STEP_COLUMNS[2], 10),
    Column(STEP_COLUMNS[3], 28, hide_below=96),
    Column(STEP_COLUMNS[4], None, min_width=20, weight=3),
    Column(STEP_COLUMNS[5], 6, hide_below=110),
    Column(STEP_COLUMNS[6], None, min_width=8, weight=1),
]


def _observed(row: dict[str, Any]) -> dict[str, Any]:
    """An observed step's record, falling back to the snapshot's own fields."""
    return {"step": row["step"], "origin": row["origin"], "error": row["error"], **row["detail"]}


class _ProgressLogHandler(logging.Handler):
    """A per-app handler; it does not retain an app or call widgets from threads."""

    def __init__(self, state: ProgressState) -> None:
        super().__init__()
        self.state = state
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.state.log(self.format(record))


class MonitorApp(App[None], Generic[T]):
    """Cancelling or closing waits for the run's trace and environment cleanup.

    Rendering reads snapshots on a timer; no hook has a reference to this app.
    A completed operation stays visible until the user closes the terminal.
    """

    TITLE = "All4One · Running experiment"
    ESCAPE_TO_MINIMIZE = False
    VERTICAL_BREAKPOINTS = [(0, "-short"), (34, "-tall")]
    CSS = """
    #topbar { height: 1; padding: 0 1; background: $panel; }
    #summary { height: auto; padding: 0 1 1 1; }
    #tasks-panel { height: auto; max-height: 8; }
    #tasks-panel:focus-within { max-height: 50%; }
    #tasks { height: auto; }
    #steps-panel { height: 1fr; min-height: 6; }
    #detail-panel { height: 1fr; min-height: 8; }
    #details { height: 1fr; }
    #uniform { height: auto; max-height: 2; }
    #log-panel { height: 6; }
    #log-panel:focus-within { height: 1fr; }
    Screen.-short #summary { padding: 0 1; }
    Screen.-short #tasks-panel { max-height: 5; }
    Screen.-short #steps-panel, Screen.-short #detail-panel { min-height: 4; }
    Screen.-short #log-panel { height: 3; }
    """
    BINDINGS = [
        Binding("ctrl+c", "copy_or_cancel", "Copy / cancel", priority=True),
        Binding("q", "cancel", "Close TUI", priority=True),
        Binding("escape", "back", "Back"),
        Binding("o", "inspect_trace", "Inspect trace"),
        Binding("z", "zoom", "Zoom"),
        Binding("question_mark", "keys", "Keys"),
        Binding("f8", "copy_details", "Copy JSON", priority=True, group=COPY_KEYS),
        Binding("f9", "copy_summary", "Copy summary", priority=True, group=COPY_KEYS),
        Binding("left_square_bracket", "step(-1)", "Previous step", show=False),
        Binding("right_square_bracket", "step(1)", "Next step", show=False),
        Binding("1", "focus_panel('tasks')", "Tasks", show=False),
        Binding("2", "focus_panel('steps')", "Steps", show=False),
        Binding("3", "focus_panel('details')", "Details", show=False),
        Binding("4", "focus_panel('events')", "Log", show=False),
    ]

    def __init__(self, factory: Callable[[], Awaitable[T]], state: ProgressState) -> None:
        super().__init__()
        self.progress = state
        self._factory = factory
        self.operation: asyncio.Task[None] | None = None
        self._value: T | None = None
        self._failure: BaseException | None = None
        self._complete = False
        self._close_requested = False
        self._cancel_requested = False
        self._last_tasks: list[dict[str, Any]] = []
        self._last_steps: list[dict[str, Any]] = []
        self._last_events: list[str] = []
        self._last_summary: Sheet = []
        self._following = True
        self._uniform: tuple[str, ...] = ()
        self._log_handler = _ProgressLogHandler(state)
        self._suspended_handlers: list[logging.Handler] = []

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(("All4One", "bold"), "  Running experiment"), id="topbar")
        yield FactSheet(id="summary")
        with Panel("Tasks", key="1", id="tasks-panel", help=TASKS_HELP):
            yield TaskTable(TASK_SPEC, id="tasks")
        with Panel("Observed steps", key="2", id="steps-panel", help=STEPS_HELP):
            yield Static("No step observed yet.", classes="empty")
            yield Caption("", id="uniform")
            yield StepTable(STEP_SPEC, id="steps")
        with Panel("Details", key="3", id="detail-panel", help=DETAILS_HELP):
            with FactScroll():
                yield FactSheet(id="facts")
            yield Caption("Full record · F7 select all · Ctrl+C copy · F8 all JSON · F9 summary")
            yield EvidenceText(id="details", language="json")
        with Panel("Log", key="4", id="log-panel", help=LOG_HELP):
            yield EvidenceText(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#steps-panel", Panel).set_subtitle(
            "seen in this process · o reads the committed trace"
        )
        self.query_one("#facts", FactSheet).show(
            message_sheet(
                "Details",
                ["Move through tasks or steps to see one here; Enter moves into it."],
                "muted",
            )
        )
        self.set_interval(0.15, self.refresh_progress)
        self.refresh_progress()
        self.query_one("#steps", DataTable).focus()
        self.begin_capture_print(self)
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if (
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, logging.FileHandler)
                and any(
                    handler.stream is stream
                    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
                )
            ):
                root_logger.removeHandler(handler)
                self._suspended_handlers.append(handler)
        root_logger.addHandler(self._log_handler)
        self.operation = asyncio.create_task(self._execute(), name="experiment-operation")
        self.operation.add_done_callback(self._operation_done)

    def on_print(self, event: Print) -> None:
        self.progress.log(event.text)

    def _operation_done(self, task: asyncio.Task[None]) -> None:
        # A task cancelled before its coroutine first runs cannot enter its
        # try/finally. It still belongs to this app and must settle.
        if not self._complete:
            self._failure = asyncio.CancelledError() if task.cancelled() else task.exception()
            self._complete = True
            if not self.progress.finished:
                self.progress.finish("cancelled" if task.cancelled() else "failed")
        if self._close_requested:
            super().exit()

    async def _execute(self) -> None:
        try:
            self._value = await self._factory()
        except BaseException as exc:
            self._failure = exc
            self.progress.log(f"{type(exc).__name__}: {exc}")
            if not self.progress.finished:
                self.progress.finish(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                )
        else:
            if not self.progress.finished:
                self.progress.finish("finished")
        finally:
            self._complete = True
            self.refresh_progress()
            if self._close_requested:
                super().exit()

    def operation_result(self) -> T:
        if not self._complete:
            raise RuntimeError("The experiment operation did not finish")
        if self._failure is not None:
            raise self._failure
        return cast(T, self._value)

    def refresh_progress(self) -> None:
        if not self.is_mounted:
            return
        snapshot = self.progress.snapshot()
        summary = summary_sheet(snapshot, complete=self._complete)
        if summary != self._last_summary:
            self.query_one("#summary", FactSheet).show(summary)
            self._last_summary = summary
        tasks = snapshot["tasks"]
        if tasks != self._last_tasks:
            self.query_one("#tasks", KeyTable).set_rows(
                [(row["row_key"], task_row(row)) for row in tasks]
            )
            finished = sum(row.get("status") == "finished" for row in tasks)
            self.query_one("#tasks-panel", Panel).set_subtitle(
                f"{finished} of {len(tasks)} finished" if tasks else None
            )
            self._last_tasks = tasks
        steps = snapshot["steps"]
        if steps != self._last_steps:
            self._render_steps(snapshot)
            self._last_steps = steps
        events = snapshot["events"]
        if events != self._last_events:
            log = self.query_one("#events", EvidenceText)
            if not log.selected_text:
                tail = log.scroll_y >= log.max_scroll_y
                log.load_text("\n".join(events))
                if tail:
                    log.move_cursor(log.document.end)
                    log.scroll_end(animate=False)
                self._last_events = events

    def _checkpoint(self, snapshot: dict[str, Any]) -> int | None:
        return (snapshot.get("branch") or {}).get("checkpoint_step")

    def _render_steps(self, snapshot: dict[str, Any]) -> None:
        table = self.query_one("#steps", KeyTable)
        checkpoint = self._checkpoint(snapshot)
        records = [_observed(row) for row in snapshot["steps"]]
        self._uniform = uniform_unmeasured(records)
        uniform = self.query_one("#uniform", Caption)
        uniform.display = bool(self._uniform)
        uniform.update(
            styled(
                [
                    ("Unmeasured ", "strong"),
                    *uniform_unmeasured_parts(self._uniform, checked_steps(records)),
                ],
                tone_styles(self),
                ellipsis=False,
            )
        )
        rows = [
            (
                str(record["step"]),
                step_row(record, checkpoint=checkpoint, uniform=self._uniform),
            )
            for record in records
        ]
        table.set_rows(rows)
        self.query_one("#steps-panel", Panel).query_one(".empty", Static).display = not rows
        if rows and self._following:
            table.select_key(rows[-1][0])
            if self.focused is table:
                self._show_step(rows[-1][0], focus=False)

    def _show_details(
        self,
        value: Any,
        sheet: Sheet | None = None,
        *,
        focus: bool = True,
        subtitle: str | None = None,
    ) -> None:
        detail = self.query_one("#details", EvidenceText)
        detail.load_text(
            json.dumps(terminal_value(value), ensure_ascii=False, indent=2, default=str)
        )
        self.query_one("#facts", FactSheet).show(sheet or [])
        self.query_one("#detail-panel", Panel).set_subtitle(subtitle)
        if focus:
            self._enter_details()

    def _enter_details(self) -> None:
        self.call_after_refresh(self.query_one("#details", EvidenceText).focus)

    def _show_task(self, key: str, *, focus: bool) -> None:
        row = next(
            (row for row in self.progress.snapshot()["tasks"] if row["row_key"] == key), None
        )
        if row is not None:
            self._show_details(row, task_facts(row), focus=focus, subtitle=f"task {row['task_id']}")

    def _show_step(self, key: str, *, focus: bool) -> None:
        snapshot = self.progress.snapshot()
        row = next((row for row in snapshot["steps"] if str(row["step"]) == key), None)
        if row is None:
            return
        sheet = step_facts(
            _observed(row), checkpoint=self._checkpoint(snapshot), uniform=self._uniform
        )
        sheet.append(fact("Committed", ("not verified here; o reads the committed trace", "muted")))
        self._show_details(
            {
                "observed_step": step_presentation(row["detail"]),
                "persistence": "Use Inspect trace to verify committed evidence.",
            },
            sheet,
            focus=focus,
            subtitle=f"observed step {key}",
        )

    @on(KeyTable.Moved, "#tasks")
    def task_moved(self, event: KeyTable.Moved) -> None:
        self._show_task(event.key, focus=False)

    @on(DataTable.RowSelected, "#tasks")
    def task_selected(self, event: DataTable.RowSelected) -> None:
        self._show_task(str(event.row_key.value), focus=True)

    @on(KeyTable.Moved, "#steps")
    def step_moved(self, event: KeyTable.Moved) -> None:
        self._following = event.table.at_last_row()
        self._show_step(event.key, focus=False)

    @on(DataTable.RowSelected, "#steps")
    def step_selected(self, event: DataTable.RowSelected) -> None:
        self._show_step(str(event.row_key.value), focus=True)

    def action_step(self, offset: int) -> None:
        table = self.query_one("#steps", KeyTable)
        keys = table.keys
        if not keys:
            return
        current = table.current_key
        index = keys.index(current) + offset if current in keys else len(keys) - 1
        index = max(0, min(len(keys) - 1, index))
        table.select_key(keys[index])
        self._following = index == len(keys) - 1
        self._show_step(keys[index], focus=False)

    def action_back(self) -> None:
        """Esc leaves one level: zoom first, then details or log back to the steps."""
        focused = self.focused
        if self.screen.maximized is not None:
            self.screen.minimize()
            return
        if not (self._within(focused, "#detail-panel") or self._within(focused, "#log-panel")):
            return
        steps = self.query_one("#steps", KeyTable)
        (steps if steps.row_count else self.query_one("#tasks", DataTable)).focus()

    def _within(self, widget: Any, selector: str) -> bool:
        if widget is None:
            return False
        panel = self.query_one(selector)
        return widget is panel or panel in widget.ancestors

    def action_focus_panel(self, name: str) -> None:
        if name == "details":
            self._enter_details()
        else:
            self.query_one(f"#{name}").focus()

    def action_zoom(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
        elif self.focused is not None:
            self.screen.maximize(self.focused)

    def action_keys(self) -> None:
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    async def action_inspect_trace(self) -> None:
        from spectragent.inspection.repository import open_repository

        path = self.progress.snapshot()["trace_path"]
        if path is None:
            self._show_details(
                {"trace": "No trace has been opened for this run."},
                message_sheet("Committed trace", ["No trace has been opened for this run."]),
            )
            return
        try:
            result = await asyncio.to_thread(lambda: open_repository(Path(path)).run("."))
        except Exception as exc:
            self._show_details(
                {"trace": path, "error": str(exc)},
                message_sheet(f"{GLYPH['bad']} Cannot read the trace", [path, str(exc)], "bad"),
            )
        else:
            self._show_details(
                {
                    **result,
                    "steps": [step_presentation(record) for record in result.get("steps", [])],
                },
                run_facts(result),
                subtitle="committed trace",
            )

    def _request_close(self) -> None:
        self._close_requested = True
        if self._complete:
            super().exit()
        elif not self._cancel_requested:
            self._cancel_requested = True
            self.progress.cancelling()
            if self.operation is not None:
                self.operation.cancel()

    def action_cancel(self) -> None:
        self._request_close()

    async def action_copy_details(self) -> None:
        details = self.query_one("#details", EvidenceText)
        await copy_text(self, details.text, copied="Copied the details JSON")

    async def action_copy_summary(self) -> None:
        await copy_text(
            self, self.query_one("#facts", FactSheet).plain_text, copied="Copied the summary"
        )

    async def action_copy_or_cancel(self) -> None:
        if isinstance(self.focused, EvidenceText):
            await self.focused.action_copy()
        else:
            self._request_close()

    def action_quit(self) -> None:
        self._request_close()

    def exit(self, result: None = None, **kwargs: Any) -> None:
        # Includes programmatic window closure: never discard an owned run.
        if self.operation is not None and not self._complete:
            self._request_close()
        else:
            super().exit(result, **kwargs)

    async def on_unmount(self) -> None:
        self.end_capture_print(self)
        operation = self.operation
        if operation is not None and not operation.done():
            if not self._cancel_requested:
                self._cancel_requested = True
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        root_logger = logging.getLogger()
        root_logger.removeHandler(self._log_handler)
        self._log_handler.close()
        for handler in self._suspended_handlers:
            root_logger.addHandler(handler)
        self._suspended_handlers.clear()
