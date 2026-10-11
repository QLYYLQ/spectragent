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


#: (viewport width, viewport height) pairs the 0-1000 grid is enumerated over.
_NORMALIZED_VIEWPORTS = [(1280, 720), (1366, 768), (1920, 1080), (1000, 1000), (800, 600)]


def _axis_trips(grid: PointGrid, axis: int) -> list[tuple[int, int, int]]:
    """``(v, g, v')`` for every integer viewport value *v* on *axis*: ``g = to_grid``, back."""
    extent = (grid.viewport_width, grid.viewport_height)[axis]
    trips = []
    for v in range(extent):
        point = (v, 0) if axis == 0 else (0, v)
        g = grid.to_grid(*point)[axis]
        back = grid.to_viewport(*((g, 0) if axis == 0 else (0, g)))[axis]
        trips.append((v, g, back))
    return trips


class TestToGrid:
    """The writing direction (arch §4.15 "round-trip"): exact enumeration, not sampling."""

    def test_the_grid_point_of_a_viewport_point(self) -> None:
        grid = _grid("normalized_1000", (2560, 1440), (1280, 720))
        assert grid.to_grid(640, 360) == (500, 500)
        assert grid.to_grid(1279, 719) == (999, 999)
        dpr2 = _grid("screenshot_pixels", (2560, 1440), (1280, 720))
        assert dpr2.to_grid(100, 50) == (200, 100)

    def test_off_the_viewport_clamps_onto_the_grid_far_edge_included(self) -> None:
        grid = _grid("normalized_1000", (1280, 720), (1280, 720))
        assert grid.to_grid(-5, 2000) == (0, 1000)
        assert grid.to_grid(1280, 720) == (1000, 1000)

    def test_quantum_values(self) -> None:
        expected = {1280: 1, 1366: 1, 1920: 1, 1000: 0, 800: 0}
        for width, height in _NORMALIZED_VIEWPORTS:
            grid = _grid("normalized_1000", (0, 0), (width, height))
            assert grid.quantum == expected[width], width
        for dpr in (1, 1.5, 2):
            image = (round(1280 * dpr), round(720 * dpr))
            assert _grid("screenshot_pixels", image, (1280, 720)).quantum == 0

    def test_quantum_bounds_every_point(self) -> None:
        """Every integer viewport value on both axes: error <= quantum, and tight."""
        grids = [_grid("normalized_1000", (0, 0), size) for size in _NORMALIZED_VIEWPORTS]
        for dpr in (1, 1.5, 2):
            for width, height in ((1280, 720), (1366, 768), (1920, 1080)):
                image = (round(width * dpr), round(height * dpr))
                grids.append(_grid("screenshot_pixels", image, (width, height)))
        for grid in grids:
            worst = 0
            for axis in (0, 1):
                for v, _g, back in _axis_trips(grid, axis):
                    worst = max(worst, abs(back - v))
            assert worst <= grid.quantum, grid
            assert worst == grid.quantum, f"quantum is not tight for {grid}"

    def test_normalized_1000_at_1280_loses_280_x_values(self) -> None:
        """The measured loss behind ruling 2: 22% of x values do not come back."""
        lost = {
            width: sum(
                1
                for v, _g, back in _axis_trips(_grid("normalized_1000", (0, 0), (width, height)), 0)
                if back != v
            )
            for width, height in _NORMALIZED_VIEWPORTS
        }
        assert lost == {1280: 280, 1366: 366, 1920: 920, 1000: 0, 800: 0}

    def test_the_written_point_is_a_fixed_point(self) -> None:
        """``to_grid(to_viewport(g)) == g`` for every g ``to_grid`` can produce."""
        grids = [_grid("normalized_1000", (0, 0), size) for size in _NORMALIZED_VIEWPORTS]
        grids += [
            _grid("screenshot_pixels", (round(1280 * dpr), round(720 * dpr)), (1280, 720))
            for dpr in (1, 1.5, 2)
        ]
        for grid in grids:
            for axis in (0, 1):
                image = {g for _v, g, _back in _axis_trips(grid, axis)}
                for g in image:
                    point = (g, 0) if axis == 0 else (0, g)
                    viewport_point = grid.to_viewport(*point)
                    assert grid.to_grid(*viewport_point)[axis] == g, (grid, axis, g)
