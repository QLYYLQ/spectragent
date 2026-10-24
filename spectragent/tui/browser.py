"""Browse committed trace snapshots through the shared inspection repository."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Input, Static

from spectragent.tui.presentation import (
    GLYPH,
    RUN_COLUMNS,
    STEP_COLUMNS,
    Sheet,
    message_sheet,
    run_facts,
    run_row,
    run_tree,
    step_facts,
    step_presentation,
    step_row,
    terminal_value,
    tree_task_parts,
    uniform_unmeasured_parts,
)
from spectragent.tui.style import styled, tone_styles
from spectragent.tui.text import EvidenceText, copy_text
from spectragent.tui.widgets import Caption, Column, FactScroll, FactSheet, KeyTable, Panel

if TYPE_CHECKING:
    from spectragent.inspection.repository import RunRepository

RUNS_HELP = """
**Runs** under the selected root, newest first, with branch runs folded under the run
their recorded provenance names as source.

- `Enter` opens a run: its steps fill the Steps panel and its overview the Details panel.
- `Space` folds or unfolds the branches under a run; `→` unfolds, `←` folds or goes up to
  the source.
- A branch whose source is missing, ambiguous or not in the current list stays at the
  top and says so.
- `/` filters by any recorded field. While a filter is on, every group with a matching
  run is unfolded; a matching branch whose source does not match stands on its own.
- *Ending* is how the agent stopped (`done` is not a task verdict); *Trace* is whether
  the recording is complete; *Tokens* is `unknown` when no call reported usage and
  `≥` when some did not.
"""

STEPS_HELP = """
**Steps** of the open run, as committed.

- Moving the cursor shows that step in Details; `Enter` moves into Details.
- `[` and `]` step backwards and forwards from any panel.
- *Origin*: `● policy` the agent decided it, `↺ restored` re-executed from the source run,
  `■ terminal` the final observation. `◆` marks the branch checkpoint.
- *Check* is the restorer's tolerance verdict (`✓ ok`, `! warn`, `✗ diverged`).
  *Measured* counts what it compared: `≠` measured differences, `?` facts it did not
  measure. A pass with a difference shows both; nothing unmeasured is drawn as zero.
- A fact unmeasured at every checked step is stated once above the table instead of on
  every row; each step's details still list it.
"""

DETAILS_HELP = """
**Details** of the run or step under the cursor: a summary, then the full recorded JSON.

- Select JSON with the mouse or `Shift`+arrows, `F7` selects all, `Ctrl+C` copies the
  selection (or all of the JSON).
- `F8` copies the whole JSON and `F9` the summary as plain text, from any panel.
- `z` gives this panel the whole screen; `Esc` returns.
- Source replies and actions of a restored step are source material from the
  recorded run, never this run's thinking.
