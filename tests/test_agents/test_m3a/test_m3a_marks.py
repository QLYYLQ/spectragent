"""M3A's set-of-marks and its summary label, drawn where AndroidWorld draws them (``agents/m3a/marks.py``).

AndroidWorld draws with OpenCV; this draws with Pillow, so the places, sizes
and colours are pinned here, not the text's pixels: a green box over an
element's bounds, a white patch inside its top-left corner holding its number,
and for the label a white patch in the bottom-right corner. An element's
bounds are in the screen's pixels; a ``screenshot_pixels`` grid puts them on
the image, so a screenshot smaller than the screen gets its marks scaled.
"""

from __future__ import annotations

import io

from PIL import Image

from spectragent.agents.m3a.marks import jpeg, label, mark_elements, open_image
from spectragent.core.capabilities.android.device.forest import ProtoRect
from spectragent.schemas.observation.geometry import PointGrid

_GREEN, _WHITE, _BLACK = (0, 255, 0), (255, 255, 255), (0, 0, 0)


def _grid(image: tuple[int, int], screen: tuple[int, int] = (1080, 2400)) -> PointGrid:
    return PointGrid("screenshot_pixels", *image, *screen)


def test_an_element_gets_a_green_box_and_its_number_on_a_white_patch_in_its_corner() -> None:
    screen = Image.new("RGB", (1080, 2400), _BLACK)
    box = ProtoRect(left=100, top=200, right=500, bottom=400)

    marked = mark_elements(screen, [(7, box)], _grid((1080, 2400)))

    assert marked.getpixel((100, 300)) == _GREEN, "the box's left edge"
    assert marked.getpixel((499, 300)) == _GREEN, "its right edge"
    assert marked.getpixel((300, 300)) == _BLACK, "inside, the screen as it was"
    # The patch: AndroidWorld's rows top+1..top+24 and columns left+1..left+34.
    assert marked.getpixel((132, 222)) == _WHITE
    assert marked.getpixel((136, 222)) == _BLACK, "past the patch"
    patch = marked.crop((101, 201, 135, 225))
    assert _BLACK in {colour for _count, colour in patch.getcolors(1 << 16) or []}, "the number"
    assert screen.getpixel((100, 300)) == _BLACK, "the screenshot itself is left as it was"


def test_marks_follow_a_screenshot_smaller_than_the_screen() -> None:
    """At half the screen's size the box lands at half its bounds, and the patch shrinks too."""
    screen = Image.new("RGB", (540, 1200), _BLACK)
    box = ProtoRect(left=100, top=200, right=500, bottom=400)

    marked = mark_elements(screen, [(3, box)], _grid((540, 1200)))

    assert marked.getpixel((50, 150)) == _GREEN
    assert marked.getpixel((100, 150)) == _BLACK, "the screen's x=100 is past the box's edge here"
    assert marked.getpixel((66, 111)) == _WHITE, "the patch is 17 x 12 at this scale"
    assert marked.getpixel((70, 111)) == _BLACK


def test_the_label_is_a_white_patch_in_the_bottom_right_corner_with_the_word() -> None:
    screen = Image.new("RGB", (1080, 2400), _BLACK)

    labelled = label(screen, "after")

    assert labelled.getpixel((1075, 2395)) == _WHITE
    assert labelled.getpixel((931, 2371)) == _WHITE, "its top-left corner, 150 x 30"
    assert labelled.getpixel((929, 2371)) == _BLACK
    word = labelled.crop((960, 2370, 1080, 2400))
    assert _BLACK in {colour for _count, colour in word.getcolors(1 << 16) or []}


def test_images_go_to_the_model_as_jpeg_and_screenshots_are_read_in_rgb() -> None:
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 80), (10, 20, 30, 255)).save(buffer, format="PNG")

    image = open_image(buffer.getvalue())

    assert image.mode == "RGB" and image.size == (40, 80)
    assert Image.open(io.BytesIO(jpeg(image))).format == "JPEG"
