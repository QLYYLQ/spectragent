"""Screenshot geometry: where a point a model reads off an image is executed.

A vision agent shows a model an image and gets a point back. Two grids lie
between that point and the action that runs it, and neither has to be the
image's pixels:

- **The model's grid.** A model writes points in pixels of the image it was
  shown, or on a 0-1000 grid laid over that image whatever its size. Which
  one is a property of the model, declared in config
  (:data:`CoordinateSpace`) and never sniffed from a reply: ``(500, 300)``
  is valid on both.
- **The executor's grid.** A capability's point actions take points in
  their own units over the area the screenshot depicts, its *viewport*. For
  ``browser.*`` that is CSS pixels, while the capture is rendered at the
  device scale factor, so at DPR 2 the image is twice the viewport in each
  direction.

:class:`ScreenshotGeometry` is what a screenshot fragment reports so the two
can be related. :func:`point_grid` builds one step's mapping, and
:meth:`PointGrid.to_viewport` converts, with a single rule for a point off
the grid. An agent converts once, when it parses a reply, so the guard, the
action and the params recorded in the trace only ever see viewport units.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

CoordinateSpace = Literal["screenshot_pixels", "normalized_1000"]
"""The grid a model writes points on.

``screenshot_pixels``: pixels of the image the model was shown, origin at its
top-left corner. ``normalized_1000``: a 0-1000 grid over that image on both
axes, x first, whatever the image's pixel size.
"""


@runtime_checkable
class ScreenshotGeometry(Protocol):
    """The sizes a screenshot fragment reports, so a point on it can be executed.

    ``image_width`` x ``image_height`` is the pixel size of the image itself.
    ``viewport_width`` x ``viewport_height`` is the area the image depicts,
    in the units the providing capability's point actions take. ``0`` means
    the size is unknown.

    Structural: a fragment satisfies this by carrying the four fields
    (``browser.screenshot`` does), without importing anything from here. A
    desktop or mobile screenshot fragment has to carry them too, or no
    vision agent can place a point on it.
    """

    image_width: int
    image_height: int
    viewport_width: int
    viewport_height: int


@dataclass(frozen=True)
class PointGrid:
    """One step's mapping from the grid a model writes on to the viewport.

    Attributes:
        space: Which grid the points are written on.
        width: The grid's horizontal extent: the image's pixel width, or 1000.
        height: The grid's vertical extent: the image's pixel height, or 1000.
        viewport_width: The executor's horizontal extent.
        viewport_height: The executor's vertical extent.
    """

    space: CoordinateSpace
    width: int
    height: int
    viewport_width: int
    viewport_height: int

    def to_viewport(self, x: float, y: float) -> tuple[int, int]:
        """A point on the grid, as the viewport point that executes it.

        Scaled by ``viewport / grid`` and rounded; the far edge
        (``x == width``) lands on the last viewport pixel.

        A point off the grid is clamped onto the viewport, not refused. Models
        overshoot an edge without misreading anything: UI-TARS-1.5 answers on
        its resized input, whose sides are rounded to a multiple of 28 and so
        can be up to 14 pixels longer than the screenshot's. Refusing such a
        point would spend one of the Loop's consecutive parse failures on a
        correct answer. What the model wrote stays in the step's
        ``llm_output``, next to the clamped params.
        """
        vx = round(x * self.viewport_width / self.width)
        vy = round(y * self.viewport_height / self.height)
        return (
            max(0, min(vx, self.viewport_width - 1)),
            max(0, min(vy, self.viewport_height - 1)),
        )


def point_grid(space: CoordinateSpace, screenshot: ScreenshotGeometry) -> PointGrid | None:
    """The grid *space* lays over *screenshot*, or ``None`` if a size it needs is unknown.

    The viewport size is needed on every grid. The image size is needed only
    for ``screenshot_pixels``, because a 0-1000 grid is defined over whatever
    size the image has. ``None`` tells the caller to refuse a point; it must
    never assume device scale factor 1 instead.
    """
    viewport_w, viewport_h = screenshot.viewport_width, screenshot.viewport_height
    if viewport_w <= 0 or viewport_h <= 0:
        return None
    if space == "normalized_1000":
        return PointGrid(space, 1000, 1000, viewport_w, viewport_h)
    image_w, image_h = screenshot.image_width, screenshot.image_height
    if image_w <= 0 or image_h <= 0:
        return None
    return PointGrid(space, image_w, image_h, viewport_w, viewport_h)
