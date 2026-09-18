"""A click lands *inside* the element, not on its exact centre (defect B2).

Measured on real pages, every strategy — instant or not — put the press within
0.78 px of the element's arithmetic centre every single time, because the
centre is what the box model hands back and nothing ever moved it. Humans
scatter 3-31 px. No amount of path realism hides a landing point that is
pixel-perfect on every click.

Two halves, tested separately here:

- the *policy* is the trajectory strategy's (``aim_offset_px``), so it swaps
  with ``trajectory_type`` and its knobs are that strategy's config;
- the *seam* is ``ActionWatchdog._aim_inside``, the one place a measured box
  becomes a point. It clips the offset to the visible viewport — a border box
  is not clipped to the window — and applies it, so the trajectory still ends
  exactly on the point it was handed and the path's endpoint, the press, the
  result and the overlay all agree on one point.
"""

from __future__ import annotations

import random
from statistics import fmean, pstdev
from typing import Any

from spectragent.core.capabilities.browser.dom.models import DOMRect
from spectragent.core.capabilities.browser.input import InstantTrajectory, WindMouseTrajectory
from spectragent.core.capabilities.browser.input._aim import (
    DEFAULT_AIM_MARGIN_PX,
    DEFAULT_AIM_SIGMA_RATIO,
    sample_aim_offset_px,
)
from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import (
    ClickCoordinateEvent,
    ClickElementEvent,
    ScrollEvent,
    TypeTextEvent,
)
from tests.test_core.test_capabilities.test_browser_action_click_robustness import (
    FakeCDP,
    _make_node,
)

#: The live rect FakeCDP reports, and the centre it implies.
BOX = {"x": 570.0, "y": 390.0, "w": 100.0, "h": 40.0}
CENTRE = (620, 410)


class _Aimless:
    """Humanized, with no path and no hold: only the aim is under test.

    It draws through the shipped sampler with the shipped defaults, so these
    tests measure the real aiming policy rather than a stand-in for it.
    """

    humanized = True

    def __init__(
        self,
        seed: int | None = None,
        *,
        sigma_ratio: float = DEFAULT_AIM_SIGMA_RATIO,
        margin_px: float = DEFAULT_AIM_MARGIN_PX,
    ) -> None:
        self._rng = random.Random(seed)
        self._sigma_ratio = sigma_ratio
        self._margin_px = margin_px
        self.seen: list[tuple[int, int]] = []
        self.boxes: list[tuple[float, float]] = []

    def generate(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        *,
        target_w: float = 0.0,
        target_h: float = 0.0,
    ) -> list[TrajectoryPoint]:
        self.seen.append((round(end_x), round(end_y)))
        return [TrajectoryPoint(round(end_x), round(end_y), 0.0)]

    def click_hold_ms(self) -> float:
        return 0.0

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        self.boxes.append((width, height))
        return sample_aim_offset_px(
            self._rng,
            width,
            height,
            sigma_ratio=self._sigma_ratio,
            margin_px=self._margin_px,
        )


def _watchdog(
    strategy: Any,
    rect: dict[str, float] | None = None,
    viewport: tuple[int, int] = (1280, 720),
) -> ActionWatchdog:
    node = _make_node(42, DOMRect(x=0.0, y=0.0, width=100.0, height=40.0))
    cdp = FakeCDP(rect=dict(rect or BOX))
    return ActionWatchdog(
        cdp=cdp,  # type: ignore[arg-type]
        selector_map_getter=lambda: {42: node},
        mouse_trajectory=strategy,
        viewport=viewport,
    )


async def _land(wd: ActionWatchdog, times: int = 1) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for _ in range(times):
        result = await wd._on_click_element(ClickElementEvent(backend_node_id=42))
        assert result is not None and result["clicked"] is True
        points.append((result["x"], result["y"]))
    return points


