"""Selectable evidence text with explicit copy, including macOS Terminal."""

import asyncio
import os
import sys

from rich.style import Style
from textual.app import App
from textual.binding import Binding
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme

from spectragent.tui.style import tone_styles


async def copy_text(app: App, value: str, *, copied: str) -> None:
    """Put ``value`` on the clipboard and say what was copied.

    Textual uses OSC 52. macOS Terminal does not support it; pbcopy is
    local-only, since an SSH session must target the user's terminal.
    """
    if not value:
        return
    app.copy_to_clipboard(value)
    if sys.platform == "darwin" and not os.environ.get("SSH_CONNECTION") and not app.is_headless:
        try:
            process = await asyncio.create_subprocess_exec("pbcopy", stdin=asyncio.subprocess.PIPE)
            try:
                await asyncio.wait_for(process.communicate(value.encode("utf-8")), timeout=2)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if process.returncode != 0:
                raise OSError("pbcopy failed")
        except (OSError, TimeoutError):
            app.notify(
                "Sent to terminal clipboard; native clipboard unavailable.", severity="warning"
            )
            return
    app.notify(copied)


class EvidenceText(TextArea):
    """Read-only details: select with mouse/Shift-arrows, F7 selects all.

    With ``language="json"`` the text is highlighted in colours taken from
    the active app theme; highlighting only styles the text, so selection
    and copy see exactly the recorded JSON.
    """

    BINDINGS = [Binding("ctrl+c", "copy", "Copy"), Binding("f7", "select_all", "Select all")]
    DEFAULT_CSS = """
    EvidenceText, EvidenceText:focus { border: none; padding: 0 1; background: transparent; }
    """

    def __init__(self, *, id: str, language: str | None = None) -> None:
        super().__init__(
            id=id,
            language=language,
            read_only=True,
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            highlight_cursor_line=False,
        )

    def on_mount(self) -> None:
        if self.language is not None:
            self._paint_syntax()
            self.app.theme_changed_signal.subscribe(self, lambda _theme: self._paint_syntax())

    def _paint_syntax(self) -> None:
        tones = tone_styles(self.app)
        label = tones["info"] + Style(bold=False)
        self.register_theme(
            TextAreaTheme(
                name="evidence",
                syntax_styles={
                    "json.label": label,
                    "string": Style(),
                    "number": tones["accent"],
                    "float": tones["accent"],
                    "boolean": tones["accent"],
                    # ``null`` is how the record says "unknown": never quiet.
                    "json.null": tones["warn"] + Style(italic=True),
                    "constant.builtin": tones["warn"] + Style(italic=True),
                    "punctuation.bracket": tones["muted"],
                    "punctuation.delimiter": tones["muted"],
                },
            )
        )
        self.theme = "evidence"

    async def action_copy(self) -> None:
        await copy_text(
            self.app,
            self.selected_text or self.text,
            copied="Copied selection" if self.selected_text else "Copied displayed text",
        )
