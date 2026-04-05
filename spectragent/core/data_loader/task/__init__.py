"""Browser task data loader infrastructure."""

from spectragent.core.data_loader.task._registrar import (
    TaskLoaderBase,
    TaskLoaderRegistrar,
)
from spectragent.core.data_loader.task.base import BaseBrowserTaskLoader

# Trigger auto-registration
from spectragent.core.data_loader.task.browsecomp import BrowseCompTaskLoader  # noqa: F401
from spectragent.core.data_loader.task.lexbench_browser import LexBenchBrowserTaskLoader  # noqa: F401
from spectragent.core.data_loader.task.online_mind2web import OnlineMind2WebTaskLoader  # noqa: F401

__all__ = [
    "BaseBrowserTaskLoader",
    "TaskLoaderBase",
    "TaskLoaderRegistrar",
    "BrowseCompTaskLoader",
    "LexBenchBrowserTaskLoader",
    "OnlineMind2WebTaskLoader",
]
