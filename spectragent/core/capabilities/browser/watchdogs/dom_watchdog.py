"""DOMWatchdog — DOM tree capture, cleaning and serialization.

Listens to ``BrowserStateRequestEvent``, captures the tab's DOM over CDP —
the tab's own target plus, when the profile asks for it, every
out-of-process iframe target below it (see ``dom/capture.py``) — delegates
to the configured DOMCleaner strategy, serializes with the configured
DOMSerializer strategy, and returns the cleaned DOMState.

Both strategies are *injected*, never looked up here: they arrive on
:class:`WatchdogContext` as the instances ``BrowserCapability`` received
from its ``dom_cleaner`` / ``dom_serializer`` wiring, so a knob set in
YAML (``descendants_occlude``, ``max_occlusion_nodes``,
``max_text_length``, ``max_href_length``, ...) is on the object that does
the work. Looking one up by name is what made those knobs unreachable —
twice; see ``tests/.../test_receptor_hygiene.py``.

Between cleaning and returning, the DOM budget (``dom/budget.py``) decides
what the fragment is allowed to cost, and — this is the part that matters
for the trace — records every cut on ``DOMObs.budget``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import ClassVar

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope

from spectragent.core.capabilities.browser.dom.budget import DOMBudget, DOMBudgetPass
from spectragent.core.capabilities.browser.dom.capture import (
    FRAME_CAPTURE_TIMEOUT_S,
    FrameTargets,
    capture_target,
)
from spectragent.core.capabilities.browser.dom.cleaning import (
    DOMCleanerProtocol,
    default_cleaner,
)
from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (
    REQUIRED_COMPUTED_STYLES,
)
from spectragent.core.capabilities.browser.dom.models import DOMSelectorMap, DOMState, TargetCapture
from spectragent.core.capabilities.browser.dom.serializers import (
    DOMSerializerProtocol,
    default_serializer,
)
from spectragent.core.capabilities.browser.observations import DOMObs
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserErrorEvent,
    BrowserStateRequestEvent,
    BrowserStateSummary,
)

logger = logging.getLogger(__name__)


class DOMWatchdog(BrowserWatchdog):
    """Captures DOM state via 3 CDP protocols, cleans via pluggable strategy."""

    level: ClassVar[WatchdogLevel] = "tab"
    provides: ClassVar[frozenset[str]] = frozenset({"browser.dom"})

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        cleaner: DOMCleanerProtocol | None = None,
        serializer: DOMSerializerProtocol | None = None,
        budget: DOMBudget | None = None,
        viewport: tuple[int, int] = (1280, 720),
        enable_js_listener_detection: bool = False,
        enable_cross_site_iframe_capture: bool = False,
        frame_capture_timeout_s: float = FRAME_CAPTURE_TIMEOUT_S,
        selector_map_setter: Callable[[DOMSelectorMap], None] | None = None,
    ) -> None:
        self._cdp = cdp
        self._viewport = viewport
        # The *instance* the capability was configured with, knobs and all —
        # never a registry key looked up here, which is what left
        # ``BrowserUseDom_CleanerConfig`` unreachable from YAML. ``None``
        # means nobody configured one (a hand-built watchdog in a test).
        #
        # The fallback calls the package's ``default_*`` factory rather
        # than naming a class, because ``BrowserCapability.__init__``
        # answers the same question for its receptors and the two answers
        # must not drift: point the capability's default at a different
        # strategy while this line still says ``TextSerializer()`` and
        # every hand-built watchdog silently stays on the old one.
        self._cleaner: DOMCleanerProtocol = cleaner or default_cleaner()
        # Same contract as the cleaner, and the same reason it is an
        # instance: ``TextSerializer(max_text_length=...)`` is only
        # configurable if the *configured object* is the one that runs.
        self._serializer: DOMSerializerProtocol = serializer or default_serializer()
        self._budget = budget or DOMBudget()
        self._enable_js_listeners = enable_js_listener_detection
        self._frame_capture_timeout_s = frame_capture_timeout_s
        # Publishes the freshly-built selector map back to the capability
        # so actions can translate an element index into the element —
        # its backend_node_id and the session that owns it.
        self._publish_selector_map = selector_map_setter
        # The tab's out-of-process iframes, or ``None`` when the profile does
        # not capture them. Owned here because this watchdog is the only
        # thing that reads their documents; the action layer finds each
        # element's session on the element itself.
        self._frames: FrameTargets | None = (
            FrameTargets(cdp) if enable_cross_site_iframe_capture else None
        )

        # Per-scope state (set during attach).
        self._scope: EventScope | None = None
        self._session_id: str | None = None

        # Latest DOM state (accessible by other watchdogs, e.g. ActionWatchdog).
        # ``selector_map`` is *post*-budget: it is what the action layer
        # routes on, so it must hold exactly what the model was shown.
        self.current_state: DOMState | None = None
        self.selector_map: DOMSelectorMap = {}
        # What the cleaner indexed last step, *pre*-budget. Only the
        # ``is_new`` marker reads it, and it has to be the pre-budget map:
        # ``*`` means "this element was not on the page last step", and an
        # element the budget declined to pay for last step was on the page.
        # Comparing against the post-budget map makes anything the budget
        # cut and then kept — every element a ``dom_viewport_only`` run
        # scrolls back to — report as new.
        self._previous_indexed: DOMSelectorMap = {}

    @classmethod
    def build(cls, ctx: WatchdogContext) -> DOMWatchdog:
        return cls(
            ctx.cdp,
            cleaner=ctx.dom_cleaner,
            serializer=ctx.dom_serializer,
            budget=ctx.dom_budget,
            viewport=ctx.viewport,
            enable_js_listener_detection=ctx.profile.js_click_listener_detection,
            enable_cross_site_iframe_capture=ctx.profile.cross_site_iframe_capture,
            selector_map_setter=ctx.selector_map_setter,
        )

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._scope = scope
        self._session_id = session_id
        if session_id is not None and self._frames is not None:
            # Never for the browser-level session: auto-attach there would
            # adopt every tab in the browser.
            self._frames.watch(session_id)
        scope.connect(
            BrowserStateRequestEvent,
            self._on_browser_state_request,
            mode=ConnectionType.QUEUED,
        )

    def detach(self) -> None:
        """Release the frame-target event callbacks. Idempotent."""
        if self._frames is not None:
            self._frames.close()

    async def _on_browser_state_request(
        self, event: BrowserStateRequestEvent
    ) -> BrowserStateSummary | None:
        """Handle BrowserStateRequestEvent — capture + clean DOM."""
        if not (event.fragments & self.provides):
            return None

        try:
            # Capture every target of the tab, plus a parallel
            # devicePixelRatio query — CDP DOMSnapshot bounds are device
            # pixels, every other consumer wants CSS.
            capture, dpr = await self._capture_cdp_data()

            # Previous selector_map for new-node tracking — pre-budget, so
            # ``*`` marks what the page gained, not what the budget gave
            # back. See ``self._previous_indexed``.
            previous_map = self._previous_indexed or None

            # Delegate to cleaner strategy.
            state = await self._cleaner.clean(
                capture,
                self._viewport,
                previous_selector_map=previous_map,
                device_pixel_ratio=dpr,
            )
            self._previous_indexed = dict(state.selector_map)

            # One call, because the order — element cuts, serialize, length
            # cut — is the budget's contract and not this resolver's to get
            # right. Whatever it removed comes back as the report, or
            # ``None`` when it removed nothing.
            budget = DOMBudgetPass(self._budget).apply(state, self._serializer.serialize)

            # Update internal state. The selector map is post-budget, so an
            # element the budget dropped is not reachable by an action: this
            # map is what ``build_guarded_act`` resolves an index against.
            self.current_state = state
            self.selector_map = state.selector_map
            if self._publish_selector_map is not None:
                self._publish_selector_map(dict(state.selector_map))

            return BrowserStateSummary(
                fragments={
                    "browser.dom": DOMObs(
                        element_tree=state,
                        selector_map=dict(state.selector_map),
                        budget=budget,
                    ),
                }
            )

        except Exception as exc:
            logger.error("DOMWatchdog capture failed: %s", exc, exc_info=True)
            if self._scope:
                self._scope.emit(BrowserErrorEvent(
                    error_type="dom_capture",
                    message=str(exc),
                ))
            return None

    async def _capture_cdp_data(self) -> tuple[TargetCapture, float]:
        """Capture the tab and its frame targets, and query the DPR in parallel.

        Returns ``(capture, dpr)``. ``dpr`` is the live
        ``window.devicePixelRatio`` for this tab — frames share it. We
        re-query every observe rather than cache because the user may zoom
        or move windows between physical displays mid-run, and the cost
        (single Runtime.evaluate, run in parallel with the capture) is well
        under 1ms relative to the 1-5s LLM step.
        """
        cdp = self._cdp
        sid = self._session_id

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

        capture, dpr = await asyncio.gather(
            capture_target(
                cdp,
                sid,
                frames=self._frames if sid is not None else None,
                js_click_listeners=self._enable_js_listeners,
                required_computed_styles=REQUIRED_COMPUTED_STYLES,
                frame_timeout_s=self._frame_capture_timeout_s,
            ),
            get_dpr(),
        )
        return capture, dpr
