"""Would this browser configuration capture what a run recorded? (arch §4.9 ``CaptureCheck``)

The training export renders a student's samples with the student's run
configuration from screenshots another configuration took (§4.15). If the two
screenshot pipelines differ, the student is trained on images it will never
see when it serves — and nothing in the export itself can tell, because only
the capability knows what its configuration captures. ``BrowserCapability``
answers through :func:`compare_capture` (``capability.py`` only delegates).

Compared, for ``browser.screenshot`` only (other fragments are not):

- the viewport — configured ``viewport_width`` x ``viewport_height`` against
  the recorded fragment's;
- the image format — configured ``screenshot_format`` against the media type
  the recorded bytes declare in their own header (``image_media_type``);
- the device scale factor — the ``replayability`` tier's
  ``device_scale_factor`` against the recorded ``image_width / viewport_width``;
- the image size that factor implies — viewport x DPR against the recorded
  image, within a pixel per axis (Chrome rounds a fractional DPR's size).

``replayability: none`` keeps the host's DPR (``device_scale_factor`` 0), which
configuration cannot know: DPR and image size are then reported as
``unchecked``, not as differences. Reads configuration and the fragments it
is handed only — no setup, no browser, no ``core.trace`` (§10).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from spectragent.core.capabilities.base import CaptureComparison
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.schemas.observation.image import image_media_type

#: The one fragment whose capture the configuration decides.
SCREENSHOT_KEY = "browser.screenshot"
#: ``screenshot_format`` → the media type its bytes declare.
_FORMAT_MEDIA_TYPES: dict[str, str] = {"png": "image/png", "jpeg": "image/jpeg"}
#: How far a recorded DPR may sit from the configured one (a size rounding).
_DPR_TOLERANCE = 0.01
#: Pixels per axis an implied image size may differ by (Chrome's rounding).
_SIZE_TOLERANCE_PX = 1


def compare_capture(
    recorded: Mapping[str, Any],
    *,
    viewport: tuple[int, int],
    screenshot_format: str,
    device_scale_factor: float,
    screenshot_cursor: bool = False,
    screenshot_cursor_size: int = 32,
    screenshot_quality: int = 80,
) -> CaptureComparison:
    """Compare the recorded ``browser.screenshot`` with what this configuration captures.

    Args:
        recorded: One step's recorded browser fragments, keyed fully
            qualified. A missing or failed screenshot compares nothing.
        viewport: The configured CSS viewport ``(width, height)``.
        screenshot_format: The configured ``screenshot_format``.
        device_scale_factor: The replayability tier's DPR; ``0`` is the
            host's (``none``), unknown from configuration.
    """
    shot = recorded.get(SCREENSHOT_KEY)
    if not isinstance(shot, ScreenshotObs):
        return CaptureComparison()
    mismatches: list[str] = []
    unchecked: list[str] = []
    cursor = shot.cursor
    if (cursor is not None) != screenshot_cursor:
        mismatches.append(
            f"{SCREENSHOT_KEY}: screenshot_cursor {screenshot_cursor} configured, "
            f"{cursor is not None} recorded"
        )
    elif cursor is not None:
        if cursor.size_css_px != screenshot_cursor_size:
            mismatches.append(
                f"{SCREENSHOT_KEY}: screenshot_cursor_size {screenshot_cursor_size} configured, "
                f"{cursor.size_css_px} recorded"
            )
        if screenshot_format == "jpeg" and cursor.image_quality != screenshot_quality:
            mismatches.append(
                f"{SCREENSHOT_KEY}: cursor image quality {screenshot_quality} configured, "
                f"{cursor.image_quality} recorded"
            )
    width, height = viewport
    recorded_viewport = (shot.viewport_width, shot.viewport_height)
    if recorded_viewport != (width, height):
        mismatches.append(
            f"{SCREENSHOT_KEY}: viewport {width}x{height} configured, "
            f"{recorded_viewport[0]}x{recorded_viewport[1]} recorded"
        )

    media_type = image_media_type(shot.image_bytes) if shot.image_bytes else None
    expected_media = _FORMAT_MEDIA_TYPES.get(screenshot_format)
    if media_type is None:
        unchecked.append(f"{SCREENSHOT_KEY}: format (the recorded image's bytes are not readable)")
    elif media_type != expected_media:
        mismatches.append(
            f"{SCREENSHOT_KEY}: screenshot_format {screenshot_format} configured "
            f"({expected_media}), {media_type} recorded"
        )

    if device_scale_factor <= 0:
        unchecked.append(
            f"{SCREENSHOT_KEY}: device scale factor and image size (replayability none "
            f"keeps the host's DPR, which configuration does not know)"
        )
        return CaptureComparison(mismatches=tuple(mismatches), unchecked=tuple(unchecked))
    if shot.viewport_width <= 0 or shot.image_width <= 0 or shot.image_height <= 0:
        unchecked.append(
            f"{SCREENSHOT_KEY}: device scale factor and image size (the recorded sizes are unknown)"
        )
        return CaptureComparison(mismatches=tuple(mismatches), unchecked=tuple(unchecked))
    recorded_dpr = shot.image_width / shot.viewport_width
    if abs(recorded_dpr - device_scale_factor) > _DPR_TOLERANCE:
        mismatches.append(
            f"{SCREENSHOT_KEY}: device scale factor {device_scale_factor:g} configured, "
            f"{recorded_dpr:g} recorded"
        )
    implied = (round(width * device_scale_factor), round(height * device_scale_factor))
    if (
        abs(implied[0] - shot.image_width) > _SIZE_TOLERANCE_PX
        or abs(implied[1] - shot.image_height) > _SIZE_TOLERANCE_PX
    ):
        mismatches.append(
            f"{SCREENSHOT_KEY}: image size {implied[0]}x{implied[1]} implied by the "
            f"configuration, {shot.image_width}x{shot.image_height} recorded"
        )
    return CaptureComparison(mismatches=tuple(mismatches), unchecked=tuple(unchecked))


__all__ = ["SCREENSHOT_KEY", "compare_capture"]
