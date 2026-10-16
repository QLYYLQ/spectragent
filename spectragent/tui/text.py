"""Selectable evidence text with explicit copy, including macOS Terminal."""

import asyncio
import os
import sys

from textual.binding import Binding
from textual.widgets import TextArea


class EvidenceText(TextArea):
    """Read-only details: select with mouse/Shift-arrows, F7 selects all."""

    BINDINGS = [Binding("ctrl+c", "copy", "Copy"), Binding("f7", "select_all", "Select all")]

    def __init__(self, *, id: str) -> None:
        super().__init__(
            id=id, read_only=True, soft_wrap=True, show_line_numbers=False, tab_behavior="focus"
        )

    async def action_copy(self) -> None:
        value = self.selected_text or self.text
        if not value:
            return
        self.app.copy_to_clipboard(value)
        # Textual uses OSC 52. macOS Terminal does not support it; pbcopy is
        # local-only, since an SSH session must target the user's terminal.
        if (
            sys.platform == "darwin"
            and not os.environ.get("SSH_CONNECTION")
            and not self.app.is_headless
        ):
            try:
                process = await asyncio.create_subprocess_exec(
                    "pbcopy", stdin=asyncio.subprocess.PIPE
                )
                try:
                    await asyncio.wait_for(process.communicate(value.encode("utf-8")), timeout=2)
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                if process.returncode != 0:
                    raise OSError("pbcopy failed")
            except (OSError, TimeoutError):
                self.notify(
                    "Sent to terminal clipboard; native clipboard unavailable.", severity="warning"
                )
                return
        self.notify("Copied selection" if self.selected_text else "Copied displayed text")