class TestTheLandingPointScatters:
    async def test_the_centre_is_not_where_every_click_goes(self) -> None:
        points = await _land(_watchdog(_Aimless(seed=1)), times=300)
        assert len(set(points)) > 50
        # Dead centre happens, as one draw among many — not as the rule.
        assert points.count(CENTRE) < 30

    async def test_the_scatter_is_human_sized_and_unbiased(self) -> None:
        points = await _land(_watchdog(_Aimless(seed=2)), times=400)
        xs = [x - CENTRE[0] for x, _ in points]
        ys = [y - CENTRE[1] for _, y in points]
        # sigma = min(100, 40) / 6 ≈ 6.7 px on both axes — the short side sets
        # the spread, so it fits the tightest direction.
        assert 4.0 < pstdev(xs) < 9.0
        assert 4.0 < pstdev(ys) < 9.0
        # Centred on the element, not nudged off it.
        assert abs(fmean(xs)) < 1.0
        assert abs(fmean(ys)) < 1.0

    async def test_every_landing_stays_inside_the_border(self) -> None:
        points = await _land(_watchdog(_Aimless(seed=3)), times=400)
        for x, y in points:
            # 100x40 at (570, 390), keeping the 2 px margin.
            assert 572 <= x <= 668
            assert 392 <= y <= 428

    async def test_a_thin_box_is_bounded_by_its_thin_side(self) -> None:
        wd = _watchdog(_Aimless(seed=4), rect={"x": 100.0, "y": 300.0, "w": 400.0, "h": 8.0})
        points = await _land(wd, times=200)
        for x, y in points:
            assert 302 <= y <= 310  # 8 px tall, 2 px margin → +-2 px
            assert 102 <= x <= 498
        assert len({y for _, y in points}) > 1  # still scatters where it can

    def test_a_box_the_margin_swallows_keeps_its_centre(self) -> None:
        wd = _watchdog(_Aimless(seed=5))
        assert wd._aim_inside(50, 50, 4.0, 4.0) == (50, 50)

    def test_an_unmeasured_box_keeps_its_centre(self) -> None:
        """No box, nothing to scatter inside — never guess outward."""
        wd = _watchdog(_Aimless(seed=6))
        assert wd._aim_inside(50, 50, 0.0, 0.0) == (50, 50)
        assert wd._aim_inside(50, 50, 0.0, 40.0) == (50, 50)


class TestItNeverAimsOffScreen:
    """A measured border box is not clipped to the window; the aim must be.

    ``getBoundingClientRect`` (and the frame path's quad bounding box) report
    the element's whole box wherever it is — a hero banner taller than the
    viewport keeps its full height, and ``scrollIntoViewIfNeeded`` only
    guarantees the element is *reachable*, not that all of it is on screen. A
    dispatched point outside the viewport is not a click on the element: Chrome
    drops it while ``_click_at`` still reports ``clicked``.
    """

    async def test_an_element_taller_than_the_viewport_still_lands_on_screen(self) -> None:
        # 1280x900 element hanging 90 px above the fold of a 1280x720 window:
        # centre (640, 360) is visible, the box reaches y = -90 and y = 810.
        wd = _watchdog(
            _Aimless(seed=1),
            rect={"x": 0.0, "y": -90.0, "w": 1280.0, "h": 900.0},
        )
        points = await _land(wd, times=300)
        for x, y in points:
            assert 0 <= x <= 1279
            assert 0 <= y <= 719
        # Clipped, not disabled: it still scatters inside what is visible.
        assert len(set(points)) > 50

    async def test_the_press_goes_where_the_result_says_even_then(self) -> None:
        wd = _watchdog(
            _Aimless(seed=2),
            rect={"x": -200.0, "y": -200.0, "w": 1700.0, "h": 1100.0},
        )
        points = await _land(wd, times=50)
        presses = [
            (c.params["x"], c.params["y"])
            for c in wd._cdp.calls  # type: ignore[attr-defined]
            if c.method == "Input.dispatchMouseEvent" and c.params["type"] == "mousePressed"
        ]
        assert presses == points
        for x, y in presses:
            assert 0 <= x <= 1279 and 0 <= y <= 719

    def test_the_visible_span_is_symmetric_about_the_centre(self) -> None:
        """An asymmetric window would bias a centred distribution off the box."""
        strategy = _Aimless(seed=3)
        wd = _watchdog(strategy, viewport=(1280, 720))
        # Centre 100 px from the top edge, box 900 px tall: 100 px of reach
        # on the short side, so 200 px of span, not 810.
        wd._aim_inside(640, 100, 1280.0, 900.0)
        # Horizontally the last addressable column is 1279, so the reach from
        # x=640 is 639 either way; vertically it is the 100 px above the
        # centre, not the 810 the box claims below it.
        assert strategy.boxes[-1] == (1278.0, 200.0)

    def test_a_centre_off_screen_keeps_its_exact_point(self) -> None:
        """Nothing sensible to aim at — leave it exactly as it was."""
        strategy = _Aimless(seed=4)
        wd = _watchdog(strategy)
        assert wd._aim_inside(640, -30, 1280.0, 900.0) == (640, -30)
        assert wd._aim_inside(-5, 360, 1280.0, 900.0) == (-5, 360)


