"""Read-only local experiment viewer.

Importing this package does not import optional web dependencies.
"""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app(root: Path) -> "FastAPI":
    """Create a local HTTP view of the committed evidence below ``root``."""
    from spectragent.viewer.server import create_app as create

    return create(root)


def serve(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    """Serve the viewer until interrupted; the default bind is loopback only."""
    from spectragent.viewer.server import serve as start

    start(root, host=host, port=port, open_browser=open_browser)
