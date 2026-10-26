"""``media_mask``: a screenshot's media boxes, and painting them out of an image."""

from __future__ import annotations

from PIL import Image

from spectragent.core.capabilities.browser.media import (
    MediaElementState,
    MediaFrameState,
    MediaReading,
    MediaRect,
    MediaState,
)
from spectragent.core.capabilities.browser.media_mask import (
    MAX_MASKED_SHARE,
    MediaBox,
    image_box,
    mask_image,
    screenshot_media_boxes,
)
from spectragent.core.capabilities.browser.observations import ScreenshotObs


def _video(rect: MediaRect, order: int = 0) -> MediaElementState:
    reading = MediaReading(current_time=1.0, paused=False, ended=False)
    return MediaElementState(
        frame="main",
        order=order,
        tag="video",
        rect=rect,
        source_kind="url",
        before=reading,
        after=reading,
    )


def _shot(media: MediaState | None, viewport: tuple[int, int] = (1280, 720)) -> ScreenshotObs:
    return ScreenshotObs(viewport_width=viewport[0], viewport_height=viewport[1], media=media)


def _measured(**fields: object) -> MediaState:
    return MediaState.model_validate(
        {"read_before": "measured", "read_after": "measured", **fields}
    )


def test_every_element_and_unreachable_frame_box_clipped_to_the_viewport() -> None:
    state = _measured(
        elements=[
            _video(MediaRect(x=160, y=88, width=640, height=360)),
            _video(MediaRect(x=-40, y=600, width=200, height=200), order=1),
            _video(MediaRect(x=0, y=900, width=320, height=180), order=2),
        ],
        unreachable_frames=[
            MediaFrameState(frame="main/unplaced-0", rect=MediaRect(x=60, y=84, width=10, height=5))
        ],
    )
    boxes, note = screenshot_media_boxes(_shot(state), "observation")
    assert [(b.kind, b.element, b.x, b.y, b.width, b.height) for b in boxes] == [
        ("media_element", "main#0 video", 160, 88, 640, 360),
        ("media_element", "main#1 video", 0, 600, 160, 120),
        ("unreachable_frame", "main/unplaced-0", 60, 84, 10, 5),
    ], "a box wholly outside the viewport masks nothing and is left out"
    assert note is None


def test_without_measured_media_facts_there_are_no_boxes_and_the_note_names_the_screenshot() -> (
    None
):
    assert screenshot_media_boxes(_shot(None), "observation") == (
        [],
        "the observation screenshot carries no media state, so its media boxes are unknown",
    )
    unknown = MediaState(read_before="unknown", read_after="unknown", detail="timed out")
    assert screenshot_media_boxes(_shot(unknown), "restored") == (
        [],
        "the restored media state is unknown (timed out)",
    )
    truncated = _measured(
        elements=[_video(MediaRect(x=0, y=0, width=10, height=10))], truncated=True
    )
    boxes, note = screenshot_media_boxes(_shot(truncated), "recorded")
    assert len(boxes) == 1
    assert note is not None and note.startswith("the recorded page has more media elements")


def test_a_box_covers_every_pixel_it_touches_at_the_images_scale() -> None:
    box = MediaBox(kind="media_element", element="v", x=10.5, y=0, width=20, height=10.2)
    # a device-scale-2 image of a 100x50 viewport
    assert image_box(box, (200, 100), (100, 50)) == (21, 0, 61, 21)
    # an unknown viewport scales by 1; the image clips
    assert image_box(box, (25, 100), (0, 0)) == (10, 0, 25, 11)
    outside = MediaBox(kind="media_element", element="v", x=300, y=0, width=10, height=10)
    assert image_box(outside, (200, 100), (100, 50)) is None


def test_masking_paints_the_boxes_black_in_any_mode_and_reports_the_share() -> None:
    box = MediaBox(kind="media_element", element="v", x=0, y=0, width=50, height=25)
    for image in (Image.new("RGB", (200, 100), (200, 100, 50)), Image.new("L", (200, 100), 180)):
        masked, share = mask_image(image, [box], (100, 50))
        assert share == 0.25
        black = 0 if image.mode == "L" else (0, 0, 0)
        assert masked.getpixel((99, 49)) == black
        assert masked.getpixel((100, 50)) == image.getpixel((100, 50))
        assert image.getpixel((0, 0)) != black, "the input is not changed"
    _, nothing = mask_image(Image.new("RGB", (10, 10)), [], (10, 10))
    assert nothing == 0.0
    assert 0 < MAX_MASKED_SHARE < 1