"""


class RunTable(KeyTable):
    BINDINGS = [
        Binding("enter", "select_cursor", "Open run"),
        Binding("space", "app.fold('toggle')", "Fold"),
        Binding("right", "app.fold('open')", "Unfold", show=False),
        Binding("left", "app.fold('close')", "Fold or go to source", show=False),
    ]


class StepTable(KeyTable):
    BINDINGS = [Binding("enter", "select_cursor", "Details")]


# Columns drop right to left as the terminal narrows; the details keep every value.
COPY_KEYS = Binding.Group("Copy JSON / summary")

RUN_SPEC = [
    Column(RUN_COLUMNS[0], None, min_width=22, weight=3),
    Column(RUN_COLUMNS[1], 13, hide_below=96),
    Column(RUN_COLUMNS[2], 8),
    Column(RUN_COLUMNS[3], 9),
    Column(RUN_COLUMNS[4], 10),
    Column(RUN_COLUMNS[5], 10),
    Column(RUN_COLUMNS[6], 9, hide_below=110),
    Column(RUN_COLUMNS[7], 7, hide_below=120),
    Column(RUN_COLUMNS[8], None, min_width=14, weight=1, hide_below=160),
    Column(RUN_COLUMNS[9], None, min_width=14, weight=2, keep_end=True, hide_below=86),
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


class BrowserApp(App[None]):
    """A keyboard-friendly experiment index and step inspector; never executes a trace."""

    TITLE = "All4One · Recorded experiments"
    ESCAPE_TO_MINIMIZE = False
    #: How long typing must pause before the search reads the runs again.
    SEARCH_DEBOUNCE_S = 0.2
    VERTICAL_BREAKPOINTS = [(0, "-short"), (34, "-tall")]
    CSS = """
    #topbar { height: 1; padding: 0 1; background: $panel; }
    #runs-panel { height: 3fr; min-height: 7; }
    #search { margin: 0 1; }
    #status { height: auto; max-height: 2; }
    #steps-panel { height: auto; min-height: 3; }
    #detail-panel { height: 1fr; min-height: 6; }
    #content { height: 1fr; }
    #uniform { height: auto; max-height: 2; }
    Screen.-run-open #runs-panel { height: 9; }
    Screen.-run-open #runs-panel:focus-within { height: 2fr; }
    Screen.-run-open #steps-panel { height: 1fr; min-height: 6; }
    Screen.-run-open #detail-panel { height: 2fr; min-height: 8; }
    Screen.-short.-run-open #runs-panel { height: 6; }
    Screen.-short #status { display: none; }
    Screen.-short.-run-open #steps-panel, Screen.-short.-run-open #detail-panel { min-height: 5; }
    """
    BINDINGS = [
        Binding("q", "quit", "Close TUI"),
        Binding("ctrl+c", "copy_or_quit", "Copy / close", priority=True),
        Binding("slash", "search", "Filter"),
        Binding("escape", "back", "Back"),
        Binding("s", "source", "Source"),
        Binding("r", "refresh_runs", "Refresh"),
        Binding("z", "zoom", "Zoom"),
        Binding("left_square_bracket", "step(-1)", "Previous step", show=False),
        Binding("right_square_bracket", "step(1)", "Next step", show=False),
        Binding("question_mark", "keys", "Keys"),
        Binding("f8", "copy_details", "Copy JSON", priority=True, group=COPY_KEYS),
        Binding("f9", "copy_summary", "Copy summary", priority=True, group=COPY_KEYS),
        Binding("1", "focus_panel('runs')", "Runs", show=False),
        Binding("2", "focus_panel('steps')", "Steps", show=False),
        Binding("3", "focus_panel('content')", "Details", show=False),
    ]

    def __init__(
        self,
        repository: RunRepository,
        initial_run: str | None = None,
        *,
        root: Path | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.root = root
        self._rows: dict[str, dict[str, Any]] = {}
        self._selected: str | None = None
        self._initial_run = initial_run
        self._selected_step: int | None = None
        self._source_selection = False
        # The branch run and checkpoint that ``s`` navigated from, if any.
        self._source_of: tuple[str, int | None] | None = None
        self._run_detail: dict[str, Any] | None = None
        self._query: str | None = None
        self._list_generation = 0
        self._detail_generation = 0
        # An explicitly requested run opens with the focus on its steps.
        self._focus_steps_on_load = initial_run is not None
        # The listed rows in list order, and which groups are unfolded: the
        # user's own folds, and those folded again while a filter is on.
        self._list: list[dict[str, Any]] = []
        self._unfolded: set[str] = set()
        self._folded_in_filter: set[str] = set()
        # Unmeasured facts shared by every checked step of the open run.
        self._uniform: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        context = f"  {self.root}" if self.root is not None else ""
        yield Static(
            Text.assemble(("All4One", "bold"), "  Recorded experiments", (context, "dim")),
            id="topbar",
        )
        with Panel("Runs", key="1", id="runs-panel", help=RUNS_HELP):
            yield Input(
                placeholder="/ to filter by task, model, agent, run or config",
                id="search",
                compact=True,
            )
            yield RunTable(RUN_SPEC, id="runs")
            yield Caption(
                "Select a run, then a step. Reading recorded evidence never replays actions.",
                id="status",
            )
        with Panel("Steps", key="2", id="steps-panel", help=STEPS_HELP):
            yield Static("Open a run with Enter to list its steps.", classes="empty")
            yield Caption("", id="uniform")
            yield StepTable(STEP_SPEC, id="steps")
        with Panel("Details", key="3", id="detail-panel", help=DETAILS_HELP):
            with FactScroll():
                yield FactSheet(id="facts")
            yield Caption("Full record · F7 select all · Ctrl+C copy · F8 all JSON · F9 summary")
            yield EvidenceText(id="content", language="json")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#steps", KeyTable).display = False
        self.query_one("#uniform", Caption).display = False
        self.action_refresh_runs()
        self.query_one("#runs", DataTable).focus()
        if self._initial_run is not None:
            self._select_run(self._initial_run)

    # --- display -------------------------------------------------------

    def _show(self, value: Any, sheet: Sheet | None = None, *, subtitle: str | None = None) -> None:
        content = self.query_one("#content", EvidenceText)
        content.load_text(
            json.dumps(terminal_value(value), indent=2, ensure_ascii=False, default=str)
        )
        self.query_one("#facts", FactSheet).show(sheet or [])
        self.query_one("#detail-panel", Panel).set_subtitle(subtitle)

    def _show_overview(self) -> None:
        detail = self._run_detail
        if detail is None:
            return
        self._show(
            {key: value for key, value in detail.items() if key != "steps"},
            run_facts(detail, self.root),
            subtitle="run overview",
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    def _checkpoint(self) -> int | None:
        return ((self._run_detail or {}).get("execution") or {}).get("checkpoint")

    def _records(self) -> list[dict[str, Any]]:
        return [item.get("record", item) for item in (self._run_detail or {}).get("steps", [])]

    # --- run list ------------------------------------------------------

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed) -> None:
        if event.value != self._query:
            self._request_runs(event.value, debounce=True)

    @on(Input.Submitted, "#search")
    def search_submitted(self) -> None:
        self.query_one("#runs", DataTable).focus()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    async def action_copy_details(self) -> None:
        content = self.query_one("#content", EvidenceText)
        await copy_text(self, content.text, copied="Copied the details JSON")

    async def action_copy_summary(self) -> None:
        await copy_text(
            self, self.query_one("#facts", FactSheet).plain_text, copied="Copied the summary"
        )

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
        self._set_status("Loading recorded runs…")
        self.run_worker(
            partial(self._load_runs, query, self._list_generation, debounce=debounce),
            group="run-list",
            exclusive=True,
            exit_on_error=False,
        )

    async def _load_runs(self, query: str, generation: int, *, debounce: bool) -> None:
        if debounce:
            await asyncio.sleep(self.SEARCH_DEBOUNCE_S)
        try:
            rows = await asyncio.to_thread(self.repository.list_runs, search=query)
        except Exception as exc:
            if self.is_mounted and generation == self._list_generation:
                self._set_status(f"Cannot read experiments: {exc}")
            return
        if not self.is_mounted or generation != self._list_generation:
            return
        self._rows = {row["key"]: row for row in rows}
        self._list = rows
        self._folded_in_filter.clear()
        if self._selected is not None:
            self._reveal(self._selected)
        self._render_runs(cursor=self._selected)
        unreadable = sum(bool(row.get("read_error")) for row in rows)
        groups = sum(node.descendants > 0 for node in run_tree(rows, ()))
        count = f"{len(rows)} runs" + (f" match “{query}”" if query else "")
        self.query_one("#runs-panel", Panel).set_subtitle(
            count
            + (f" · {groups} with branches" if groups else "")
            + (f" · {unreadable} unreadable" if unreadable else "")
        )
        if rows:
            self._set_status(
                "Trace “writing” means an open trace; whether its process lives is unknown."
            )
        elif query:
            self._set_status(f"No run matches “{query}”. Change the filter or clear it.")
        else:
            self._set_status("No recorded runs under this root. Press r after a run commits.")
        initial_run, self._initial_run = self._initial_run, None
        if self._selected in self._rows:
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
            self.screen.set_class(False, "-run-open")
            self._detail_generation += 1
            self.workers.cancel_group(self, "run-detail")
            self._clear_steps()
            self._show({}, [])

    # --- lineage folding -----------------------------------------------

    def _unfolded_now(self) -> set[str]:
        """While a filter is on, every group with a match is open unless folded again."""
        if not self._query:
            return self._unfolded
        return set(self._rows) - self._folded_in_filter

    def _render_runs(self, cursor: str | None = None) -> None:
        nodes = run_tree(self._list, self._unfolded_now())
        rows = []
        for node in nodes:
            cells = run_row(self._rows[node.key], self.root)
            if not self._rows[node.key].get("read_error"):
                cells[0] = tree_task_parts(node, self._rows[node.key])
            rows.append((node.key, cells))
        self.query_one("#runs", KeyTable).set_rows(rows, cursor=cursor)

    def _parents(self) -> dict[str, str]:
        """Each listed branch run's listed source, as the tree placed it."""
        parents: dict[str, str] = {}
        stack: list[str] = []
        for node in run_tree(self._list, set(self._rows)):
            del stack[node.depth :]
            if stack:
                parents[node.key] = stack[-1]
            stack.append(node.key)
        return parents

    def _set_unfolded(self, key: str, unfolded: bool) -> None:
        if self._query:
            (self._folded_in_filter.discard if unfolded else self._folded_in_filter.add)(key)
        else:
            (self._unfolded.add if unfolded else self._unfolded.discard)(key)

    def _reveal(self, key: str) -> None:
        """Unfold every source above ``key`` so its row is on screen."""
        parents = self._parents()
        while key in parents:
            key = parents[key]
            self._set_unfolded(key, True)

    def action_fold(self, how: str) -> None:
        """Fold or unfold the branches under the run at the cursor."""
        table = self.query_one("#runs", KeyTable)
        key = table.current_key
        if key is None:
            return
        node = next(
            (node for node in run_tree(self._list, self._unfolded_now()) if node.key == key), None
        )
        if node is None:
            return
        parent = self._parents().get(key)
        if node.descendants and (how == "open" or (how == "toggle" and not node.expanded)):
            self._set_unfolded(key, True)
        elif node.descendants and node.expanded and how in ("toggle", "close"):
            self._set_unfolded(key, False)
        elif parent is not None and how in ("toggle", "close"):
            # On a branch: fold its source and stand on it.
            self._set_unfolded(parent, False)
            key = parent
        else:
            return
        self._render_runs(cursor=key)

    def _clear_steps(self) -> None:
        self._uniform = ()
        self.query_one("#uniform", Caption).display = False
        steps = self.query_one("#steps", KeyTable)
        steps.set_rows([])
        steps.display = False
        panel = self.query_one("#steps-panel", Panel)
        panel.query_one(".empty", Static).display = True
        panel.set_title()
        panel.set_subtitle(None)

    # --- one run -------------------------------------------------------

    @on(DataTable.RowSelected, "#runs")
    def run_selected(self, event: DataTable.RowSelected) -> None:
        self._focus_steps_on_load = True
        self._select_run(str(event.row_key.value))

    def _select_run(
        self, key: str, *, step: int | None = None, source_navigation: bool = False
    ) -> None:
        self._selected = key
        self._source_selection = source_navigation
        if not source_navigation:
            self._source_of = None
        self._selected_step = step
        self._run_detail = None
        self._detail_generation += 1
        self.query_one("#steps", KeyTable).set_rows([])
        self.query_one("#uniform", Caption).display = False
        self._show({"loading_run": key}, message_sheet(f"Loading run {key}…", []))
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
                self._show(
                    {"error": str(exc)},
                    message_sheet(f"{GLYPH['bad']} Cannot read run {key}", [str(exc)], "bad"),
                )
            return
        if not self._detail_is_current(key, generation):
            return
        self._run_detail = detail
        self.screen.set_class(True, "-run-open")
        runs = self.query_one("#runs", KeyTable)
        runs.show_cursor = key in self._rows
        if key in self._rows and key not in runs.keys:
            self._reveal(key)
            self._render_runs(cursor=key)
        runs.select_key(key)
        execution = detail.get("execution") or {}
        checkpoint = execution.get("checkpoint")
        if self._source_selection and self._source_of is not None:
            # The step the branch was cut from, in its source run.
            checkpoint = self._source_of[1]
        records = self._records()
        panel = self.query_one("#steps-panel", Panel)
        scope = (
            " (outside the list filter)" if self._source_selection and key not in self._rows else ""
        )
        panel.set_title(f"{key}{scope}")
        if self._source_selection and self._source_of is not None:
            fork, step = self._source_of
            panel.set_subtitle(f"source of {fork} · checkpoint {GLYPH['checkpoint']} {step}")
        else:
            panel.set_subtitle(self._strategy_line(detail))
        panel.query_one(".empty", Static).display = not records
        if not records:
            panel.query_one(".empty", Static).update(Text("This run committed no steps."))
        everywhere = detail.get("unmeasured_everywhere")
        self._uniform = tuple((everywhere or {}).get("facts") or [])
        uniform = self.query_one("#uniform", Caption)
        uniform.display = bool(self._uniform)
        uniform.update(
            styled(
                [("Unmeasured ", "strong"), *uniform_unmeasured_parts(everywhere)],
                tone_styles(self),
                ellipsis=False,
            )
        )
        table = self.query_one("#steps", KeyTable)
        table.display = bool(records)
        table.set_rows(
            [
                (
                    str(record["step"]),
                    step_row(record, checkpoint=checkpoint, uniform=self._uniform),
                )
                for record in records
            ],
            cursor=str(self._selected_step) if self._selected_step is not None else "",
        )
        self._show_overview()
        if self._focus_steps_on_load and records:
            self._focus_steps_on_load = False
            table.focus()
        if self._selected_step is not None:
            step = self._selected_step
            if table.select_key(str(step)):
                self.call_after_refresh(table.focus)
            await self._load_step(key, step, generation)

    def _strategy_line(self, detail: dict[str, Any]) -> str | None:
        """The recorded branch strategy, visible while any step of the run is shown."""
        execution = detail.get("execution") or {}
        summary = detail.get("summary") or {}
        if execution.get("checkpoint") is None:
            policy = summary.get("policy_steps")
            return f"{policy} policy steps" if policy is not None else None
        parts = [
            f"Agent history: {execution.get('history_source') or 'unknown'}",
            f"verify: {execution.get('verify') or 'unknown'}",
            f"checkpoint {GLYPH['checkpoint']} {execution.get('checkpoint')}",
        ]
        if execution.get("continue_steps") is not None:
            parts.append(f"continue_steps {execution['continue_steps']}")
        return " · ".join(parts)

    # --- steps ---------------------------------------------------------

    @on(KeyTable.Moved, "#steps")
    def step_moved(self, event: KeyTable.Moved) -> None:
        self._open_step(int(event.key), debounce=True)

    @on(DataTable.RowSelected, "#steps")
    def step_selected(self, event: DataTable.RowSelected) -> None:
        self._open_step(int(str(event.row_key.value)), debounce=False)
        self._enter_details()

    def _enter_details(self) -> None:
        self.call_after_refresh(self.query_one("#content", EvidenceText).focus)

    def action_step(self, offset: int) -> None:
        table = self.query_one("#steps", KeyTable)
        keys = table.keys
        if not keys:
            return
        current = table.current_key
        index = keys.index(current) + offset if current in keys else 0
        index = max(0, min(len(keys) - 1, index))
        table.select_key(keys[index])
        self._open_step(int(keys[index]), debounce=True)

    def _open_step(self, index: int, *, debounce: bool) -> None:
        if self._selected is None:
            return
        self._detail_generation += 1
        self._selected_step = index
        record = next((item for item in self._records() if item.get("step") == index), None)
        sheet = (
            step_facts(record, checkpoint=self._checkpoint(), uniform=self._uniform)
            if record is not None
            else message_sheet(f"Loading step {index}…", [])
        )
        self._show({"loading_step": index, "run": self._selected}, sheet, subtitle=f"step {index}")
        self.run_worker(
            partial(self._load_step, self._selected, index, self._detail_generation, debounce),
            group="run-detail",
            exclusive=True,
            exit_on_error=False,
        )

    def _detail_is_current(self, key: str, generation: int) -> bool:
        return self.is_mounted and generation == self._detail_generation and key == self._selected

    async def _load_step(
        self, key: str, index: int, generation: int, debounce: bool = False
    ) -> None:
        if debounce:
            await asyncio.sleep(0.12)
            if not self._detail_is_current(key, generation):
                return
        try:
            detail = await asyncio.to_thread(self.repository.step, key, index)
        except Exception as exc:
            if self._detail_is_current(key, generation):
                self._show(
                    {"error": str(exc)},
                    message_sheet(f"{GLYPH['bad']} Cannot read step {index}", [str(exc)], "bad"),
                    subtitle=f"step {index}",
                )
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
                    },
                    step_facts(
                        detail["record"],
                        checkpoint=self._checkpoint(),
                        comparison=detail.get("comparison"),
                        uniform=self._uniform,
                    ),
                    subtitle=f"step {index}",
                )

    # --- navigation ----------------------------------------------------

    def action_back(self) -> None:
        """Esc leaves one level: zoom, then details, then steps, then the filter."""
        focused = self.focused
        steps = self.query_one("#steps", KeyTable)
        if self.screen.maximized is not None:
            self.screen.minimize()
        elif isinstance(focused, Input):
            self.query_one("#runs", DataTable).focus()
        elif self._within(focused, "#detail-panel"):
            self._focus_or_runs(steps)
        elif focused is steps:
            self.query_one("#runs", DataTable).focus()
            if self._run_detail is not None:
                # Leave the step: a pending step load must not replace the
                # overview, and a refresh must not reopen the step.
                self._detail_generation += 1
                self.workers.cancel_group(self, "run-detail")
                self._selected_step = None
                self._show_overview()

    def _focus_or_runs(self, steps: KeyTable) -> None:
        (steps if steps.display else self.query_one("#runs", DataTable)).focus()

    def _within(self, widget: Any, selector: str) -> bool:
        if widget is None:
            return False
        panel = self.query_one(selector)
        return widget is panel or panel in widget.ancestors

    def action_focus_panel(self, name: str) -> None:
        if name == "content":
            self._enter_details()
            return
        widget = self.query_one(f"#{name}")
        if widget.display:
            widget.focus()

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

    def action_source(self) -> None:
        detail = self._run_detail
        if detail is None:
            self._show(
                {"source_navigation": "Select a run and wait for its details to load."},
                message_sheet(
                    "Source checkpoint", ["Open a run and wait for its details to load."]
                ),
            )
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
            reason = reasons.get(
                resolution, "The recorded source could not be resolved under this root."
            )
            self._show(
                {
                    "source_run_id": execution.get("source_run_id", branch.get("source_run_id")),
                    "checkpoint_step": checkpoint,
                    "source_navigation": reason,
                },
                message_sheet("Source checkpoint", [reason], "warn"),
            )
            return
        if self.query_one("#search", Input).value:
            self.query_one("#search", Input).value = ""
            self._request_runs("", debounce=False)
        self._source_of = (self._selected or "", checkpoint)
        # Unfold the source so the branch it came from stays in view under it.
        self._set_unfolded(source, True)
        self._render_runs(cursor=self.query_one("#runs", KeyTable).current_key)
        self._select_run(source, step=checkpoint, source_navigation=True)
