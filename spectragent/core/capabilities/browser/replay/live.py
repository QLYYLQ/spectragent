"""``live`` — the default network source: the network is the network.

Nothing is intercepted, nothing is isolated, no archive is opened. A
``replay_archive`` given to a capability with this source is refused at setup
(the cooperation check), as is ``replayability: pinned`` with
``base_time: from_archive`` — T0 can only come from an archive being replayed.
"""

from spectragent.core.capabilities.browser.replay.network_source import NetworkSourceBase


class LiveNetworkSource(NetworkSourceBase):
    """No knobs: the base's defaults are the live network's."""

    __registry_key__ = "live"
