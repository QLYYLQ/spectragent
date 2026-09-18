"""ScreenshotWatchdog — capture screenshots via CDP Page.captureScreenshot.

The encoding of an *observation* capture is capability configuration
(``screenshot_format`` / ``screenshot_quality``), routed here through
:class:`~spectragent.core.capabilities.browser.watchdogs.base.WatchdogContext`
like the trajectory and DOM-cleaning strategies. It is one CDP parameter
either way, and it is the single largest lever on a vision run's step time:
a 1280x720 Wikipedia article is 240 KB of PNG and 107 KB of JPEG q70, which
Chrome also encodes in half the time (67 ms → 34 ms, measured headless), on
top of the upload the model waits for. ``png`` stays the default and sends
byte-for-byte the parameters it always did.

The ``browser.screenshot`` *action* keeps its own per-call format/quality —
it writes a run artifact on demand, a different question from what every
observation ships — and reaches :meth:`ScreenshotWatchdog._capture` through
``ScreenshotRequestEvent``.

No capture *scale*, though it would cut a vision model's image tokens the
way quality cuts bytes. CDP exposes a scale only inside ``clip``, and a
clip rectangle is in **document** coordinates: measured against headless
Chrome, ``clip={0, 0, 1280, 720}`` on a page scrolled to y=1500 captures the
top of the document, not the viewport. Scaling the viewport therefore has to
learn the page's scroll offset first — a second CDP round trip on every
observe, which is exactly the cost negotiation exists to hold at one.
(``Emulation.setDeviceMetricsOverride`` could shrink the capture for free at
attach time, but only by changing ``window.devicePixelRatio``, which the DOM
cleaner reads live and pages use to pick ``srcset`` — a page-visible change,
and a fractional DPR is its own automation tell.)
"""

from __future__ import annotations

import base64
import logging
from typing import ClassVar, Literal

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
    ScreenshotRequestEvent,
)
from spectragent.schemas.observation.image import image_size

logger = logging.getLogger(__name__)

#: Encodings the observation fragment may be captured in. WebP is missing
#: on purpose: the ``browser.screenshot`` action can write one as an
#: artifact, but not every vision provider accepts one as input, and an
#: observation that a model cannot be shown is worse than a larger one.
ScreenshotFormat = Literal["png", "jpeg"]


class ScreenshotWatchdog(BrowserWatchdog):
    """Captures screenshots via CDP."""

    level: ClassVar[WatchdogLevel] = "tab"
    provides: ClassVar[frozenset[str]] = frozenset({"browser.screenshot"})

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        viewport: tuple[int, int] = (1280, 720),
        image_format: ScreenshotFormat = "png",
        image_quality: int = 80,
    ) -> None:
        self._cdp = cdp
        self._viewport = viewport
        self._format: ScreenshotFormat = image_format
        self._quality = image_quality
        self._session_id: str | None = None

    @classmethod
    def build(cls, ctx: WatchdogContext) -> ScreenshotWatchdog:
        return cls(
            ctx.cdp,
            viewport=ctx.viewport,
            image_format=ctx.screenshot_format,
            image_quality=ctx.screenshot_quality,
        )

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._session_id = session_id
        scope.connect(
            ScreenshotRequestEvent,
            self._on_screenshot_request,
            mode=ConnectionType.QUEUED,
        )
        scope.connect(
            BrowserStateRequestEvent,
            self._on_browser_state_request,
            mode=ConnectionType.QUEUED,
        )

    async def _on_screenshot_request(self, event: ScreenshotRequestEvent) -> bytes:
        """Capture screenshot and return the raw encoded bytes.

        The event's own ``format`` / ``quality`` win: this is the
        ``browser.screenshot`` action, which captures an artifact on
        demand and says what it wants.
        """
        return await self._capture(
            full_page=event.full_page,
            clip=event.clip,
            format_=event.format,
            quality=event.quality,
        )

    async def _on_browser_state_request(
        self, event: BrowserStateRequestEvent
    ) -> BrowserStateSummary | None:
        """Contribute screenshot to BrowserStateSummary."""
        if not (event.fragments & self.provides):
            return None
        try:
            screenshot = await self._capture()
            width, height = self._viewport
            image_width, image_height = image_size(screenshot)
            return BrowserStateSummary(
                fragments={
                    "browser.screenshot": ScreenshotObs(
                        image_bytes=screenshot,
                        viewport_width=width,
                        viewport_height=height,
                        image_width=image_width,
                        image_height=image_height,
                    ),
                }
            )
        except Exception as exc:
            logger.error("Screenshot capture failed: %s", exc)
            return None

    async def _capture(
        self,
        *,
        full_page: bool = False,
        clip: dict[str, float] | None = None,
        format_: str | None = None,
        quality: int | None = None,
    ) -> bytes:
        """Execute CDP Page.captureScreenshot and return decoded bytes.

        ``format_ is None`` is the observation path: the configured
        format, and with it the configured quality. Naming a format is
        the action path, which carries its own quality (``None`` =
        omit it, Chrome's own default) — the two must not borrow each
        other's knobs, or configuring the observation encoding would
        silently re-quality every artifact the action writes.

        ``quality`` is never sent for PNG: it is meaningless there, and
        omitting it keeps the default capture's parameters exactly what
        they have always been.
        """
        if format_ is None:
            fmt: str = self._format
            quality = self._quality
        else:
            fmt = format_
        params: dict = {"format": fmt}
        if fmt != "png" and quality is not None:
            params["quality"] = quality
        if full_page:
            params["captureBeyondViewport"] = True
        if clip:
            params["clip"] = {**clip, "scale": 1}

        result = await self._cdp.send(
            "Page.captureScreenshot",
            params,
            session_id=self._session_id,
        )
        b64_data = result.get("data", "") if result else ""
        return base64.b64decode(b64_data)
