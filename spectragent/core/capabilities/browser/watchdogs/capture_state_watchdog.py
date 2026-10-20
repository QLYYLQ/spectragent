"""CaptureStateWatchdog — the browser's ``capture_state`` fragment.

``docs/architecture_design.md`` §4.4, "跨平台采集状态 ``capture_state``". It
answers the same ``BrowserStateRequestEvent`` as every other browser
fragment, so it shares the capture's one settle wait, and it measures only
what no other record of the capture holds:

- **pointer**: the tab's last confirmed pointer position, read from the
  :class:`~spectragent.core.capabilities.browser.input.mouse.PointerPosition`
  the tab's input controller updates (the screenshot resolver reads the same
  one). ``known`` with the point, or ``unknown``: no move confirmed yet, or
  the last one unconfirmed. Whether the screenshot draws a cursor
  (``screenshot_cursor``) does not matter.
- **failed visible resources**: this round only images. One in-page probe, in
  the isolated world :data:`CAPTURE_STATE_WORLD` (page scripts neither see it
  nor change what it reads), counts the ``<img>`` elements of the main
  document that intersect the viewport, have a rendered size, are not hidden
  (``checkVisibility`` with opacity and ``visibility``), and finished loading
  with nothing to show (``complete`` and ``naturalWidth === 0``) from a
  non-empty ``currentSrc`` / ``src``. An image that has not finished loading
  (a lazy one not yet requested) is not a failure. The first
  :data:`~spectragent.schemas.observation.capture_state.MAX_RESOURCE_IDENTIFIERS`
  are named by the last segment of their URL path (never its query),
  clipped to :data:`IDENTIFIER_MAX_CHARS`.

  The probe is two commands on the tab's session (``Page.createIsolatedWorld``
  for the document's world, found by name after the first time, and one
  ``Runtime.evaluate`` by value), together bounded by
  ``BrowserCapability.capture_state_timeout_s``. A probe that runs out, fails,
  or answers with something that is not its result records the fact as
  ``unknown`` with a short ``detail``, never as ``0``.

The settle wait and the tab focus move are not measured here: the capability
assigns ``settle`` and ``focus_moved`` to every browser fragment of the
capture once the resolvers answered, and :class:`BrowserCaptureStateObs`
projects them.

**This resolver never raises and always answers.** ``BrowserCapability``
treats a negotiated fragment that a capture did not produce as a failed
capture of the whole slice (every browser fragment of the observation, the
screenshot included, re-fired and then failed), so a measurement problem is
written inside the fragment as ``unknown`` instead.

It runs after the DOM and screenshot resolvers and before ``browser_meta``
(:data:`PRIORITY`), so a document that changed while it probed shows up as
the screenshot's and ``browser_meta``'s document stamps disagreeing, and the
capability re-fires the capture.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, ClassVar

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope, Participation, ResultContract

from spectragent.core.capabilities.browser.observations import BrowserCaptureStateObs
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
)
from spectragent.schemas.observation.capture_state import (
    MAX_RESOURCE_IDENTIFIERS,
    FailedVisibleResources,
    PointerState,
)

logger = logging.getLogger(__name__)

#: Connection priority: after the DOM and screenshot resolvers (agent-cdp's
#: default 0) and before ``browser_meta`` (-100), which reads the document
#: stamp last.
PRIORITY = -50

#: The isolated world the probe runs in (``Page.createIsolatedWorld``): one per
#: document, reused by name, invisible to the page's own scripts.
CAPTURE_STATE_WORLD = "af-capture-state"

#: The longest identifier a failed resource is named by, in characters.
IDENTIFIER_MAX_CHARS = 40

#: The longest ``detail`` an unknown measurement carries, in characters.
_DETAIL_MAX_CHARS = 160

#: What the page hands back per identifier before it is clipped here: bounds
#: the reply on a page whose image URLs are megabytes of ``data:``.
_IDENTIFIER_TRANSFER_CHARS = 200

#: The in-page half. ``%(names)d``: how many failed images to name;
#: ``%(transfer)d``: how much of each name to hand back.
_FAILED_IMAGES_PROBE = """
(() => {
  const limit = %(names)d;
  const vw = window.innerWidth || document.documentElement.clientWidth || 0;
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  const visibility = {checkOpacity: true, checkVisibilityCSS: true,
                      opacityProperty: true, visibilityProperty: true};
  const name = (src) => {
    try {
      const url = new URL(src, document.baseURI);
      if (url.protocol === 'data:') return 'data:' + url.pathname.split(/[;,]/)[0];
      if (url.protocol === 'blob:') return 'blob';
      const segment = url.pathname.split('/').filter(Boolean).pop();
      if (!segment) return url.hostname || url.protocol;
      try { return decodeURIComponent(segment); } catch (error) { return segment; }
    } catch (error) {
      return String(src).split(/[?#]/)[0];
    }
  };
  let count = 0;
  const names = [];
  for (const img of document.images) {
    if (!img.complete || img.naturalWidth !== 0) continue;
    const src = img.currentSrc || img.src;
    if (!src) continue;
    const box = img.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) continue;
    if (box.right <= 0 || box.bottom <= 0 || box.left >= vw || box.top >= vh) continue;
    if (typeof img.checkVisibility === 'function') {
      if (!img.checkVisibility(visibility)) continue;
    } else {
      const style = getComputedStyle(img);
      if (style.visibility !== 'visible' || style.opacity === '0') continue;
    }
    count += 1;
    if (names.length < limit) names.push(name(src).slice(0, %(transfer)d));
  }
  return {count: count, names: names};
})()
"""
_FAILED_IMAGES_EXPRESSION = _FAILED_IMAGES_PROBE % {
    "names": MAX_RESOURCE_IDENTIFIERS,
    "transfer": _IDENTIFIER_TRANSFER_CHARS,
}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unknown(detail: str) -> FailedVisibleResources:
    return FailedVisibleResources(status="unknown", detail=_clip(detail, _DETAIL_MAX_CHARS))


def failed_images_from(value: Any) -> FailedVisibleResources:
    """The fact from the probe's by-value result; ``unknown`` when it is not one."""
    if not isinstance(value, dict):
        return _unknown("the probe returned no result")
    count, names = value.get("count"), value.get("names")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or not isinstance(names, list)
        or not all(isinstance(name, str) for name in names)
    ):
        return _unknown("the probe returned no result")
    identifiers = tuple(
        _clip(name, IDENTIFIER_MAX_CHARS) for name in names[: min(count, MAX_RESOURCE_IDENTIFIERS)]
    )
    return FailedVisibleResources(status="measured", count=count, identifiers=identifiers)


