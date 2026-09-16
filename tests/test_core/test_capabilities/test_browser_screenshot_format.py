"""The observation capture's encoding is capability config, and it is one CDP call.

Three claims:

1. **The default is unchanged, byte for byte.** ``Page.captureScreenshot``
   still receives exactly ``{"format": "png"}`` — no ``quality``, no
   ``clip`` — and exactly once per observe. The whole point of making the
   encoding configurable is that leaving it alone costs nothing.
2. **``screenshot_format`` / ``screenshot_quality`` reach the watchdog
   through the wiring**, the way ``dom_cleaner`` and ``mouse_trajectory``
   do: capability field → ``WatchdogContext`` → ``build()``. A regression
   that forgets to thread one is a silently-still-PNG run.
3. **The observation's encoding and the action's are separate knobs.**
   ``browser.screenshot`` (the action) writes an artifact on demand and
   carries its own format/quality; configuring the observation must not
   re-quality it.

The image size the fragment reports is read from the capture's own header
either way — that is what keeps the observe cycle at the one CDP round trip
negotiation paid for, and it has to work for JPEG now that a run can ask
for one.
"""

from __future__ import annotations

import io
import zlib
from typing import Any

import pytest
from agent_cdp import EventScope, ScopeGroup

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.actions.screenshot import ScreenshotAction
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.capabilities.browser.watchdogs import ScreenshotWatchdog
from spectragent.schemas.observation.image import (
    image_media_type,
    image_size,
    image_suffix,
)


def _png(width: int, height: int) -> bytes:
    """A real PNG header + IHDR — enough for any reader of the size."""
    ihdr = b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + len(ihdr[4:]).to_bytes(4, "big")
        + ihdr
        + zlib.crc32(ihdr).to_bytes(4, "big")
    )


