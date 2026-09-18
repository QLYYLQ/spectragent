"""Placing a frame's contents on the page, from its owner's box model alone.

The invariant every case here checks is the same one: the frame's own
viewport rectangle — ``(0, 0)`` to ``(viewport_w, viewport_h)`` in frame
coordinates — must land exactly on the corners of the content quad the
owner's renderer reported. That is what makes a click inside the frame land
where Chrome draws it, whatever transform is between them.

``REAL_CHROME_CROSS_FRAME`` is a verbatim ``DOM.getBoxModel`` answer from
Chrome 152 for a 320x180 cross-origin iframe (border 4, padding 6) under
``transform: scale(0.8) rotate(3deg) translate(20px, 10px)``. It is here so
the assumption the whole mapping rests on — that ``width`` / ``height`` are
the *layout* border box, which no transform touches, while the quads are
transformed — is pinned against something a browser actually said.
"""

from __future__ import annotations

import math

import pytest

from spectragent.core.capabilities.browser.geometry import (
    FrameGeometryError,
    FrameProjection,
    bounding_box,
    corners,
)

REAL_CHROME_CROSS_FRAME = {
    "model": {
        "content": [
            286.2311706542969,
            71.02269744873047,
            541.8803100585938,
            84.42070007324219,
            534.3439331054688,
            228.22335815429688,
            278.6947937011719,
            214.82534790039062,
        ],
        "border": [
            278.66082763671875,
            62.61497497558594,
            550.2880859375,
            76.85035705566406,
            541.914306640625,
            236.63108825683594,
            270.2870788574219,
            222.3957061767578,
        ],
        "width": 340,
        "height": 200,
    }
}


def rect_model(
    x: float, y: float, content_w: float, content_h: float, edge: float
) -> dict[str, object]:
    """An untransformed owner: content box inset by ``edge`` in its border box."""
    return {
        "model": {
            "content": [x, y, x + content_w, y, x + content_w, y + content_h, x, y + content_h],
            "border": [
                x - edge,
                y - edge,
                x + content_w + edge,
                y - edge,
                x + content_w + edge,
                y + content_h + edge,
                x - edge,
                y + content_h + edge,
            ],
            "width": content_w + 2 * edge,
            "height": content_h + 2 * edge,
        }
    }


def linear_model(
    origin: tuple[float, float],
    ex: tuple[float, float],
    ey: tuple[float, float],
    *,
    content: tuple[float, float] = (320.0, 180.0),
    border: tuple[float, float] = (340.0, 200.0),
) -> dict[str, object]:
    """An owner whose transform maps one CSS pixel to ``ex`` / ``ey``.

    The border quad is placed at the same origin: the projection reads only
    its edge *lengths*, which is how it recovers the scale.
    """

    def quad(width: float, height: float) -> list[float]:
        points = [
            (0.0, 0.0),
            (width, 0.0),
            (width, height),
            (0.0, height),
        ]
        return [
            value
            for x, y in points
            for value in (
                origin[0] + x * ex[0] + y * ey[0],
                origin[1] + x * ex[1] + y * ey[1],
            )
        ]

    return {
        "model": {
            "content": quad(*content),
            "border": quad(*border),
            "width": border[0],
            "height": border[1],
        }
    }


