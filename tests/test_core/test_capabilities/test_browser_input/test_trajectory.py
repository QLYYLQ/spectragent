"""Trajectory strategy invariants — endpoint exactness, timing, determinism."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.input._timing import fitts_duration_ms
from spectragent.core.capabilities.browser.input.bezier import BezierTrajectory
from spectragent.core.capabilities.browser.input.learned import LearnedTrajectory
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryProtocol,
    MouseTrajectoryRegistrar,
    TrajectoryPoint,
)
from spectragent.core.capabilities.browser.input.windmouse import WindMouseTrajectory

# (start, end) pairs covering same-point, 1px, short, and full-viewport diagonal.
_GRID = [
    ((0, 0), (0, 0)),
    ((100, 100), (101, 100)),
    ((10, 10), (200, 90)),
    ((5, 5), (1270, 710)),
    ((1270, 710), (5, 5)),
    ((640, 360), (640, 360)),
]


def _every_registered_strategy(mdn_export: Path) -> list[tuple[str, Any]]:
    """One live instance of every key in the registry, ``learned`` included.

    Built by iterating ``keys()`` rather than a literal list, so a strategy
    added later is covered by the invariants below without anyone remembering
    to add it here. ``learned`` is the only one that needs an argument (an
    export directory); everything else constructs bare.
    """
    built: list[tuple[str, Any]] = []
    for key in sorted(MouseTrajectoryRegistrar.keys()):
        cls = MouseTrajectoryRegistrar.get(key)
        built.append((key, cls(weights_dir=str(mdn_export)) if cls is LearnedTrajectory else cls()))
    return built


def test_registry_resolves_all_strategies(mdn_export: Path) -> None:
    for key, strat in _every_registered_strategy(mdn_export):
        assert strat.trajectory_type == key


def test_every_registered_strategy_satisfies_the_protocol(mdn_export: Path) -> None:
    """The check conscribe does not do.

    Registering a class does **not** verify it against the registry's
    protocol — a strategy missing ``generate`` or ``aim_offset_px`` registers
    happily and fails at the first click, minutes into a run. ``isinstance``
    against the runtime-checkable protocol does see the gap, so growing
    ``MouseTrajectoryProtocol`` is caught here for every strategy at once
    rather than by whoever runs the new one first.
    """
    for key, strat in _every_registered_strategy(mdn_export):
        assert isinstance(strat, MouseTrajectoryProtocol), key


def test_a_strategy_that_is_not_humanized_opts_out_of_everything(mdn_export: Path) -> None:
    """``humanized == False`` means the pre-humanization behaviour, exactly.

    Stated in the protocol's docstrings as a MUST; pinned here across the
    registry, because a per-strategy test named after ``instant`` only ever
    pins ``instant``.
    """
    for key, strat in _every_registered_strategy(mdn_export):
        if strat.humanized:
            continue
        assert strat.click_hold_ms() == 0.0, key
        assert strat.aim_offset_px(100.0, 40.0) == (0.0, 0.0), key


def test_no_strategy_aims_outside_an_unmeasured_box(mdn_export: Path) -> None:
    """Humanized or not: no box, no offset — never guess outward."""
    for key, strat in _every_registered_strategy(mdn_export):
        assert strat.aim_offset_px(0.0, 0.0) == (0.0, 0.0), key
        assert strat.aim_offset_px(0.0, 40.0) == (0.0, 0.0), key
        assert strat.aim_offset_px(100.0, 0.0) == (0.0, 0.0), key


def test_every_humanized_strategy_stays_inside_the_box_it_is_given(mdn_export: Path) -> None:
    for key, strat in _every_registered_strategy(mdn_export):
        if not strat.humanized:
            continue
        for _ in range(200):
            dx, dy = strat.aim_offset_px(100.0, 40.0)
            assert abs(dx) <= 48.0, key  # half of 100, less the 2 px margin
            assert abs(dy) <= 18.0, key


def test_instant_is_single_endpoint() -> None:
    inst = MouseTrajectoryRegistrar.get("instant")()
    assert inst.generate(0, 0, 55, 70) == [TrajectoryPoint(55, 70, 0.0)]
    # Floats round to the nearest pixel.
    assert inst.generate(3.2, 4.8, 55.4, 70.6) == [TrajectoryPoint(55, 71, 0.0)]


@pytest.mark.parametrize("name", ["windmouse", "bezier"])
@pytest.mark.parametrize("start,end", _GRID)
def test_endpoint_exactness(name: str, start: tuple[int, int], end: tuple[int, int]) -> None:
    strat = WindMouseTrajectory(seed=7) if name == "windmouse" else BezierTrajectory(seed=7)
    pts = strat.generate(start[0], start[1], end[0], end[1], target_w=30, target_h=20)
    assert pts, "trajectory must be non-empty"
    assert (pts[-1].x, pts[-1].y) == (round(end[0]), round(end[1]))


@pytest.mark.parametrize("name", ["windmouse", "bezier"])
def test_timing_is_well_formed(name: str) -> None:
    strat = WindMouseTrajectory(seed=3) if name == "windmouse" else BezierTrajectory(seed=3)
    pts = strat.generate(10, 10, 900, 500, target_w=40, target_h=20)
    assert len(pts) > 1
    assert pts[0].dt_ms == 0.0  # no pre-move sleep on the first point
    assert all(p.dt_ms >= 0.0 for p in pts)
    total = sum(p.dt_ms for p in pts)
    # Within the configured duration envelope (jitter keeps it close, not exact).
    assert 120.0 * 0.5 <= total <= 1400.0 * 1.5


@pytest.mark.parametrize("name", ["windmouse", "bezier"])
def test_max_points_bound(name: str) -> None:
    if name == "windmouse":
        strat = WindMouseTrajectory(seed=1, max_points=40)
    else:
        strat = BezierTrajectory(seed=1, max_points=40)
    pts = strat.generate(0, 0, 1270, 710, target_w=5, target_h=5)
    # WindMouse caps spatial points at max_points; Bézier caps samples at
    # max_points (so at most max_points + 2 after the appended endpoint).
    assert len(pts) <= 40 + 2


@pytest.mark.parametrize("name", ["windmouse", "bezier"])
def test_determinism_with_seed(name: str) -> None:
    def make():
        return WindMouseTrajectory(seed=99) if name == "windmouse" else BezierTrajectory(seed=99)

    a = make().generate(10, 10, 600, 400, target_w=30)
    b = make().generate(10, 10, 600, 400, target_w=30)
    assert a == b


@pytest.mark.parametrize("name", ["windmouse", "bezier"])
def test_zero_distance_is_single_point(name: str) -> None:
    strat = WindMouseTrajectory(seed=1) if name == "windmouse" else BezierTrajectory(seed=1)
    assert strat.generate(42, 42, 42, 42) == [TrajectoryPoint(42, 42, 0.0)]


def test_fitts_duration_scaling() -> None:
    # Bigger target → easier → shorter time; longer distance → harder → longer.
    kw = {"a": 80.0, "b": 55.0, "min_ms": 0.0, "max_ms": 100_000.0}
    big = fitts_duration_ms(500.0, 200.0, **kw)
    small = fitts_duration_ms(500.0, 10.0, **kw)
    assert big < small
    far = fitts_duration_ms(1500.0, 40.0, **kw)
    near = fitts_duration_ms(150.0, 40.0, **kw)
    assert far > near


def test_fitts_duration_clamped() -> None:
    # A large MT clamps to max_ms; a tiny one clamps to min_ms.
    assert fitts_duration_ms(2000.0, 5.0, a=80, b=55, min_ms=120, max_ms=300) == 300
    assert fitts_duration_ms(1.0, 9999.0, a=80, b=55, min_ms=120, max_ms=1400) == 120


@pytest.mark.parametrize("name", ["windmouse", "bezier"])
def test_path_curves_off_the_straight_line(name: str) -> None:
    """A human path should deviate from the straight segment at least once."""
    strat = WindMouseTrajectory(seed=5) if name == "windmouse" else BezierTrajectory(seed=5)
    sx, sy, ex, ey = 0, 0, 800, 0  # horizontal line → any y deviation is curvature
    pts = strat.generate(sx, sy, ex, ey, target_w=30)
    max_dev = max(abs(p.y - sy) for p in pts)
    assert max_dev > 0, "expected the path to bow away from the straight line"
    # Sanity: deviation stays bounded relative to the travel distance.
    assert max_dev < math.hypot(ex - sx, ey - sy)
