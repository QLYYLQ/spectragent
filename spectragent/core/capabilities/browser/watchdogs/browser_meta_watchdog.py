"""BrowserMetaWatchdog — url / page title / open-tab list.

Extracted from ``BrowserCapability`` so that ``browser.browser_meta`` is
produced the same way every other fragment is: by a registered watchdog
that declares ``provides``. The capability previously special-cased this
fragment with its own resolver branch and its own cache field: the coupling
a watchdog registry exists to remove (``docs/architecture_design.md`` §11).

Tab enumeration is browser-level state (which target is focused is owned
by the capability, not by any one tab's ``EventScope``), so the capability
lends it to this watchdog through ``WatchdogContext.tabs_getter``.

**It runs last in the cycle and stamps what it reads (M18, §4.14).** The
tab's observation scope runs its queued handlers one at a time in priority
order; at the default priority this watchdog's URL was read before the DOM
and the screenshot and ran about 0.25 s stale on a page that was still
navigating. It now connects with :data:`LAST` priority and takes the URL and
``loaderId`` from ``Page.getFrameTree`` — the same tree the DOM capture
reads — so its stamp can be compared with theirs. The URL is the frame's
``url`` plus its ``urlFragment`` (CDP keeps the fragment apart; a hash-routed
page is still told ``…#/inbox``). The title still comes from the navigation
history, which is the only place CDP keeps it.

**Both reads are bounded** (``base.OBSERVATION_READ_TIMEOUT_S``, §4.14): a
renderer that does not answer costs this fragment its stamp, or its URL and
title, never the whole observation event.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope, Participation, ResultContract

from spectragent.core.capabilities.browser.observations import BrowserMetaObs
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
    observation_read,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
    DocumentStamp,
)

logger = logging.getLogger(__name__)

#: Connection priority: lower than every other ``BrowserStateRequestEvent``
#: handler (they use agent-cdp's default 0), so this one runs last.
LAST = -100


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
        connection = scope.connect(
            BrowserStateRequestEvent,
            self._on_browser_state_request,
            mode=ConnectionType.QUEUED,
            priority=LAST,
            participation=Participation.HANDLER,
            filter=lambda event: (
                isinstance(event, BrowserStateRequestEvent)
                and bool(event.fragments & self.provides)
            ),
            receiver_key="browser.browser_meta",
            result_contract=ResultContract(lambda value: isinstance(value, BrowserStateSummary)),
        )
        self.track_binding(scope, session_id, connections=[connection])

    async def _on_browser_state_request(
        self, event: BrowserStateRequestEvent
    ) -> BrowserStateSummary | None:
        if not (event.fragments & self.provides):
            return None

        url = ""
        page_title = ""
        try:
            nav_history = await observation_read(
                self._cdp.send("Page.getNavigationHistory", {}, session_id=self._session_id),
                "Page.getNavigationHistory",
            )
            if nav_history is not None:
                entries = nav_history.get("entries", [])
                idx = nav_history.get("currentIndex", 0)
                if entries and 0 <= idx < len(entries):
                    url = entries[idx].get("url", "")
                    page_title = entries[idx].get("title", "")
        except TimeoutError as exc:
            logger.warning("%s (session %s)", exc, self._session_id)
        except Exception as exc:
            logger.debug(
                "Page.getNavigationHistory failed for %s: %s",
                self._session_id,
                exc,
            )

        loader_id: str | None = None
        try:
            tree = await observation_read(
                self._cdp.send("Page.getFrameTree", {}, session_id=self._session_id),
                "Page.getFrameTree",
            )
            frame = ((tree or {}).get("frameTree") or {}).get("frame") or {}
            loader_id = frame.get("loaderId")
            if frame.get("unreachableUrl"):
                # An error page's frame URL is chrome-error://; the agent is
                # told the URL it tried (fragment included), as the navigation
                # history always told it.
                url = frame["unreachableUrl"]
            elif frame.get("url"):
                # ``Frame.url`` carries no fragment — CDP keeps it apart in
                # ``urlFragment`` — and a hash-routed page is told its whole
                # URL, as the navigation history told it.
                url = f"{frame['url']}{frame.get('urlFragment') or ''}"
        except TimeoutError as exc:
            logger.warning("%s (session %s); browser_meta goes unstamped", exc, self._session_id)
        except Exception as exc:
            logger.debug("Page.getFrameTree failed for %s: %s", self._session_id, exc)

        tabs = await self._tabs_getter() if self._tabs_getter is not None else []
        return BrowserStateSummary(
            fragments={
                "browser.browser_meta": BrowserMetaObs(
                    url=url,
                    page_title=page_title,
                    tabs=tabs,
                    loader_id=loader_id,
                ),
            },
            document=DocumentStamp(loader_id=loader_id, url=url) if loader_id else None,
        )
