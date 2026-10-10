"""Replayability tiers — ``ReplayabilityRegistrar`` (``off`` / ``pinned``), §4.14.

Importing this package registers both tiers (conscribe metaclass), which is
what ``BrowserCapability`` relies on before its ``__wiring__`` is evaluated.
"""

from spectragent.core.capabilities.browser.replay.off import OffReplayability
from spectragent.core.capabilities.browser.replay.pinned import PinnedReplayability
from spectragent.core.capabilities.browser.replay.replayability import (
    ReplayabilityBase,
    ReplayabilityPlan,
    ReplayabilityProtocol,
    ReplayabilityRegistrar,
    default_replayability,
)
from spectragent.core.capabilities.browser.replay.shims import SHIM_SHA256, render_shim

__all__ = [
    "SHIM_SHA256",
    "OffReplayability",
    "PinnedReplayability",
    "ReplayabilityBase",
    "ReplayabilityPlan",
    "ReplayabilityProtocol",
    "ReplayabilityRegistrar",
    "default_replayability",
    "render_shim",
]