def _jpeg(width: int, height: int, quality: int = 70) -> bytes:
    """A real JPEG, encoded by Pillow — the segment chain is the point."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(
        buffer, format="JPEG", quality=quality
    )
    return buffer.getvalue()


class _CaptureCDP:
    """Records every ``Page.captureScreenshot`` and answers with *payload*."""

    is_connected = True

    def __init__(self, payload: bytes = b"\x89PNG\r\n\x1a\n") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._payload = payload

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        import base64

        self.calls.append((method, params or {}))
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(self._payload).decode()}
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        return None

    def off_event(self, method: str, callback: Any) -> None:
        return None

    def shots(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "Page.captureScreenshot"]


async def _observe(cap: BrowserCapability, cdp: _CaptureCDP) -> tuple[Any, ScopeGroup]:
    """One real observe cycle for ``browser.screenshot`` over a real EventScope."""
    cap._cdp = cdp  # type: ignore[assignment]
    cap.apply_negotiation({"browser.screenshot"})
    group = ScopeGroup("test")
    scope: EventScope = await group.create_scope("tab1", target_id="tab1")
    for wd in cap._create_watchdogs_for_scope(level="tab", cdp_session_id="sid1"):
        wd.attach(scope, session_id="sid1")
    cap._scopes["sid1"] = scope
    cap._default_session_id = "sid1"
    return await cap.resolve_fragment("browser.screenshot"), group


# ── 1. the default is untouched ───────────────────────────────────────


class TestDefaultIsUnchanged:
    async def test_one_call_with_exactly_the_legacy_params(self) -> None:
        cap = BrowserCapability(profile="vision_only")
        cdp = _CaptureCDP(_png(1280, 720))
        fragment, group = await _observe(cap, cdp)
        try:
            assert cdp.shots() == [{"format": "png"}]
            assert [m for m, _ in cdp.calls] == ["Page.captureScreenshot"]
            assert fragment.image_width, fragment.image_height == (1280, 720)
        finally:
            await group.close_all()

    def test_png_is_the_default_on_the_capability_and_the_watchdog(self) -> None:
        assert BrowserCapability().screenshot_format == "png"
        assert BrowserCapability().screenshot_quality == 80
        cdp = _CaptureCDP()
        assert ScreenshotWatchdog(cdp)._format == "png"  # type: ignore[arg-type]


# ── 2. the config reaches the watchdog ────────────────────────────────


class TestEncodingIsWiredThrough:
    @pytest.mark.parametrize("quality", [70, 95])
    async def test_jpeg_config_reaches_captureScreenshot(self, quality: int) -> None:
        cap = BrowserCapability(
            profile="vision_only", screenshot_format="jpeg", screenshot_quality=quality
        )
        cdp = _CaptureCDP(_jpeg(1280, 720))
        _, group = await _observe(cap, cdp)
        try:
            assert cdp.shots() == [{"format": "jpeg", "quality": quality}]
        finally:
            await group.close_all()

    async def test_the_jpeg_fragment_still_reports_its_pixel_size(self) -> None:
        # The geometry contract a vision agent converts points with. Read
        # from the bytes, so it costs no second CDP call for JPEG either.
        cap = BrowserCapability(profile="vision_only", screenshot_format="jpeg")
        cdp = _CaptureCDP(_jpeg(1280, 720))
        fragment, group = await _observe(cap, cdp)
        try:
            assert (fragment.image_width, fragment.image_height) == (1280, 720)
            assert (fragment.viewport_width, fragment.viewport_height) == (1280, 720)
            assert [m for m, _ in cdp.calls] == ["Page.captureScreenshot"]
        finally:
            await group.close_all()

    def test_the_watchdog_is_built_from_the_context(self) -> None:
        cap = BrowserCapability(screenshot_format="jpeg", screenshot_quality=55)
        cap._cdp = _CaptureCDP()  # type: ignore[assignment]
        ctx = cap._watchdog_context("sid1")
        assert (ctx.screenshot_format, ctx.screenshot_quality) == ("jpeg", 55)
        wd = ScreenshotWatchdog.build(ctx)
        assert (wd._format, wd._quality) == ("jpeg", 55)

    def test_an_unknown_format_is_a_validation_error_not_a_silent_png(self) -> None:
        with pytest.raises(Exception, match="screenshot_format|jpg"):
            _validate_capability({"capability_id": "browser", "screenshot_format": "jpg"})

    def test_quality_out_of_range_is_refused(self) -> None:
        with pytest.raises(Exception, match="screenshot_quality|less than or equal"):
            _validate_capability(
                {
                    "capability_id": "browser",
                    "screenshot_format": "jpeg",
                    "screenshot_quality": 400,
                }
            )


def _validate_capability(block: dict[str, Any]) -> Any:
    """Validate one capability config block against the generated union."""
    from spectragent.generated import ComposedConfig

    return ComposedConfig.model_validate({"capability": [block]})


# ── 3. the action keeps its own knobs ─────────────────────────────────


class TestActionAndObservationAreSeparate:
    async def test_the_action_format_wins_and_carries_its_own_quality(self) -> None:
        """An action-path capture never borrows the observation's quality.

        ``ScreenshotAction`` defaults ``quality`` to ``None``, which means
        "Chrome's own default". Letting the configured observation quality
        leak in would silently re-encode every artifact a run writes.
        """
        cdp = _CaptureCDP(_jpeg(64, 64))
        wd = ScreenshotWatchdog(cdp, image_format="jpeg", image_quality=55)  # type: ignore[arg-type]
        await wd._capture(format_="webp", quality=None)
        await wd._capture(format_="jpeg", quality=91)
        await wd._capture()  # the observation path
        assert cdp.shots() == [
            {"format": "webp"},
            {"format": "jpeg", "quality": 91},
            {"format": "jpeg", "quality": 55},
        ]

    def test_the_action_still_advertises_its_own_format_param(self) -> None:
        # The action/descriptor seam is untouched by this change.
        params = ScreenshotAction().descriptor().params
        assert {"full_page", "format", "quality"} <= set(params)


# ── the image header readers ──────────────────────────────────────────


class TestImageHeaders:
    def test_png(self) -> None:
        data = _png(2560, 1440)
        assert image_size(data) == (2560, 1440)
        assert image_media_type(data) == "image/png"
        assert image_suffix(data) == ".png"

    @pytest.mark.parametrize(("w", "h"), [(1280, 720), (37, 4001)])
    def test_jpeg_size_walks_the_segment_chain(self, w: int, h: int) -> None:
        data = _jpeg(w, h)
        assert image_size(data) == (w, h)
        assert image_media_type(data) == "image/jpeg"
        assert image_suffix(data) == ".jpg"

    def test_unknown_bytes_never_guess(self) -> None:
        for data in (b"", b"not an image", b"\xff\xd8\xff"):
            assert image_size(data) == (0, 0)
        assert image_media_type(b"not an image") is None
        assert image_suffix(b"not an image") is None


class TestTraceExtensionFollowsTheBytes:
    def test_png_bytes_are_persisted_as_png(self) -> None:
        obs = ScreenshotObs(image_bytes=_png(8, 8))
        assert obs.trace_binary() == (".png", obs.image_bytes)

    def test_jpeg_bytes_are_persisted_as_jpg(self) -> None:
        data = _jpeg(8, 8)
        obs = ScreenshotObs(image_bytes=data)
        assert obs.trace_binary() == (".jpg", data)

    def test_no_bytes_means_nothing_to_persist(self) -> None:
        assert ScreenshotObs(image_path="x.png").trace_binary() is None