class CaptureStateWatchdog(BrowserWatchdog):
    """Resolves ``browser.capture_state``: the pointer and the visible images that failed."""

    level: ClassVar[WatchdogLevel] = "tab"
    provides: ClassVar[frozenset[str]] = frozenset({"browser.capture_state"})

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        timeout_s: float = 1.0,
        pointer_getter: Callable[[], tuple[int, int] | None] | None = None,
        main_frame_id_getter: Callable[[], str | None] | None = None,
    ) -> None:
        self._cdp = cdp
        self._timeout_s = timeout_s
        self._pointer_getter = pointer_getter
        self._main_frame_id_getter = main_frame_id_getter
        self._session_id: str | None = None

    @classmethod
    def build(cls, ctx: WatchdogContext) -> CaptureStateWatchdog:
        return cls(
            ctx.cdp,
            timeout_s=ctx.capture_state_timeout_s,
            pointer_getter=ctx.pointer_position.snapshot,
            main_frame_id_getter=ctx.main_frame_id_getter,
        )

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._session_id = session_id
        connection = scope.connect(
            BrowserStateRequestEvent,
            self._on_browser_state_request,
            mode=ConnectionType.QUEUED,
            priority=PRIORITY,
            participation=Participation.HANDLER,
            filter=lambda event: (
                isinstance(event, BrowserStateRequestEvent)
                and bool(event.fragments & self.provides)
            ),
            receiver_key="browser.capture_state",
            result_contract=ResultContract(lambda value: isinstance(value, BrowserStateSummary)),
        )
        self.track_binding(scope, session_id, connections=[connection])

    async def _on_browser_state_request(
        self, event: BrowserStateRequestEvent
    ) -> BrowserStateSummary | None:
        if not (event.fragments & self.provides):
            return None
        return BrowserStateSummary(
            fragments={
                "browser.capture_state": BrowserCaptureStateObs(
                    pointer=self._pointer(),
                    failed_visible_resources=await self._failed_images(),
                ),
            },
        )

    def _pointer(self) -> PointerState:
        position = self._pointer_getter() if self._pointer_getter is not None else None
        if position is None:
            return PointerState(status="unknown")
        return PointerState(status="known", x=position[0], y=position[1])

    async def _failed_images(self) -> FailedVisibleResources:
        """The probe within the budget; ``unknown`` with the reason on any failure."""
        frame_id = self._main_frame_id_getter() if self._main_frame_id_getter else None
        if frame_id is None:
            return _unknown("the tab's main frame is not known")
        try:
            return await asyncio.wait_for(self._probe(frame_id), self._timeout_s)
        except TimeoutError:
            return _unknown(f"the probe got no answer within {self._timeout_s:g}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # measuring never fails the capture
            logger.debug("capture_state probe (sid=%s) failed: %r", self._session_id, exc)
            return _unknown(f"the probe failed: {exc}")

    async def _probe(self, frame_id: str) -> FailedVisibleResources:
        world = await self._cdp.send(
            "Page.createIsolatedWorld",
            {"frameId": frame_id, "worldName": CAPTURE_STATE_WORLD},
            session_id=self._session_id,
        )
        context = world.get("executionContextId") if isinstance(world, dict) else None
        if not isinstance(context, int):
            return _unknown("the probe's world has no execution context")
        reply = await self._cdp.send(
            "Runtime.evaluate",
            {
                "expression": _FAILED_IMAGES_EXPRESSION,
                "contextId": context,
                "returnByValue": True,
            },
            session_id=self._session_id,
        )
        if not isinstance(reply, dict):
            return _unknown("the probe returned no result")
        if reply.get("exceptionDetails"):
            return _unknown("the probe raised in the page")
        return failed_images_from((reply.get("result") or {}).get("value"))


__all__ = [
    "CAPTURE_STATE_WORLD",
    "IDENTIFIER_MAX_CHARS",
    "PRIORITY",
    "CaptureStateWatchdog",
    "failed_images_from",
]
