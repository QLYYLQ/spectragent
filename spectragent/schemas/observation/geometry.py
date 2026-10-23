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
the grid. :class:`PointFrame` is the frame one step's points are written in,
recorded on the step (``StepRecord.frame``).

The conversion is the framework's, never an agent's
(``spectragent/core/loops/coordinate_frames.py``, ``docs/architecture_design.md``
§4.1 "坐标系归框架"): it converts a parsed reply's points once, so the guard,
the action and the params recorded in the trace only ever see viewport
units; and it writes every point shown to a model back on that model's grid.

The other direction, :meth:`PointGrid.to_grid`, writes a point given in
viewport units — on the feedback a model reads, and in the reply the
training export writes for a student model (arch §4.15). It is not an exact
inverse when the grid is coarser than the viewport: :attr:`PointGrid.quantum`
bounds what one ``to_grid`` → ``to_viewport`` round trip can move a point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

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

    def to_grid(self, x: float, y: float) -> tuple[int, int]:
        """A viewport point, as the grid point that writes it.

        Scaled by ``grid / viewport`` and rounded, clamped onto
        ``[0, width] x [0, height]`` (the grid's far edge is a valid point to
        write, unlike the viewport's). The inverse of :meth:`to_viewport` up
        to :attr:`quantum`: ``to_viewport(*to_grid(x, y))`` is within
        ``quantum`` of ``(x, y)`` on each axis for every integer point of the
        viewport, and ``to_grid`` of that point is the same grid point again,
        so the written form is a fixed point.
        """
        gx = round(x * self.width / self.viewport_width)
        gy = round(y * self.height / self.viewport_height)
        return (max(0, min(gx, self.width)), max(0, min(gy, self.height)))

    @property
    def quantum(self) -> int:
        """The most one ``to_grid`` → ``to_viewport`` trip can move a viewport point.

        In viewport units, the larger of the two axes'. An axis whose grid is
        at least as fine as the viewport (``viewport <= grid``) loses nothing:
        0. A coarser one rounds twice, each time by at most half a step, so
        the error is at most ``floor(viewport / grid / 2 + 1/2)``: 1 for a
        0-1000 grid over a 1280, 1366 or 1920 pixel wide viewport, where
        280, 366 and 920 of the viewport's x values do not come back as
        themselves.
        """
        return max(
            _axis_quantum(self.viewport_width, self.width),
            _axis_quantum(self.viewport_height, self.height),
        )

    def holds(self, x: float, y: float) -> bool:
        """Whether the viewport point (x, y) lies in the area the screenshot depicts.

        ``to_grid`` clamps a point outside it onto the grid's edge, which
        would misstate where the point is; a writer asks this first.
        """
        return 0 <= x < self.viewport_width and 0 <= y < self.viewport_height


class PointFrame(BaseModel):
    """The frame one step's points were written in: a model's grid over that step's screenshot.

    Recorded on the step (``StepRecord.frame``) and handed to the Agent that
    builds the step's prompt (``ReactPromptContext.frame``), so every later
    reader — the next step's feedback, a branch's restored history, the
    training export, a trace reader — knows how the step's reply relates to
    its actions without asking the Agent. ``None`` in those places, not a
    frame, means the run's points are the capability's own units: its agent
    declares no grid, or its action space holds no point action.

    Attributes:
        space: The grid the run's model writes points on — its agent's
            declared ``coordinate_space``.
        grid: That grid over the screenshot the step showed. ``None`` when
            the step showed no screenshot of known size: no point of that
            step has a place, so one in a reply is refused and one shown to
            the model is said to be not placeable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    space: CoordinateSpace
    grid: PointGrid | None = None

    @model_validator(mode="after")
    def _grid_is_of_the_space(self) -> Self:
        if self.grid is not None and self.grid.space != self.space:
            raise ValueError(
                f"a {self.space} frame cannot hold a {self.grid.space} grid; "
                f"the grid is the declared space laid over the screenshot"
            )
        return self


def _axis_quantum(viewport: int, grid: int) -> int:
    """``floor(viewport / grid / 2 + 1/2)`` in integers, or 0 when the grid is no coarser."""
    if viewport <= grid:
        return 0
    return (viewport + grid) // (2 * grid)


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
