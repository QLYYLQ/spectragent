"""ScreenshotWatchdog — capture screenshots via CDP Page.captureScreenshot."""

from __future__ import annotations

import base64
import logging
from typing import ClassVar

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

logger = logging.getLogger(__name__)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> tuple[int, int]:
    """``(width, height)`` of a PNG, read from its IHDR chunk; ``(0, 0)`` if not a PNG.

    The image size is not the viewport size — the capture is rendered at the
    device scale factor — and it is already in the bytes we hold, so reading
    the 24-byte header costs nothing and keeps the observe cycle at one CDP
    call.
    """
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return 0, 0
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


class ScreenshotWatchdog(BrowserWatchdog):
    """Captures screenshots via CDP."""

    level: ClassVar[WatchdogLevel] = "tab"
    provides: ClassVar[frozenset[str]] = frozenset({"browser.screenshot"})

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        viewport: tuple[int, int] = (1280, 720),
    ) -> None:
        self._cdp = cdp
        self._viewport = viewport
        self._session_id: str | None = None

    @classmethod
    def build(cls, ctx: WatchdogContext) -> ScreenshotWatchdog:
        return cls(ctx.cdp, viewport=ctx.viewport)

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
        """Capture screenshot and return raw PNG bytes."""
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
            image_width, image_height = png_size(screenshot)
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
        format_: str = "png",
        quality: int | None = None,
    ) -> bytes:
        """Execute CDP Page.captureScreenshot and return decoded bytes."""
        params: dict = {"format": format_}
        if quality is not None:
            params["quality"] = quality
        if full_page:
            params["captureBeyondViewport"] = True
        if clip:
            params["clip"] = {**clip, "scale": 1}

        result = await self._cdp.send(
            "Page.captureScreenshot", params, session_id=self._session_id,
        )
        b64_data = result.get("data", "") if result else ""
        return base64.b64decode(b64_data)
