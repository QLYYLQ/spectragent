"""Replay: the browser's three replay axes (§4.14).

- ``replayability`` (``ReplayabilityRegistrar``: ``none`` / ``pinned``) — how far a
  recording is pinned so it can be replayed;
- ``network_source`` (``NetworkSourceRegistrar``: ``live`` / ``archive``) — where
  the network comes from; ``archive`` replays a recorded run offline;
- ``miss_policy`` (``MissPolicyRegistrar``: ``strict``) — how an ``archive``
  source answers what the recording does not hold.

Importing this package registers every strategy of all three (conscribe
metaclass), which is what ``BrowserCapability`` relies on before its
``__wiring__`` is evaluated.

A replaying run's moving parts (M30): ``archive_index.py`` loads the source
run, ``canonical.py`` / ``matcher.py`` / ``site_rules.py`` match, ``server.py``
answers paused requests (``responder.py`` shapes the answers), ``replay_log.py``
records every decision and reads them back, ``consistency.py`` checks the
source run against the configuration before anything launches.
"""

from spectragent.core.capabilities.browser.replay.archive_source import ArchiveNetworkSource
from spectragent.core.capabilities.browser.replay.live import LiveNetworkSource
from spectragent.core.capabilities.browser.replay.miss_policy import (
    MissDecision,
    MissPolicyBase,
    MissPolicyProtocol,
    MissPolicyRegistrar,
    ReplayMiss,
    default_miss_policy,
)
from spectragent.core.capabilities.browser.replay.network_source import (
    NetworkSourceBase,
    NetworkSourceProtocol,
    NetworkSourceRegistrar,
    default_network_source,
)
from spectragent.core.capabilities.browser.replay.none import NoneReplayability
from spectragent.core.capabilities.browser.replay.pinned import PinnedReplayability
from spectragent.core.capabilities.browser.replay.replay_log import (
    ReplayLog,
    ReplayLogEntry,
    diverged_steps,
    iter_replay_log,
    replay_log,
)
from spectragent.core.capabilities.browser.replay.replayability import (
    ReplayabilityBase,
    ReplayabilityPlan,
    ReplayabilityProtocol,
    ReplayabilityRegistrar,
    default_replayability,
)
from spectragent.core.capabilities.browser.replay.server import ReplayContext, ReplayServer
from spectragent.core.capabilities.browser.replay.shims import SHIM_SHA256, render_shim
from spectragent.core.capabilities.browser.replay.strict import StrictMissPolicy

__all__ = [
    "SHIM_SHA256",
    "ArchiveNetworkSource",
    "LiveNetworkSource",
    "MissDecision",
    "MissPolicyBase",
    "MissPolicyProtocol",
    "MissPolicyRegistrar",
    "NetworkSourceBase",
    "NetworkSourceProtocol",
    "NetworkSourceRegistrar",
    "NoneReplayability",
    "PinnedReplayability",
    "ReplayContext",
    "ReplayLog",
    "ReplayLogEntry",
    "ReplayMiss",
    "ReplayServer",
    "ReplayabilityBase",
    "ReplayabilityPlan",
    "ReplayabilityProtocol",
    "ReplayabilityRegistrar",
    "StrictMissPolicy",
    "default_miss_policy",
    "default_network_source",
    "default_replayability",
    "diverged_steps",
    "iter_replay_log",
    "render_shim",
    "replay_log",
]
