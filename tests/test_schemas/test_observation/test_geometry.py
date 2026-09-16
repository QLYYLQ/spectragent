"""Screenshot geometry: the one mapping from a model's point to a point action.

Two vision agents take points off screenshots (browser_use's
``click_coordinate`` and UI-TARS), and a desktop or Android screenshot will
be read the same way. What a fragment has to report, which grids a model may
write on, and what happens to a point off the grid are therefore framework
questions with one answer, pinned here:

- a fragment is a :class:`ScreenshotGeometry` by carrying four sizes, and
  ``browser.screenshot`` does;
- a size the chosen grid needs but the fragment reports as ``0`` yields no
  grid, so a caller refuses the point instead of assuming DPR 1;
- conversion scales by ``viewport / grid``, lands the far edge on the last
  viewport pixel, and clamps a point off the grid onto the viewport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

import pytest

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.schemas.observation.geometry import (
    CoordinateSpace,
    PointGrid,
    ScreenshotGeometry,
    point_grid,
)


@dataclass
class _Sizes:
    image_width: int = 0
    image_height: int = 0
    viewport_width: int = 0
    viewport_height: int = 0


def _grid(space: CoordinateSpace, image: tuple[int, int], viewport: tuple[int, int]) -> PointGrid:
    grid = point_grid(space, _Sizes(*image, *viewport))
    assert grid is not None
    return grid


def test_the_two_grids_a_model_may_write_on() -> None:
    assert set(get_args(CoordinateSpace)) == {"screenshot_pixels", "normalized_1000"}


class TestWhatAFragmentReports:
    def test_the_browser_screenshot_is_a_screenshot_geometry(self) -> None:
        assert isinstance(ScreenshotObs(), ScreenshotGeometry)

    def test_any_object_carrying_the_four_sizes_is_one(self) -> None:
        """Structural: a desktop fragment complies without importing this module."""
        assert isinstance(_Sizes(), ScreenshotGeometry)

    def test_a_fragment_missing_a_size_is_not(self) -> None:
        @dataclass
        class _NoViewport:
            image_width: int = 0
            image_height: int = 0

        assert not isinstance(_NoViewport(), ScreenshotGeometry)


class TestScreenshotPixels:
    def test_dpr_one_is_the_identity(self) -> None:
        grid = _grid("screenshot_pixels", (1280, 720), (1280, 720))
        assert grid.to_viewport(61, 377) == (61, 377)

    def test_dpr_two_halves_a_point(self) -> None:
        grid = _grid("screenshot_pixels", (2560, 1440), (1280, 720))
        assert (grid.width, grid.height) == (2560, 1440)
        assert grid.to_viewport(1199, 747) == (600, 374)

    def test_the_far_edge_lands_on_the_last_viewport_pixel(self) -> None:
        grid = _grid("screenshot_pixels", (2560, 1440), (1280, 720))
        assert grid.to_viewport(2560, 1440) == (1279, 719)

    @pytest.mark.parametrize(
        ("image", "viewport"),
        [((0, 720), (1280, 720)), ((1280, 0), (1280, 720)), ((1280, 720), (0, 720))],
    )
    def test_an_unknown_size_is_no_grid_not_a_dpr_one_guess(
        self, image: tuple[int, int], viewport: tuple[int, int]
    ) -> None:
        assert point_grid("screenshot_pixels", _Sizes(*image, *viewport)) is None


class TestNormalized1000:
    def test_the_grid_maps_onto_the_viewport_whatever_the_image_size(self) -> None:
        grid = _grid("normalized_1000", (2560, 1440), (1280, 720))
        assert (grid.width, grid.height) == (1000, 1000)
        assert grid.to_viewport(500, 500) == (640, 360)
        assert grid.to_viewport(1000, 1000) == (1279, 719)

    def test_only_the_viewport_size_is_needed(self) -> None:
        assert _grid("normalized_1000", (0, 0), (1000, 800)).to_viewport(250, 500) == (250, 400)
        assert point_grid("normalized_1000", _Sizes(1000, 800, 0, 0)) is None


class TestOffTheGrid:
    """Clamped onto the viewport — the rule browser_use's coordinate click shipped with."""

    def test_past_the_far_edge_clamps_to_the_last_pixel(self) -> None:
        grid = _grid("screenshot_pixels", (2560, 1440), (1280, 720))
        assert grid.to_viewport(2600, 300) == (1279, 150)
        assert grid.to_viewport(300, 1500) == (150, 719)

    def test_before_the_origin_clamps_to_zero(self) -> None:
        grid = _grid("screenshot_pixels", (1280, 720), (1280, 720))
        assert grid.to_viewport(-1, -40) == (0, 0)

    def test_a_ui_tars_1_5_answer_on_its_resized_input_stays_on_screen(self) -> None:
        """Qwen2.5-VL resizes 1280x720 to 1288x728; its bottom-right corner is a real answer."""
        grid = _grid("screenshot_pixels", (1280, 720), (1280, 720))
        assert grid.to_viewport(1287, 727) == (1279, 719)

    def test_a_normalized_answer_past_1000_clamps_too(self) -> None:
        grid = _grid("normalized_1000", (2560, 1440), (1280, 720))
        assert grid.to_viewport(1500, 300) == (1279, 216)
