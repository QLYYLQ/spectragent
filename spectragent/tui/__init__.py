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


def browse(
    root: Path,
    initial_run: str | None = None,
    *,
    config_source: Path | str | None = None,
    config_hash: str | None = None,
) -> None:
    """Browse recorded runs without starting a browser or executing actions."""
    from spectragent.inspection.repository import open_repository
    from spectragent.tui.browser import BrowserApp

    repository = open_repository(root, config_source=config_source, config_hash=config_hash)
    if initial_run is not None and not repository.matches(initial_run):
        raise ValueError("The selected run does not match the configuration filters")
    BrowserApp(repository, initial_run=initial_run, root=Path(root).expanduser().resolve()).run()


__all__ = ["browse", "run_operation"]
