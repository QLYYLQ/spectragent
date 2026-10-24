"""Map presentation tones onto the active Textual theme.

Rich cells (tables, fact sheets) cannot read CSS variables, so the styles are
resolved from the running app's theme and rebuilt when the theme changes.
The theme's ``text-*`` colours are made for text on its own background, in
light and dark themes alike; the ANSI themes resolve to the terminal's own
16 colours. Where a token has no plain colour (an alpha blend over the
terminal default), the tone falls back to ``dim``.
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.console import Group
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.app import App
from textual.color import Color, ColorParseError

from spectragent.tui.presentation import Banner, Fact, Heading, Part, Sheet, Tone

_TOKENS: dict[Tone, str] = {
    "ok": "text-success",
    "warn": "text-warning",
    "bad": "text-error",
    "info": "text-primary",
    "accent": "text-accent",
}


def _color(value: str | None, background: str | None = None) -> Color | None:
    if not value:
        return None
    try:
        color = Color.parse(value)
    except ColorParseError:
        return None
    if color.a < 1:
        if background is None:
            return None
        try:
            color = Color.parse(background) + color
        except ColorParseError:
            return None
    return color


def tone_styles(app: App) -> dict[Tone, Style]:
    variables = app.get_css_variables()
    background = variables.get("background")
    styles: dict[Tone, Style] = {"plain": Style(), "strong": Style(bold=True)}
    for tone, token in _TOKENS.items():
        color = _color(variables.get(token), background)
        styles[tone] = Style(color=color.rich_color) if color is not None else Style(bold=True)
    muted = _color(variables.get("foreground-muted"), background)
    styles["muted"] = Style(color=muted.rich_color) if muted is not None else Style(dim=True)
    return styles


def styled(
    parts: Iterable[Part],
    styles: dict[Tone, Style],
    *,
    ellipsis: bool = True,
    badge: Tone = "plain",
) -> Text:
    """One line of parts; user data is never parsed as markup.

    A ``badge`` part takes the colour of the ``badge`` tone in reverse video,
    so the status word stands out in any theme, colour or not.
    """
    text = Text(no_wrap=ellipsis, overflow="ellipsis" if ellipsis else "fold", end="")
    for value, tone in parts:
        if tone == "badge":
            color = styles.get(badge, Style()).color
            text.append(value, Style(color=color, reverse=True, bold=True))
        else:
            text.append(value, styles.get(tone, Style()))
    return text


LABEL_WIDTH = 11


def sheet_renderable(sheet: Sheet, styles: dict[Tone, Style]) -> Group:
    """A fact sheet: bold section titles over grids with hanging indents."""
    blocks: list[Text | Table] = []
    grid: Table | None = None
    for item in sheet:
        if isinstance(item, Banner):
            banner = styled(item.parts, styles, ellipsis=False, badge=item.badge)
            banner.end = "\n"
            blocks.append(banner)
            grid = None
        elif isinstance(item, Heading):
            if blocks:
                blocks.append(Text(""))
            blocks.append(Text(item.title, style=styles["strong"], overflow="fold"))
            grid = None
        elif isinstance(item, Fact):
            if grid is None:
                grid = Table.grid(padding=(0, 1), expand=True)
                grid.add_column(no_wrap=True, width=LABEL_WIDTH)
                grid.add_column(ratio=1, overflow="fold")
                blocks.append(grid)
            grid.add_row(
                Text(item.label, style=styles["muted"], overflow="ellipsis"),
                styled(item.parts, styles, ellipsis=False),
            )
    return Group(*blocks)