class TestWhatKeepsItsExactPoint:
    async def test_instant_lands_dead_centre(self) -> None:
        """The legacy path stays byte-for-byte legacy, landing included."""
        points = await _land(_watchdog(InstantTrajectory()), times=20)
        assert set(points) == {CENTRE}

    async def test_no_strategy_at_all_lands_dead_centre(self) -> None:
        node = _make_node(42, DOMRect(x=0.0, y=0.0, width=100.0, height=40.0))
        cdp = FakeCDP(rect=dict(BOX))
        wd = ActionWatchdog(cdp=cdp, selector_map_getter=lambda: {42: node})  # type: ignore[arg-type]
        assert await _land(wd, times=5) == [CENTRE] * 5

    async def test_a_coordinate_click_is_the_models_own_aim(self) -> None:
        """``click_coordinate`` carries the model's intent; we do not overrule it."""
        strategy = _Aimless(seed=7)
        cdp = FakeCDP()
        wd = ActionWatchdog(cdp=cdp, mouse_trajectory=strategy)  # type: ignore[arg-type]
        for _ in range(20):
            result = await wd._on_click_coordinate(
                ClickCoordinateEvent(coordinate_x=311, coordinate_y=207)
            )
            assert result == {"clicked": True, "x": 311, "y": 207}
        assert set(strategy.seen) == {(311, 207)}
        assert strategy.boxes == []  # the aiming policy is never even asked


class TestEveryMeasuredBoxComesThroughTheSeam:
    """A click is not the only action that turns a box into a point."""

    async def test_the_cursor_approach_before_typing_scatters_too(self) -> None:
        """A mousemove endpoint is exactly what a page records."""
        strategy = _Aimless(seed=11)
        wd = _watchdog(strategy)
        rests: set[tuple[int, int]] = set()
        for _ in range(40):
            await wd._on_type_text(TypeTextEvent(backend_node_id=42, text="hi"))
            moves = [
                (c.params["x"], c.params["y"])
                for c in wd._cdp.calls  # type: ignore[attr-defined]
                if c.method == "Input.dispatchMouseEvent" and c.params["type"] == "mouseMoved"
            ]
            rests.add(moves[-1])
        assert len(rests) > 10
        for x, y in rests:
            assert 572 <= x <= 668 and 392 <= y <= 428

    async def test_the_wheel_is_aimed_the_same_way(self) -> None:
        strategy = _Aimless(seed=12)
        wd = _watchdog(strategy)
        aims: set[tuple[int, int]] = set()
        for _ in range(40):
            await wd._on_scroll(ScrollEvent(direction="down", amount=120, backend_node_id=42))
            wheels = [
                (c.params["x"], c.params["y"])
                for c in wd._cdp.calls  # type: ignore[attr-defined]
                if c.method == "Input.dispatchMouseEvent" and c.params["type"] == "mouseWheel"
            ]
            aims.add(wheels[-1])
        assert len(aims) > 10
        for x, y in aims:
            assert 572 <= x <= 668 and 392 <= y <= 428


