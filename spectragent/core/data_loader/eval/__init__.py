"""Browser eval data loader infrastructure."""

from spectragent.core.data_loader.eval._registrar import (
    EvalLoaderBase,
    EvalLoaderRegistrar,
)
from spectragent.core.data_loader.eval.base import BaseBrowserEvalLoader

# Trigger auto-registration
from spectragent.core.data_loader.eval.browsecomp import BrowseCompEvalLoader  # noqa: F401
from spectragent.core.data_loader.eval.lexbench_browser import LexBenchBrowserEvalLoader  # noqa: F401
from spectragent.core.data_loader.eval.online_mind2web import OnlineMind2WebEvalLoader  # noqa: F401

__all__ = [
    "BaseBrowserEvalLoader",
    "EvalLoaderBase",
    "EvalLoaderRegistrar",
    "BrowseCompEvalLoader",
    "LexBenchBrowserEvalLoader",
    "OnlineMind2WebEvalLoader",
]