class TestTheFramesViewportLandsOnItsContentQuad:
    @pytest.mark.parametrize(
        ("ex", "ey"),
        [
            ((1.0, 0.0), (0.0, 1.0)),  # untransformed
            ((0.5, 0.0), (0.0, 0.5)),  # scale(.5)
            ((0.5, 0.0), (0.0, 2.0)),  # scale(.5, 2)
            ((0.0, 0.5), (-0.5, 0.0)),  # rotate(90deg) scale(.5)
            ((0.9, 0.3), (-0.3, 0.9)),  # rotate + scale
            ((1.0, 0.0), (0.4, 1.0)),  # skewX
        ],
    )
    def test_every_corner(self, ex: tuple[float, float], ey: tuple[float, float]) -> None:
        model = linear_model((400.0, 250.0), ex, ey)
        projection = FrameProjection.of_box_model(model)
        content = model["model"]["content"]  # type: ignore[index]

        for (x, y), expected in zip(
            [(0.0, 0.0), (320.0, 0.0), (320.0, 180.0), (0.0, 180.0)],
            [tuple(content[i : i + 2]) for i in (0, 2, 4, 6)],  # type: ignore[index]
            strict=True,
        ):
            got = projection.point(x, y)
            assert math.isclose(got[0], expected[0], abs_tol=1e-6)
            assert math.isclose(got[1], expected[1], abs_tol=1e-6)

    def test_a_real_chrome_answer(self) -> None:
        """The recorded 320x180 frame comes back as 320x180, transform and all."""
        projection = FrameProjection.of_box_model(REAL_CHROME_CROSS_FRAME)
        content = REAL_CHROME_CROSS_FRAME["model"]["content"]

        assert projection.point(0.0, 0.0) == pytest.approx(content[0:2], abs=1e-6)
        assert projection.point(320.0, 0.0) == pytest.approx(content[2:4], abs=1e-4)
        assert projection.point(320.0, 180.0) == pytest.approx(content[4:6], abs=1e-4)
        assert projection.point(0.0, 180.0) == pytest.approx(content[6:8], abs=1e-4)
        # scale(0.8): one frame pixel is 0.8 page pixels, rotated 3 degrees.
        assert math.hypot(*projection.ex) == pytest.approx(0.8, abs=1e-6)
        assert math.degrees(math.atan2(projection.ex[1], projection.ex[0])) == pytest.approx(
            3.0, abs=1e-3
        )

    def test_a_scrollbar_does_not_shrink_the_mapping(self) -> None:
        """The frame's coordinates start at the content box, scrollbar included.

        ``Page.getLayoutMetrics`` would answer 305 for a frame whose classic
        scrollbar takes 15px, and a mapping built on that would stretch every
        point by 320/305. The box model has no such notion.
        """
        projection = FrameProjection.of_box_model(rect_model(0.0, 0.0, 320.0, 180.0, 10.0))
        assert projection.point(320.0, 0.0) == (320.0, 0.0)


class TestWhatCannotBeMapped:
    def test_a_perspective_transform_is_refused_by_name(self) -> None:
        model = linear_model((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
        content = model["model"]["content"]  # type: ignore[index]
        content[4] += 40.0  # bottom-right alone moves: no longer a parallelogram

        with pytest.raises(FrameGeometryError, match="perspective transform"):
            FrameProjection.of_box_model(model)

    def test_rounding_is_not_mistaken_for_perspective(self) -> None:
        model = linear_model((0.0, 0.0), (0.9, 0.3), (-0.3, 0.9))
        content = model["model"]["content"]  # type: ignore[index]
        content[4] += 0.4

        assert FrameProjection.of_box_model(model).point(0.0, 0.0) == (0.0, 0.0)

    @pytest.mark.parametrize(
        "model",
        [
            None,
            {},
            {"model": {}},
            {"model": {"content": [1, 2, 3, 4], "border": [0] * 8, "width": 10, "height": 10}},
            # A frame with layout but no size: scaled to nothing, or display:none.
            {"model": {"content": [0.0] * 8, "border": [0.0] * 8, "width": 0, "height": 0}},
            {"model": {"content": [0.0] * 8, "border": [0.0] * 8, "width": 340, "height": 200}},
        ],
        ids=["none", "empty", "no-quads", "short-quad", "no-size", "degenerate"],
    )
    def test_a_frame_with_no_usable_box_says_so(self, model: dict[str, object] | None) -> None:
        with pytest.raises(FrameGeometryError, match="observe the page again"):
            FrameProjection.of_box_model(model)


class TestBoxes:
    def test_corners_and_bounding_box_round_trip(self) -> None:
        assert bounding_box(corners((10.0, 20.0, 30.0, 40.0))) == (10.0, 20.0, 30.0, 40.0)

    def test_a_rotated_box_keeps_its_centre_and_grows(self) -> None:
        projection = FrameProjection.of_box_model(
            linear_model((0.0, 0.0), (0.0, 1.0), (-1.0, 0.0))  # rotate(90deg)
        )
        x, y, width, height = bounding_box(projection.quad(corners((10.0, 20.0, 40.0, 20.0))))

        centre = projection.point(30.0, 30.0)  # the box's own centre, mapped
        assert (x + width / 2, y + height / 2) == centre
        assert (width, height) == (20.0, 40.0)
