"""The shared table keeps row identity and fits the width it is given."""

from textual.app import App, ComposeResult

from spectragent.tui.widgets import Column, KeyTable


class _TableApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.moves: list[str] = []

    def compose(self) -> ComposeResult:
        yield KeyTable(
            [Column("Name", None, min_width=6), Column("Wide only", 10, hide_below=100)],
            id="table",
        )

    def on_key_table_moved(self, event: KeyTable.Moved) -> None:
        self.moves.append(event.key)


def _rows(*keys: str):
    return [(key, [[(key, "plain")], [(f"{key}!", "muted")]]) for key in keys]


async def test_cursor_follows_the_row_key_and_only_user_moves_are_reported():
    app = _TableApp()
    async with app.run_test(size=(120, 20)) as pilot:
        table = app.query_one(KeyTable)
        table.set_rows(_rows("a", "b", "c"))
        assert table.select_key("b")
        table.set_rows(_rows("c", "b", "a"))
        await pilot.pause()
        assert table.current_key == "b"
        assert app.moves == []
        table.focus()
        await pilot.press("j")
        await pilot.pause()
        assert app.moves == ["a"]
        await pilot.press("g")
        await pilot.pause()
        assert app.moves == ["a", "c"]


async def test_low_priority_columns_drop_on_narrow_terminals():
    app = _TableApp()
    async with app.run_test(size=(120, 20)) as pilot:
        table = app.query_one(KeyTable)
        table.set_rows(_rows("a"))
        await pilot.pause()
        assert [str(column.label) for column in table.ordered_columns] == ["Name", "Wide only"]
        await pilot.resize_terminal(80, 20)
        await pilot.pause()
        assert [str(column.label) for column in table.ordered_columns] == ["Name"]
        assert table.row_count == 1
