"""The browser's ``CaptureCheck``: would this configuration capture the recorded screenshot? (M32)

``docs/architecture_design.md`` §4.9: viewport, format (from the recorded
bytes), device scale factor (``replayability``) and the image size it
implies are compared for ``browser.screenshot``; under ``replayability: none``
the DPR is the host's, so DPR and size are unchecked, not differences. The
capability only delegates to ``capture_check.compare_capture`` and never
needs a browser.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from spectragent.core.capabilities.base import CaptureCheck
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.capture_check import compare_capture
from spectragent.core.capabilities.browser.cursor import ScreenshotCursor
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
from spectragent.core.capabilities.browser.replay.pinned import PinnedReplayability
from spectragent.schemas.observation.failure import FragmentFailure
from tests._helpers.export_traces import screenshot


def _jpeg(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _compare(shot: object, **config: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    settings: dict[str, object] = {
        "viewport": (1280, 720),
        "screenshot_format": "png",
        "device_scale_factor": 1.0,
    }
    settings.update(config)
    comparison = compare_capture({"browser.screenshot": shot}, **settings)  # type: ignore[arg-type]
    return comparison.mismatches, comparison.unchecked


class TestTheComparison:
    @pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
    def test_the_same_pipeline_matches(self, dpr: float) -> None:
        assert _compare(screenshot(dpr=dpr), device_scale_factor=dpr) == ((), ())

    def test_a_different_viewport_is_named(self) -> None:
        mismatches, _ = _compare(screenshot(viewport=(1366, 768)))
        assert mismatches[0] == (
            "browser.screenshot: viewport 1280x720 configured, 1366x768 recorded"
        )

    def test_a_different_format_is_read_from_the_bytes(self) -> None:
        jpeg = ScreenshotObs(
            image_bytes=_jpeg(1280, 720),
            viewport_width=1280,
            viewport_height=720,
            image_width=1280,
            image_height=720,
        )
        mismatches, _ = _compare(jpeg)
        assert mismatches == (
            "browser.screenshot: screenshot_format png configured (image/png), image/jpeg recorded",
        )
        assert _compare(jpeg, screenshot_format="jpeg") == ((), ())

    def test_a_different_device_scale_factor_and_its_image_size_are_named(self) -> None:
        mismatches, unchecked = _compare(screenshot(dpr=2), device_scale_factor=1.0)
        assert mismatches == (
            "browser.screenshot: device scale factor 1 configured, 2 recorded",
            "browser.screenshot: image size 1280x720 implied by the configuration, "
            "2560x1440 recorded",
        )
        assert unchecked == ()

    def test_off_cannot_know_the_host_dpr(self) -> None:
        mismatches, unchecked = _compare(screenshot(dpr=2), device_scale_factor=0.0)
        assert mismatches == ()
        assert unchecked == (
            "browser.screenshot: device scale factor and image size (replayability none "
            "keeps the host's DPR, which configuration does not know)",
        )

    def test_unknown_recorded_sizes_are_unchecked(self) -> None:
        shot = screenshot().model_copy(update={"image_width": 0, "image_height": 0})
        mismatches, unchecked = _compare(shot)
        assert mismatches == ()
        assert unchecked == (
            "browser.screenshot: device scale factor and image size (the recorded sizes "
            "are unknown)",
        )

    def test_unreadable_bytes_leave_the_format_unchecked(self) -> None:
        shot = screenshot().model_copy(update={"image_bytes": None})
        mismatches, unchecked = _compare(shot)
        assert mismatches == ()
        assert unchecked == (
            "browser.screenshot: format (the recorded image's bytes are not readable)",
        )

    def test_no_screenshot_or_a_failed_one_compares_nothing(self) -> None:
        failure = FragmentFailure(fragment_id="browser.screenshot", error="boom")
        for recorded in (
            {},
            {"browser.screenshot": failure},
            {"browser.browser_meta": BrowserMetaObs()},
        ):
            comparison = compare_capture(
                recorded, viewport=(1, 1), screenshot_format="png", device_scale_factor=1.0
            )
            assert comparison.mismatches == () and comparison.unchecked == ()


class TestTheCapability:
    def test_it_implements_the_protocol_and_reads_only_its_configuration(self) -> None:
        cap = BrowserCapability(viewport_width=1280, viewport_height=720)
        assert isinstance(cap, CaptureCheck)
        cap.replayability = PinnedReplayability(device_scale_factor=2.0)
        comparison = cap.compare_capture({"browser.screenshot": screenshot(dpr=2)})
        assert comparison.mismatches == () and comparison.unchecked == ()
        comparison = cap.compare_capture({"browser.screenshot": screenshot(dpr=1)})
        assert "device scale factor 2 configured, 1 recorded" in comparison.mismatches[0]

    def test_the_default_off_tier_leaves_the_dpr_unchecked(self) -> None:
        cap = BrowserCapability(viewport_width=1280, viewport_height=720)
        comparison = cap.compare_capture({"browser.screenshot": screenshot(dpr=2)})
        assert comparison.mismatches == ()
        assert len(comparison.unchecked) == 1


def test_cursor_disabled_ignores_unused_size_and_quality() -> None:
    assert _compare(screenshot(), screenshot_cursor_size=96, screenshot_quality=30) == ((), ())


def test_cursor_enabled_unknown_still_compares_presentation_settings() -> None:
    image = screenshot()
    image.cursor = ScreenshotCursor(position=None, size_css_px=40)
    assert _compare(image, screenshot_cursor=True, screenshot_cursor_size=40) == ((), ())
    differences, _ = _compare(image, screenshot_cursor=False)
    assert differences == ("browser.screenshot: screenshot_cursor False configured, True recorded",)
    differences, _ = _compare(screenshot(), screenshot_cursor=True)
    assert differences == ("browser.screenshot: screenshot_cursor True configured, False recorded",)


def test_cursor_jpeg_quality_is_part_of_capture_presentation() -> None:
    image = screenshot()
    image.image_bytes = _jpeg(1280, 720)
    image.cursor = ScreenshotCursor(position=(30, 40), image_quality=70)
    differences, _ = _compare(
        image,
        screenshot_format="jpeg",
        screenshot_cursor=True,
        screenshot_quality=80,
    )
    assert differences == ("browser.screenshot: cursor image quality 80 configured, 70 recorded",)
    assert _compare(
        image,
        screenshot_format="jpeg",
        screenshot_cursor=True,
        screenshot_quality=70,
    ) == ((), ())
