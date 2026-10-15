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
from textual.containers import Vertical
from textual.events import Print
from textual.widgets import DataTable, Footer, Header, RichLog, Static, TabbedContent, TabPane

from spectragent.monitoring.state import ProgressState
from spectragent.tui.browser import _terminal_value

T = TypeVar("T")


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
    CSS = """
    Screen { background: $surface; }
    #summary { height: auto; padding: 0 1; border-bottom: solid $primary; }
    #views { height: 1fr; }
    TabPane { padding: 0; }
    #tasks { height: 1fr; min-height: 3; }
    #steps { height: 2fr; min-height: 3; }
    #events, #details { height: 1fr; }
    .caption { height: 1; color: $text-muted; padding-left: 1; }
    """
    BINDINGS = [
        Binding("ctrl+c", "cancel", "Cancel and close", priority=True),
        Binding("q", "cancel", "Close", priority=True),
        Binding("escape", "activity", "Activity"),
        Binding("o", "inspect_trace", "Inspect trace"),
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
        self._last_summary = ""
        self._log_handler = _ProgressLogHandler(state)
        self._suspended_handlers: list[logging.Handler] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="summary", markup=False)
        with TabbedContent(id="views", initial="activity"):
            with TabPane("Activity", id="activity"), Vertical():
                yield Static("TASKS · select a row for details", classes="caption")
                yield DataTable(id="tasks", cursor_type="row")
                yield Static(
                    "OBSERVED STEPS · persistence is verified in the trace viewer",
                    classes="caption",
                )
                yield DataTable(id="steps", cursor_type="row")
            with TabPane("Details", id="detail_view"):
                yield RichLog(id="details", markup=False, wrap=True)
            with TabPane("Logs", id="log_view"):
                yield RichLog(id="events", markup=False, wrap=True, max_lines=100)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tasks", DataTable).add_columns("Task", "State", "Agent ending", "Trace")
        self.query_one("#steps", DataTable).add_columns("Step", "Origin", "Actions", "Error")
        self.set_interval(0.15, self.refresh_progress)
        self.refresh_progress()
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
        identity = snapshot["identity"]
        phase = snapshot["phase"]
        active = snapshot["active"]
        phase_elapsed = f" · {active[-1]['elapsed_s']:.1f}s" if active else ""
        ending = f" · agent ended: {snapshot['agent_done']}" if snapshot["agent_done"] else ""
        finished = " · Press q to return" if self._complete else ""
        budget = snapshot["max_steps"] if snapshot["max_steps"] is not None else "—"
        token_limit = f" / {snapshot['token_limit']}" if snapshot["token_limit"] is not None else ""
        summary = (
            f"{snapshot['status'].upper()}  {phase}{phase_elapsed}{ending}{finished}\n"
            f"Task: {snapshot['task_id'] or '—'}  ·  "
            f"{identity.get('agent_id', '—')} / {identity.get('loop_id', '—')} / "
            f"{identity.get('llm_model', '—')}\n"
            f"Tasks {snapshot['tasks_completed']}/{snapshot['tasks_total']}  ·  "
            f"elapsed {snapshot['elapsed_s']:.1f}s  ·  policy steps {snapshot['policy_steps']}/{budget}  ·  "
            f"model calls {snapshot['calls']}\n"
            f"Known session tokens {snapshot['tokens']:,}{token_limit}  ·  "
            f"calls without usage {snapshot['usage_missing']}  ·  "
            f"Trace: {snapshot['trace_path'] or 'not opened'}"
        )
        if summary != self._last_summary:
            self.query_one("#summary", Static).update(summary)
            self._last_summary = summary
        tasks = snapshot["tasks"]
        if tasks != self._last_tasks:
            table = self.query_one("#tasks", DataTable)
            table.clear()
            for row in tasks:
                table.add_row(
                    Text(str(row["task_id"])),
                    Text(str(row.get("status", ""))),
                    Text(str(row.get("agent_done", "—"))),
                    Text(str(row.get("trace_status", "—"))),
                    key=row["row_key"],
                )
            self._last_tasks = tasks
        steps = snapshot["steps"]
        if steps != self._last_steps:
            table = self.query_one("#steps", DataTable)
            table.clear()
            for row in steps:
                table.add_row(
                    str(row["step"]),
                    row["origin"],
                    Text(row["actions"]),
                    Text(str(row["error"] or "")),
                    key=str(row["step"]),
                )
            self._last_steps = steps
        events = snapshot["events"]
        if events != self._last_events:
            log = self.query_one("#events", RichLog)
            log.clear()
            for event in events:
                log.write(Text(event))
            self._last_events = events

    def _show_details(self, value: Any) -> None:
        detail = self.query_one("#details", RichLog)
        detail.clear()
        detail.write(
            Text(json.dumps(_terminal_value(value), ensure_ascii=False, indent=2, default=str))
        )
        self.set_focus(None)
        self.query_one("#views", TabbedContent).active = "detail_view"
        self.call_after_refresh(detail.focus)

    @on(DataTable.RowSelected, "#tasks")
    def task_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        row = next(
            (row for row in self.progress.snapshot()["tasks"] if row["row_key"] == key), None
        )
        if row is not None:
            self._show_details(row)

    @on(DataTable.RowSelected, "#steps")
    def step_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        row = next(
            (row for row in self.progress.snapshot()["steps"] if str(row["step"]) == key), None
        )
        if row is not None:
            self._show_details(
                {
                    "observed_step": row["detail"],
                    "persistence": "Use Inspect trace to verify committed evidence.",
                }
            )

    def action_activity(self) -> None:
        self.set_focus(None)
        self.query_one("#views", TabbedContent).active = "activity"
        self.call_after_refresh(self.query_one("#steps", DataTable).focus)

    async def action_inspect_trace(self) -> None:
        from spectragent.inspection.repository import open_repository

        path = self.progress.snapshot()["trace_path"]
        if path is None:
            self._show_details({"trace": "No trace has been opened for this run."})
            return
        try:
            result = await asyncio.to_thread(lambda: open_repository(Path(path)).run("."))
        except Exception as exc:
            self._show_details({"trace": path, "error": str(exc)})
        else:
            self._show_details(result)

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
