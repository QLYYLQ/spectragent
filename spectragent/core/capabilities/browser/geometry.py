"""Placing an out-of-process frame's contents on the page.

CDP answers geometry questions in the coordinates of the *local root* of
the node asked about: the page's own viewport for the tab's document and
its same-process iframes, and the frame's own viewport for a frame Chrome
runs in another renderer (an OOPIF). Blink composes CSS transforms along
the way — ``DOM.getContentQuads`` for an element inside a rotated
same-process iframe already comes back as a rotated quad in page
coordinates — but it stops at a process boundary, because the other
renderer knows nothing about where its frame is drawn.

So each process boundary has to be crossed here, and the only thing that
describes it is the ``<iframe>`` element's own box model, measured in the
owner's renderer. That box model is enough to reconstruct the whole map
and not just the offset:

* ``content`` is the frame's viewport rectangle *after* every transform
  around it — four points, so a scaled / rotated / skewed frame comes
  back as a parallelogram rather than a rectangle;
* ``width`` / ``height`` are the border box's layout size, which no
  transform touches.

The two together give the scale, because a transform stretches the border
box and the content box by the same factor along each axis. Dividing the
content edge by the frame's own viewport size yields the page vector of
one CSS pixel of the frame — :class:`FrameProjection`. A frame-local
point then lands on the page exactly, whatever the transform:

    P(u, v) = content_top_left + u * ex + v * ey

Taking the content box's *bounding* box instead — the mapping this
replaced — is right only while ``ex`` and ``ey`` are the unit axes, i.e.
for a pure translation. Under ``transform: scale(.8) rotate(3deg)`` a
click on a control near the far corner of a 320x180 frame missed it by
about 50 px, and reported success, because the press landed on whatever
the page draws there.

Deriving the frame's viewport size this way rather than asking the frame
(``Page.getLayoutMetrics``) is deliberate: that call reports the *layout*
viewport, which a classic scrollbar shrinks, while the frame's
coordinates still start at the content box's edge — the scrollbar is
drawn inside it. It also costs a round trip into the other renderer,
which is the process this module is trying not to wait on.

A perspective transform is the one case this cannot express: it maps the
content box to a general quadrilateral, not a parallelogram, and no
affine map reproduces it. :meth:`FrameProjection.of_box_model` raises
:class:`FrameGeometryError` there rather than returning a point that is
quietly wrong.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import hypot
from typing import Any

#: ``(x, y)`` in some viewport, CSS pixels.
Point = tuple[float, float]

#: ``(x, y, width, height)``, axis-aligned, CSS pixels.
Box = tuple[float, float, float, float]

#: How far the content quad may miss being a parallelogram before the
#: transform is called uninvertible. Chrome's quads are exact to well
#: under a pixel for any affine transform; a perspective one is off by
#: many. Scaled by the quad's own size so a very large frame is not
#: rejected by float rounding alone.
_PARALLELOGRAM_TOLERANCE_PX = 1.0
_PARALLELOGRAM_TOLERANCE_REL = 1e-4


class FrameGeometryError(RuntimeError):
    """A frame's contents cannot be placed on the page.

    Carries the sentence an action reports to the agent: the caller has
    no better description of the problem than this module does.
    """


@dataclass(frozen=True, slots=True)
class FrameProjection:
    """The affine map from a frame's own viewport into its owner's.

    ``ex`` / ``ey`` are where one CSS pixel of the frame's x and y axes
    points in the owner's coordinates — ``(1, 0)`` and ``(0, 1)`` for an
    untransformed frame, rotated and scaled otherwise.
    """

    origin: Point
    ex: Point
    ey: Point

    @classmethod
    def of_box_model(cls, model: dict[str, Any] | None) -> FrameProjection:
        """Build the projection from a frame owner's ``DOM.getBoxModel``.

        Raises :class:`FrameGeometryError` when the answer does not
        describe a frame that can be drawn into: a missing or degenerate
        box (a frame with no layout, or one scaled to nothing), or a
        perspective transform, which is not affine.
        """
        box: dict[str, Any] = (model or {}).get("model") or {}
        content = _quad(box.get("content"))
        border = _quad(box.get("border"))
        width = _positive(box.get("width"))
        height = _positive(box.get("height"))
        if content is None or border is None or width is None or height is None:
            raise FrameGeometryError(_NO_BOX)

        top_left, top_right, bottom_right, bottom_left = content
        across = _minus(top_right, top_left)
        down = _minus(bottom_left, top_left)
        _require_parallelogram(top_left, top_right, bottom_right, bottom_left)

        # The border box is the same rectangle scaled by the same
        # transform, and its layout size is known — so the content box's
        # own layout size, which is the frame's viewport, follows.
        border_across = hypot(*_minus(border[1], border[0]))
        border_down = hypot(*_minus(border[3], border[0]))
        if border_across <= 0.0 or border_down <= 0.0:
            raise FrameGeometryError(_NO_BOX)
        viewport_width = hypot(*across) * width / border_across
        viewport_height = hypot(*down) * height / border_down
        if viewport_width <= 0.0 or viewport_height <= 0.0:
            raise FrameGeometryError(_NO_BOX)

        return cls(
            origin=top_left,
            ex=(across[0] / viewport_width, across[1] / viewport_width),
            ey=(down[0] / viewport_height, down[1] / viewport_height),
        )

    def point(self, x: float, y: float) -> Point:
        """Where the frame-local point ``(x, y)`` lands in the owner."""
        return (
            self.origin[0] + x * self.ex[0] + y * self.ey[0],
            self.origin[1] + x * self.ex[1] + y * self.ey[1],
        )

    def quad(self, points: Sequence[Point]) -> list[Point]:
        """Every point of ``points``, mapped."""
        return [self.point(x, y) for x, y in points]


#: What an action reports when a frame cannot be measured at all.
_NO_BOX = (
    "The element is inside a frame whose box Chrome will not report, so it "
    "cannot be located; observe the page again before acting on it."
)

#: What an action reports for a frame under a perspective transform.
NON_AFFINE_FRAME = (
    "The element is inside a frame drawn under a perspective transform, "
    "which has no single page point this can compute; act on it by "
    "coordinates from a screenshot instead of by index."
)


def corners(box: Box) -> list[Point]:
    """The four corners of an axis-aligned box, clockwise from top-left."""
    x, y, width, height = box
    return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]


def bounding_box(points: Sequence[Point]) -> Box:
    """The axis-aligned box around ``points``.

    For an affine image of a rectangle this keeps the centre exact — the
    box around a parallelogram is centred on its centroid — while the
    width and height grow to cover a rotation. That is the right trade
    for both consumers: the click lands on the centre, and the trajectory
    layer only wants a target size.
    """
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _quad(raw: Any) -> tuple[Point, Point, Point, Point] | None:
    """A CDP ``Quad`` (8 numbers) as four points, or ``None``."""
    if not isinstance(raw, Sequence) or len(raw) < 8:
        return None
    try:
        values = [float(v) for v in raw[:8]]
    except (TypeError, ValueError):
        return None
    return (
        (values[0], values[1]),
        (values[2], values[3]),
        (values[4], values[5]),
        (values[6], values[7]),
    )


def _positive(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0.0 else None


def _minus(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _require_parallelogram(
    top_left: Point, top_right: Point, bottom_right: Point, bottom_left: Point
) -> None:
    """Raise unless the quad is an affine image of a rectangle."""
    expected_x = top_right[0] + bottom_left[0] - top_left[0]
    expected_y = top_right[1] + bottom_left[1] - top_left[1]
    skew = hypot(bottom_right[0] - expected_x, bottom_right[1] - expected_y)
    size = hypot(*_minus(top_right, top_left)) + hypot(*_minus(bottom_left, top_left))
    if skew > max(_PARALLELOGRAM_TOLERANCE_PX, _PARALLELOGRAM_TOLERANCE_REL * size):
        raise FrameGeometryError(NON_AFFINE_FRAME)
