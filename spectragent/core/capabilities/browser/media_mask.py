"""A screenshot's media areas, and masking them out of an image comparison.

A browser screenshot carries the media facts read around its capture
(``ScreenshotObs.media``, ``media.py``): the box of every ``<video>`` /
``<audio>`` element in the top viewport, and of every embedded frame whose
own media read did not answer (what such a frame plays is unknown).
:func:`screenshot_media_boxes` lists those boxes, clipped to the viewport;
:func:`mask_image` paints boxes black on an image, scaled from the viewport
to the image's pixels, and says what share of the image they cover. On a
page a media-area rule declares (``replay/media_areas.py``), masking both
images the same way leaves a player that plays, shows an error or is missing
out of their comparison, while every pixel outside it still counts. Past
:data:`MAX_MASKED_SHARE` the rest of the image says too little about the
page, and the whole images are compared instead.

Restoration verification masks the union of the recorded and the restored
screenshot's boxes before their difference hash
(``restoration.py::media_area_measure``, ``docs/architecture_design.md``
§4.14 "页面上的媒体区域"). A reader of a recording that compares one
screenshot with later images of the same page masks that screenshot's boxes
out of both (the repository's ``lookahead stability`` review). Both read
these functions and this limit; neither keeps a copy.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final, Protocol

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from spectragent.core.capabilities.browser.media import MediaRect
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.schemas.restoration import MaskedAreaKind

MAX_MASKED_SHARE: Final = 0.75
"""The largest share of a screenshot a media-area mask may cover and still be compared.

The difference hash reads the image as a 9x8 grid of averages, one bit per
horizontally adjacent pair: 64 bits. Masked pixels are the same on both
sides, so with three quarters of the image masked only about 16 bits still
read the page, and even a wholly different rest of the page flips about half
of them — barely past the restorer's default ``screenshot_max_distance`` of
5 — while a smaller difference cannot fail at all. Past this share the
masked distance would pass by construction, so the mask is not applied: the
screenshot distance is the whole images' one, as on an undeclared page, and
the evidence says why. Measured on the 2026-10-23 forks: Tencent Video's
home and TV channel pages hold a frame the media read cannot reach that
covers 100% and 87% of the viewport, and the channel page that failed to
load offline would otherwise have passed. Every comparison masked with
these boxes applies the same limit.
"""


class MediaBox(BaseModel):
    """One box a screenshot's media facts measured.

    ``kind``: a media element or an embedded frame the media read could not
    reach; ``element``: the media element's name (the browser's
    ``<frame>#<order> <tag>``) or the frame's path. The box is in CSS pixels
    of the top viewport, clipped to it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MaskedAreaKind
    element: str
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class ViewportBox(Protocol):
    """A box in CSS pixels of the top viewport: what :func:`mask_image` paints."""

    @property
    def x(self) -> float: ...

    @property
    def y(self) -> float: ...

    @property
    def width(self) -> float: ...

    @property
    def height(self) -> float: ...


def _clipped(rect: MediaRect, width: int, height: int) -> tuple[float, float, float, float] | None:
    """*rect* clipped to the viewport (an unknown size, 0, does not clip), or ``None`` when empty."""
    x0, y0 = max(rect.x, 0.0), max(rect.y, 0.0)
    x1, y1 = rect.x + rect.width, rect.y + rect.height
    if width > 0:
        x1 = min(x1, float(width))
    if height > 0:
        y1 = min(y1, float(height))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def screenshot_media_boxes(shot: ScreenshotObs, whose: str) -> tuple[list[MediaBox], str | None]:
    """The boxes *shot*'s media facts measured, clipped to its viewport, and what they cannot show.

    Every media element's box and every unreachable frame's box (what such
    a frame plays is unknown); a box wholly outside the viewport is left
    out. A screenshot without media state, or with a state neither read
    measured, has no boxes, and the note says so; so it does when the page
    had more media elements than the facts keep. *whose* names the
    screenshot in the note (``recorded``, ``restored``, ``observation``).
    """
    state = shot.media
    if state is None:
        return [], f"the {whose} screenshot carries no media state, so its media boxes are unknown"
    if not state.known:
        return [], f"the {whose} media state is unknown ({state.detail or 'no read answered'})"
    boxes: list[MediaBox] = []

    def add(kind: MaskedAreaKind, name: str, rect: MediaRect) -> None:
        clipped = _clipped(rect, shot.viewport_width, shot.viewport_height)
        if clipped is not None:
            x, y, width, height = clipped
            boxes.append(MediaBox(kind=kind, element=name, x=x, y=y, width=width, height=height))

    for media in state.elements:
        add("media_element", media.label, media.rect)
    for unreachable in state.unreachable_frames:
        add("unreachable_frame", unreachable.frame, unreachable.rect)
    note = (
        f"the {whose} page has more media elements than its media facts keep: "
        "only the boxes kept are masked"
        if state.truncated
        else None
    )
    return boxes, note


def image_box(
    box: ViewportBox, image_size: tuple[int, int], viewport: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    """*box* in pixels of an image of *image_size* that shows a *viewport* of CSS pixels.

    Every pixel the box touches, so a fractional edge is included:
    ``(x0, y0, x1, y1)``, right and bottom exclusive, clipped to the image;
    ``None`` when nothing is left. A viewport size of 0 is unknown and
    scales by 1.
    """
    width, height = image_size
    scale_x = width / viewport[0] if viewport[0] > 0 else 1.0
    scale_y = height / viewport[1] if viewport[1] > 0 else 1.0
    x0 = max(math.floor(box.x * scale_x), 0)
    y0 = max(math.floor(box.y * scale_y), 0)
    x1 = min(math.ceil((box.x + box.width) * scale_x), width)
    y1 = min(math.ceil((box.y + box.height) * scale_y), height)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def mask_image(
    image: Image.Image, boxes: Sequence[ViewportBox], viewport: tuple[int, int]
) -> tuple[Image.Image, float]:
    """A copy of *image* with *boxes* painted black, and the share of its pixels they cover.

    Each box is scaled from the *viewport* (CSS pixels) to the image's own
    pixels by :func:`image_box`. Any image mode; the input is not changed.
    """
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    for box in boxes:
        pixels = image_box(box, image.size, viewport)
        if pixels is not None:
            x0, y0, x1, y1 = pixels
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    masked = image.copy()
    masked.paste(0, mask=mask)
    return masked, mask.histogram()[255] / (image.width * image.height)


__all__ = [
    "MAX_MASKED_SHARE",
    "MediaBox",
    "ViewportBox",
    "image_box",
    "mask_image",
    "screenshot_media_boxes",
]
