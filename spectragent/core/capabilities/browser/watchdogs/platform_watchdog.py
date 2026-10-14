"""Resolve the run's measured platform without extra page or DOM reads."""

from __future__ import annotations

from typing import ClassVar

from agent_cdp import ConnectionType, EventScope

from spectragent.core.capabilities.browser.observations import BrowserPlatformObs
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
)


class PlatformWatchdog(BrowserWatchdog):
    """The platform is run state, so each tab observes the same immutable fact."""

    level: ClassVar[WatchdogLevel] = "tab"
    provides: ClassVar[frozenset[str]] = frozenset({"browser.platform"})

    def __init__(self, input_platform: InputPlatform | None) -> None:
        self._platform = input_platform

    @classmethod
    def build(cls, ctx: WatchdogContext) -> PlatformWatchdog:
        return cls(ctx.input_platform)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        scope.connect(
            BrowserStateRequestEvent, self._on_browser_state_request, mode=ConnectionType.QUEUED
        )

    async def _on_browser_state_request(
        self, event: BrowserStateRequestEvent
    ) -> BrowserStateSummary | None:
        if not event.fragments & self.provides:
            return None
        if self._platform is None:
            raise RuntimeError("browser input platform was not measured during setup")
        return BrowserStateSummary(
            fragments={"browser.platform": BrowserPlatformObs(operating_system=self._platform)}
        )