class TestTheAimedPointIsTheOnePoint:
    async def test_the_trajectory_is_handed_the_aimed_point(self) -> None:
        strategy = _Aimless(seed=8)
        wd = _watchdog(strategy)
        points = await _land(wd, times=30)
        # What the result reports, what the path was asked to end on, and
        # where the button went down are one and the same point.
        assert strategy.seen == points
        presses = [
            (c.params["x"], c.params["y"])
            for c in wd._cdp.calls  # type: ignore[attr-defined]
            if c.method == "Input.dispatchMouseEvent" and c.params["type"] == "mousePressed"
        ]
        assert presses == points

    async def test_the_box_handed_to_the_trajectory_is_still_the_element(self) -> None:
        """The aim moves the point, never the target size Fitts's law reads."""
        seen: list[tuple[float, float]] = []

        class _Recorder(_Aimless):
            def generate(self, *a: Any, **k: Any) -> list[TrajectoryPoint]:
                seen.append((k["target_w"], k["target_h"]))
                return super().generate(*a, **k)

        await _land(_watchdog(_Recorder(seed=9)), times=5)
        assert seen == [(100.0, 40.0)] * 5


class TestTheScatterReplays:
    async def test_the_same_seed_lands_in_the_same_places(self) -> None:
        first = await _land(_watchdog(_Aimless(seed=42)), times=25)
        second = await _land(_watchdog(_Aimless(seed=42)), times=25)
        assert first == second
        assert len(set(first)) > 1

    async def test_a_different_seed_lands_elsewhere(self) -> None:
        first = await _land(_watchdog(_Aimless(seed=42)), times=25)
        other = await _land(_watchdog(_Aimless(seed=43)), times=25)
        assert first != other

    async def test_an_unseeded_strategy_still_scatters(self) -> None:
        """No seed means no reproducibility — it never means no offset."""
        points = await _land(_watchdog(_Aimless(seed=None)), times=60)
        assert len(set(points)) > 5

    async def test_a_real_strategy_is_wired_the_same_way(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            "spectragent.core.capabilities.browser.input._schedule.asyncio.sleep",
            _no_sleep,
        )
        first = await _land(_watchdog(WindMouseTrajectory(seed=11, max_points=4)), times=8)
        second = await _land(_watchdog(WindMouseTrajectory(seed=11, max_points=4)), times=8)
        assert first == second
        assert len(set(first)) > 1
        assert CENTRE not in first or first.count(CENTRE) < len(first)


class TestTheAimIsConfigurable:
    """The knobs are the strategy's constructor, i.e. fields of its YAML block."""

    async def test_sigma_zero_pins_the_exact_centre(self) -> None:
        """The opt-out a run can write down, without swapping to ``instant``."""
        points = await _land(_watchdog(_Aimless(seed=1, sigma_ratio=0.0)), times=20)
        assert set(points) == {CENTRE}

    async def test_a_wider_ratio_scatters_wider(self) -> None:
        tight = await _land(_watchdog(_Aimless(seed=2, sigma_ratio=1 / 12)), times=300)
        loose = await _land(_watchdog(_Aimless(seed=2, sigma_ratio=1 / 3)), times=300)
        assert pstdev([x for x, _ in tight]) < pstdev([x for x, _ in loose])

    async def test_a_bigger_margin_keeps_further_from_the_border(self) -> None:
        points = await _land(_watchdog(_Aimless(seed=3, margin_px=15.0)), times=300)
        for x, y in points:
            assert 585 <= x <= 655  # 100 px wide, 15 px margin
            assert 405 <= y <= 415  # 40 px tall, 15 px margin

    def test_a_real_strategy_carries_them_as_constructor_knobs(self) -> None:
        """Pinned because this is what makes them reachable from YAML."""
        pinned = WindMouseTrajectory(seed=1, aim_sigma_ratio=0.0)
        assert pinned.aim_offset_px(100.0, 40.0) == (0.0, 0.0)
        wide = WindMouseTrajectory(seed=1, aim_sigma_ratio=1 / 3)
        assert wide.aim_offset_px(100.0, 40.0) != (0.0, 0.0)


async def _no_sleep(_seconds: float) -> None:
    return None
