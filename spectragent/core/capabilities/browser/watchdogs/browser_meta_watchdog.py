"""BrowserMetaWatchdog — url / page title / open-tab list.

Extracted from ``BrowserCapability`` so that ``browser.browser_meta`` is
produced the same way every other fragment is: by a registered watchdog
that declares ``provides``. The capability previously special-cased this
fragment with its own resolver branch and its own cache field, which is
exactly the coupling F9 removes.

Tab enumeration is browser-level state (which target is focused is owned
by the capability, not by any one tab's ``EventScope``), so the capability
lends it to this watchdog through ``WatchdogContext.tabs_getter``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope

from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
)
from spectragent.schemas.observation.fragments import BrowserMetaObs

logger = logging.getLogger(__name__)


class BrowserMetaWatchdog(BrowserWatchdog):
    """Resolves ``browser.browser_meta`` — current url, title, open tabs."""

    level: ClassVar[WatchdogLevel] = "tab"
    provides: ClassVar[frozenset[str]] = frozenset({"browser.browser_meta"})

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        tabs_getter: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self._cdp = cdp
        self._tabs_getter = tabs_getter
        self._session_id: str | None = None

    @classmethod
    def build(cls, ctx: WatchdogContext) -> BrowserMetaWatchdog:
        return cls(ctx.cdp, tabs_getter=ctx.tabs_getter)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._session_id = session_id
        scope.connect(
            BrowserStateRequestEvent,
            self._on_browser_state_request,
            mode=ConnectionType.QUEUED,
        )

    async def _on_browser_state_request(
        self, event: BrowserStateRequestEvent
    ) -> BrowserStateSummary | None:
        if not (event.fragments & self.provides):
            return None

        url = ""
        page_title = ""
        try:
            nav_history = await self._cdp.send(
                "Page.getNavigationHistory", {}, session_id=self._session_id,
            )
            if nav_history is not None:
                entries = nav_history.get("entries", [])
                idx = nav_history.get("currentIndex", 0)
                if entries and 0 <= idx < len(entries):
                    url = entries[idx].get("url", "")
                    page_title = entries[idx].get("title", "")
        except Exception as exc:
            logger.debug(
                "Page.getNavigationHistory failed for %s: %s",
                self._session_id,
                exc,
            )

        tabs = await self._tabs_getter() if self._tabs_getter is not None else []
        return BrowserStateSummary(
            fragments={
                "browser.browser_meta": BrowserMetaObs(
                    url=url, page_title=page_title, tabs=tabs,
                ),
            }
        )
