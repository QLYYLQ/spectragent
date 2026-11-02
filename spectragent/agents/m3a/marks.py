"""Set-of-marks: M3A's numbered boxes on a screenshot, and the label its summary's screenshot carries.

Ported from AndroidWorld (``google-research/android_world`` at commit
``55ddaf5edc6f6c25673aed343907d5b9dca66f84``, Apache-2.0; ``THIRD_PARTY_NOTICES.md``):
``m3a_utils.add_ui_element_mark`` and ``add_screenshot_label``. Each element
gets a green box over its bounds, a white patch inside its top-left corner and
its number on the patch; a label is a white patch in the bottom-right corner
with a word on it. The places, sizes and colours are AndroidWorld's; it draws
with OpenCV and its Hershey font, this with Pillow and Pillow's default font,
sized so a digit stands as tall as OpenCV's, so the text pixels differ.

Where an element is on the image is the framework's to say (``docs/architecture_design.md``
§4.1 "坐标系归框架"): its bounds are in the capability's units (screen pixels on
Android), and :meth:`~spectragent.schemas.observation.geometry.PointGrid.to_grid`
of a ``screenshot_pixels`` grid over the screenshot puts each corner in the
image's pixels, as it writes any point a model is shown.
"""

from __future__ import annotations

import io
import math
from collections.abc import Iterable
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont

from spectragent.schemas.observation.geometry import PointGrid

__all__ = ["Bounds", "jpeg", "label", "mark_elements", "open_image"]

_GREEN = (0, 255, 0)
_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)
#: Pillow's default font, at this size per unit of OpenCV's font scale, draws a
#: digit as tall as ``FONT_HERSHEY_SIMPLEX`` does (22 px at scale 1, measured).
_FONT_PX_PER_SCALE = 31


class Bounds(Protocol):
    """An element's bounds in the capability's units: the screen's pixels on Android."""

    @property
    def left(self) -> int: ...
    @property
    def top(self) -> int: ...
    @property
    def right(self) -> int: ...
    @property
    def bottom(self) -> int: ...


def open_image(data: bytes) -> Image.Image:
    """The image *data* encodes, in RGB: a screenshot's alpha has nothing to say here."""
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")


def jpeg(image: Image.Image) -> bytes:
    """*image* as JPEG at Pillow's default quality, as AndroidWorld hands a model every image."""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def mark_elements(
    image: Image.Image, elements: Iterable[tuple[int, Bounds]], grid: PointGrid
) -> Image.Image:
    """A copy of *image* with each element's mark: its box, and its number in the box's corner.

    *grid* is a ``screenshot_pixels`` grid over the screenshot *image* is: it
    puts the bounds on the image, and its scale sizes the marks, as
    AndroidWorld's scale from the screen to the screenshot does.
    """
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    x_scale = grid.width / grid.viewport_width
    y_scale = grid.height / grid.viewport_height
    iso_scale = math.hypot(x_scale, y_scale)
    thickness = int(2 * iso_scale)
    font = _font(0.7 * iso_scale)
    for index, box in elements:
        left, top = grid.to_grid(box.left, box.top)
        right, bottom = grid.to_grid(box.right, box.bottom)
        draw.rectangle((left, top, right, bottom), outline=_GREEN, width=thickness)
        draw.rectangle(
            (
                left + int(1 * x_scale),
                top + int(1 * y_scale),
                left + int(35 * x_scale) - 1,
                top + int(25 * y_scale) - 1,
            ),
            fill=_WHITE,
        )
        draw.text(
            (left + int(1 * x_scale), top + int(20 * y_scale)),
            str(index),
            fill=_BLACK,
            font=font,
            anchor="ls",
        )
    return marked


def label(image: Image.Image, word: str) -> Image.Image:
    """A copy of *image* with *word* on a white patch in its bottom-right corner (``after``)."""
    labelled = image.copy()
    draw = ImageDraw.Draw(labelled)
    width, height = labelled.size
    draw.rectangle((width - 150, height - 30, width - 1, height - 1), fill=_WHITE)
    draw.text(
        (width - 120, height - 5),
        word,
        fill=_BLACK,
        font=_font(1.0),
        anchor="ls",
    )
    return labelled


def _font(scale: float) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Pillow's default font at OpenCV font scale *scale*."""
    return ImageFont.load_default(size=max(1, round(_FONT_PX_PER_SCALE * scale)))
