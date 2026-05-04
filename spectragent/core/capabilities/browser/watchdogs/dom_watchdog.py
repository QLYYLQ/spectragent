"""DOMWatchdog — DOM tree capture and cleaning.

Listens to ``BrowserStateRequestEvent``, performs 3-protocol CDP capture
(optionally 4th for JS event listeners), delegates to the configured
DOMCleaner strategy, and returns the cleaned DOMState.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope, WatchdogBase

from spectragent.core.capabilities.browser.dom.cleaning import (
    DOMCleanerProtocol,
    DOMCleanerRegistrar,
)
from spectragent.core.capabilities.browser.dom.cleaning.browser_use import REQUIRED_COMPUTED_STYLES
from spectragent.core.capabilities.browser.dom.models import DOMSelectorMap, DOMState
from spectragent.core.capabilities.browser.dom.serializers import (
    DOMSerializerProtocol,
    DOMSerializerRegistrar,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserErrorEvent,
    BrowserStateRequestEvent,
    BrowserStateSummary,
)

logger = logging.getLogger(__name__)


class DOMWatchdog(WatchdogBase):
    """Captures DOM state via 3 CDP protocols, cleans via pluggable strategy."""

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        cleaner_type: str = "browser_use",
        serializer_type: str = "text",
        viewport: tuple[int, int] = (1280, 720),
        enable_js_listener_detection: bool = False,
    ) -> None:
        self._cdp = cdp
        self._viewport = viewport
        self._cleaner: DOMCleanerProtocol = DOMCleanerRegistrar.get(cleaner_type)()
        self._serializer: DOMSerializerProtocol | None = None
        if serializer_type:
            self._serializer = DOMSerializerRegistrar.get(serializer_type)()
        self._enable_js_listeners = enable_js_listener_detection

        # Per-scope state (set during attach).
        self._scope: EventScope | None = None
        self._session_id: str | None = None

        # Latest DOM state (accessible by other watchdogs, e.g. ActionWatchdog).
        self.current_state: DOMState | None = None
        self.selector_map: DOMSelectorMap = {}

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._scope = scope
        self._session_id = session_id
        scope.connect(
            BrowserStateRequestEvent,
            self._on_browser_state_request,
            mode=ConnectionType.QUEUED,
        )

    async def _on_browser_state_request(
        self, event: BrowserStateRequestEvent
    ) -> BrowserStateSummary | None:
        """Handle BrowserStateRequestEvent — capture + clean DOM."""
        if not event.include_dom:
            return None

        try:
            # 3-protocol parallel capture (+ optional 4th for JS listeners,
            # plus a parallel devicePixelRatio query — CDP DOMSnapshot
            # bounds are device pixels, every other consumer wants CSS).
            raw_dom, snapshot, ax_tree, js_listener_ids, dpr = await self._capture_cdp_data()

            # Previous selector_map for new-node tracking.
            previous_map = self.selector_map if self.selector_map else None

            # Delegate to cleaner strategy.
            state = await self._cleaner.clean(
                raw_dom, snapshot, ax_tree, self._viewport,
                session_id=self._session_id,
                js_click_listener_ids=js_listener_ids,
                previous_selector_map=previous_map,
                device_pixel_ratio=dpr,
            )

            # Serialize if serializer is configured.
            if self._serializer is not None and state.dom_tree is not None:
                state.serialized_dom = self._serializer.serialize(state)

            # Update internal state.
            self.current_state = state
            self.selector_map = state.selector_map

            return BrowserStateSummary(dom_state=state)

        except Exception as exc:
            logger.error("DOMWatchdog capture failed: %s", exc, exc_info=True)
            if self._scope:
                self._scope.emit(BrowserErrorEvent(
                    error_type="dom_capture",
                    message=str(exc),
                ))
            return None

    async def _capture_cdp_data(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], set[int] | None, float]:
        """Execute 3 CDP protocols in parallel (+ optional 4th + DPR query).

        Returns ``(raw_dom, snapshot, ax_tree, js_click_listener_ids, dpr)``.
        ``dpr`` is the live ``window.devicePixelRatio`` for this target;
        we re-query every observe rather than cache because the user may
        zoom or move windows between physical displays mid-run, and the
        cost (single Runtime.evaluate, run in parallel with the existing
        gather) is well under 1ms relative to the 1-5s LLM step.
        """
        import asyncio

        cdp = self._cdp
        sid = self._session_id

        async def get_document() -> dict[str, Any]:
            try:
                result = await cdp.send(
                    "DOM.getDocument",
                    {"depth": -1, "pierce": True},
                    session_id=sid,
                )
                return result or {}
            except Exception as e:
                logger.warning("DOM.getDocument failed: %s", e)
                return {}

        async def get_snapshot() -> dict[str, Any]:
            try:
                result = await cdp.send(
                    "DOMSnapshot.captureSnapshot",
                    {
                        "computedStyles": list(REQUIRED_COMPUTED_STYLES),
                        "includeDOMRects": True,
                        "includePaintOrder": True,
                    },
                    session_id=sid,
                )
                return result or {}
            except Exception as e:
                logger.warning("DOMSnapshot.captureSnapshot failed: %s", e)
                return {}

        async def get_ax_tree() -> dict[str, Any]:
            try:
                result = await cdp.send(
                    "Accessibility.getFullAXTree", {},
                    session_id=sid,
                )
                return result or {}
            except Exception as e:
                logger.warning("Accessibility.getFullAXTree failed: %s", e)
                return {}

        async def get_js_listeners() -> set[int] | None:
            if not self._enable_js_listeners:
                return None
            try:
                from spectragent.core.capabilities.browser.dom.js.event_listeners import (
                    detect_js_click_listeners,
                )
                return await detect_js_click_listeners(cdp.send, session_id=sid)
            except Exception as e:
                logger.warning("JS listener detection failed: %s", e)
                return None

        async def get_dpr() -> float:
            try:
                result = await cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": "window.devicePixelRatio || 1",
                        "returnByValue": True,
                    },
                    session_id=sid,
                )
                value = (result or {}).get("result", {}).get("value")
                if isinstance(value, (int, float)) and value > 0:
                    return float(value)
                return 1.0
            except Exception as exc:
                logger.warning("DPR query failed for session %s: %s", sid, exc)
                return 1.0

        raw_dom, snapshot, ax_tree, js_ids, dpr = await asyncio.gather(
            get_document(), get_snapshot(), get_ax_tree(),
            get_js_listeners(), get_dpr(),
        )
        return raw_dom, snapshot, ax_tree, js_ids, dpr
