"""Terminal entry points; importing monitoring never imports the UI toolkit."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from spectragent.monitoring.state import ProgressState

T = TypeVar("T")


def run_operation(factory: Callable[[], Awaitable[T]], state: ProgressState) -> T:
    """Run an async CLI operation with a terminal, preserving its result or error."""
    from spectragent.tui.app import MonitorApp

    app = MonitorApp(factory, state)
    app.run()
    return app.operation_result()


def browse(root: Path) -> None:
    """Browse recorded runs without starting a browser or executing actions."""
    from spectragent.inspection.repository import open_repository
    from spectragent.tui.browser import BrowserApp

    BrowserApp(open_repository(root)).run()


__all__ = ["browse", "run_operation"]
