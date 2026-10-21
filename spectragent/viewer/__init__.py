"""Local experiment viewer: read-only over traces, optionally labeling into its own directory.

Importing this package does not import optional web dependencies.
"""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app(
    root: Path,
    initial_run: str | None = None,
    *,
    config_source: Path | str | None = None,
    config_hash: str | None = None,
    annotations: Path | str | None = None,
    annotator: str | None = None,
) -> "FastAPI":
    """Create a local HTTP view of the committed evidence below ``root``."""
    from spectragent.viewer.server import create_app as create

    return create(
        root,
        initial_run=initial_run,
        config_source=config_source,
        config_hash=config_hash,
        annotations=annotations,
        annotator=annotator,
    )


def serve(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    initial_run: str | None = None,
    *,
    config_source: Path | str | None = None,
    config_hash: str | None = None,
    annotations: Path | str | None = None,
    annotator: str | None = None,
) -> None:
    """Serve the viewer until interrupted; the default bind is loopback only."""
    from spectragent.viewer.server import serve as start

    start(
        root,
        host=host,
        port=port,
        open_browser=open_browser,
        initial_run=initial_run,
        config_source=config_source,
        config_hash=config_hash,
        annotations=annotations,
        annotator=annotator,
    )
