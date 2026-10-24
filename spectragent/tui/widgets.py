"""Shared building blocks of the two terminal apps.

``Panel`` is the one bordered region: its title names it and its jump key,
its subtitle carries a count or a state, and only the focused panel's border
lights up. Nothing inside a panel has a border of its own.

``KeyTable`` keeps the cursor on the same row key when its rows are
replaced, fits a flexible column to the available width, and reports cursor
moves that the user made (keys, mouse) separately from the app's own
repositioning, so a detail view can follow the user without reacting to
refreshes.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.markup import escape
from textual.message import Message
from textual.widgets import DataTable, Static

from spectragent.tui.presentation import Part, Sheet, Tone, sheet_text
from spectragent.tui.style import sheet_renderable, styled, tone_styles


class Panel(Vertical):
    """A titled, maximizable region whose border shows where the focus is."""

    ALLOW_MAXIMIZE = True
    DEFAULT_CSS = """
    Panel {
        border: round $border-blurred;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
        border-title-align: left;
        border-subtitle-align: right;
        height: 1fr;
        padding: 0;
        &:focus-within {
            border: round $border;
            border-title-color: $text;
            border-title-style: bold;
        }
        & > .empty { color: $text-muted; padding: 0 1; height: auto; }
    }
    """

    def __init__(self, title: str, *, key: str | None = None, id: str, help: str = "") -> None:
        super().__init__(id=id)
        self.label = title
        self.jump_key = key
        # HelpPanel reads the nearest ancestor's HELP: each panel has its own.
        object.__setattr__(self, "HELP", help)
        self.set_title()

    def set_title(self, detail: str | None = None) -> None:
        prefix = f"\\[{self.jump_key}] " if self.jump_key else ""
        suffix = f" · {escape(detail)}" if detail else ""
        self.border_title = f"{prefix}{escape(self.label)}{suffix}"

    def set_subtitle(self, text: str | None) -> None:
        self.border_subtitle = escape(text) if text else None


@dataclass(frozen=True)
class Column:
    """A table column: a fixed width, or ``None`` to share the remaining width.

    ``hide_below`` drops the column when the table is narrower than that many
    cells (the details still hold its value). ``keep_end`` truncates from the
    left, for paths whose distinguishing part is their end.
    """

    title: str
    width: int | None = None
    min_width: int = 8
    weight: int = 1
    hide_below: int = 0
    keep_end: bool = False


class KeyTable(DataTable):
    """A row table whose cursor follows row keys across refreshes."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_top", "First row", show=False),
        Binding("G", "scroll_bottom", "Last row", show=False),
    ]
    DEFAULT_CSS = """
    KeyTable { height: 1fr; scrollbar-gutter: stable; background: transparent; }
    KeyTable > .datatable--header { text-style: bold; color: $text-muted; background: transparent; }
    """

    class Moved(Message):
        """The user moved the cursor onto ``key``."""

        def __init__(self, table: KeyTable, key: str) -> None:
            super().__init__()
            self.table = table
            self.key = key

        @property
        def control(self) -> KeyTable:
            return self.table

    def __init__(self, columns: Sequence[Column], *, id: str) -> None:
        super().__init__(id=id, cursor_type="row", cell_padding=1)
        self.spec = tuple(columns)
        self._rows: list[tuple[str, list[list[Part]]]] = []
        self._widths: tuple[tuple[int, int], ...] = ()
        self._quiet = 0

    @contextmanager
    def quietly(self) -> Iterator[None]:
        """Reposition the cursor without reporting a user move."""
        self._quiet += 1
        try:
            yield
        finally:
            self._quiet -= 1

    def watch_cursor_coordinate(self, old: Coordinate, new: Coordinate) -> None:
        super().watch_cursor_coordinate(old, new)
        if not self._quiet and old != new and (key := self.current_key) is not None:
            self.post_message(self.Moved(self, key))

    @property
    def current_key(self) -> str | None:
        if not self.row_count:
            return None
        try:
            return str(self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value)
        except Exception:
            return None

    @property
    def keys(self) -> list[str]:
        return [key for key, _ in self._rows]

    def at_last_row(self) -> bool:
        return not self.row_count or self.cursor_coordinate.row == self.row_count - 1

    def set_rows(
        self, rows: Sequence[tuple[str, list[list[Part]]]], cursor: str | None = None
    ) -> None:
        """Replace all rows; the cursor stays on ``cursor`` (default: its current key)."""
        target = self.current_key if cursor is None else cursor
        self._rows = list(rows)
        with self.quietly():
            self._render(target)

    def select_key(self, key: str) -> bool:
        """Move the cursor to ``key`` without reporting a user move."""
        keys = self.keys
        if key not in keys:
            return False
        with self.quietly():
            self.move_cursor(row=keys.index(key))
        return True

    def _layout(self) -> tuple[tuple[int, int], ...]:
        """(column index, width) of the columns that fit the current width.

        Flexible columns get their minimum first and share what is left by
        weight, so the row never overflows into a horizontal scrollbar while
        the minimums fit.
        """
        width = self.size.width
        shown = [
            (index, column)
            for index, column in enumerate(self.spec)
            if not column.hide_below or width >= column.hide_below
        ]
        # Rows added after this layout may bring in the vertical scrollbar,
        # which narrows the table further: reserve it when the rows will scroll.
        scrollbar = self.styles.scrollbar_size_vertical
        inner = self.scrollable_content_region.width
        if len(self._rows) + 1 > self.size.height:
            inner = min(inner, self.size.width - 2 * scrollbar)
        available = inner - 2 * self.cell_padding * len(shown)
        flexible = [column for _, column in shown if column.width is None]
        fixed = sum(column.width or 0 for _, column in shown)
        spare = max(0, available - fixed - sum(column.min_width for column in flexible))
        weights = sum(column.weight for column in flexible) or 1
        return tuple(
            (
                index,
                column.width
                if column.width is not None
                else column.min_width + spare * column.weight // weights,
            )
            for index, column in shown
        )

    def _cell(
        self, parts: list[Part], width: int, keep_end: bool, styles: dict[Tone, Style]
    ) -> Text:
        text = styled(parts, styles)
        if keep_end and text.cell_len > width > 1:
            text = text[len(text) - (width - 1) :]
            text = Text("…", style=text.style, no_wrap=True, end="") + text
        return text

    def _render(self, cursor: str | None) -> None:
        styles = tone_styles(self.app)
        self._widths = self._layout()
        self.clear(columns=True)
        for index, width in self._widths:
            self.add_column(self.spec[index].title, width=width, key=str(index))
        for key, cells in self._rows:
            self.add_row(
                *(
                    self._cell(cells[index], width, self.spec[index].keep_end, styles)
                    for index, width in self._widths
                ),
                key=key,
            )
        keys = self.keys
        if cursor in keys:
            self.move_cursor(row=keys.index(cursor), animate=False)

    def restyle(self) -> None:
        with self.quietly():
            self._render(self.current_key)

    def on_resize(self, event: events.Resize) -> None:
        if self.spec and self._layout() != self._widths:
            self.restyle()
        elif self.row_count:
            # A panel that shrinks keeps the cursor row in view.
            with self.quietly():
                self.move_cursor(row=self.cursor_coordinate.row, animate=False)

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self.restyle())


class FactSheet(Static):
    """Labelled facts in reading order, styled by tone from the active theme."""

    DEFAULT_CSS = "FactSheet { height: auto; padding: 0 1; }"

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.sheet: Sheet = []

    def show(self, sheet: Sheet) -> None:
        self.sheet = list(sheet)
        self._paint()

    @property
    def plain_text(self) -> str:
        return sheet_text(self.sheet)

    def _paint(self) -> None:
        self.update(sheet_renderable(self.sheet, tone_styles(self.app)) if self.sheet else "")

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, lambda _theme: self._paint())


class FactScroll(VerticalScroll):
    """Scrolls a long fact sheet with the mouse; never takes focus.

    Focus inside a details panel always lands on its text, so Ctrl-C there
    copies rather than reaching the activity area's cancel.
    """

    can_focus = False
    DEFAULT_CSS = "FactScroll { height: auto; max-height: 70%; scrollbar-size-vertical: 1; }"


class Caption(Static):
    """A one-line muted note between regions of a panel."""

    DEFAULT_CSS = "Caption { height: 1; color: $text-muted; padding: 0 1; }"

    def __init__(self, text: str, *, id: str | None = None) -> None:
        super().__init__(Text(text), id=id)
