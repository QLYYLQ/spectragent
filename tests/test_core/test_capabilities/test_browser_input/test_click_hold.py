"""The button stays down for a human length of time.

Measured on real pages, every strategy pressed and released within 1.4-3.4 ms
— one CDP round trip, because that is literally all there was between the two
sends. A human holds for tens to hundreds of ms, and the press-to-release
duration is one of the cheapest things for a page to record.

The hold belongs to the strategy (it is humanization, and the strategy is the
one humanization switch) but not to the path (nothing moves while the button
is down), so it is :meth:`MouseTrajectoryProtocol.click_hold_ms`, not a field
on ``TrajectoryPoint``. It is spent on the same deadline scheduler as
everything else in the layer — a second clock here is what would reopen the
playback regression the scheduler exists to fix.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.input import mouse as mouse_module
from spectragent.core.capabilities.browser.input._timing import (
    DEFAULT_CLICK_HOLD_MAX_MS,
    DEFAULT_CLICK_HOLD_MEDIAN_MS,
    DEFAULT_CLICK_HOLD_MIN_MS,
    DEFAULT_CLICK_HOLD_SIGMA,
    sample_click_hold_ms,
)
from spectragent.core.capabilities.browser.input.bezier import BezierTrajectory
from spectragent.core.capabilities.browser.input.instant import InstantTrajectory
from spectragent.core.capabilities.browser.input.learned import LearnedTrajectory
from spectragent.core.capabilities.browser.input.mouse import HumanMouse
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryBase,
    MouseTrajectoryRegistrar,
    TrajectoryPoint,
)
from spectragent.core.capabilities.browser.input.windmouse import WindMouseTrajectory

from .conftest import RecordCDP, VirtualClock


class _FixedHold:
    """A humanized strategy: jump to the endpoint, hold for a stated time."""

    humanized = True

    def __init__(self, hold_ms: float) -> None:
        self.hold_ms = hold_ms
        self.holds_asked = 0

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        return (0.0, 0.0)

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
        return [TrajectoryPoint(round(end_x), round(end_y), 0.0)]

    def click_hold_ms(self) -> float:
        self.holds_asked += 1
        return self.hold_ms


class TestTheStrategyStatesTheHold:
    def test_instant_holds_for_exactly_zero(self) -> None:
        """Pinned. ``instant`` is the legacy click; a default here would humanize it."""
        assert InstantTrajectory().click_hold_ms() == 0.0
        # Not "small", not "approximately" — the one strategy that must not
        # spend any wall time between press and release.
        assert MouseTrajectoryRegistrar.get("instant")().click_hold_ms() == 0.0

    @pytest.mark.parametrize("name", ["windmouse", "bezier"])
    def test_a_humanized_strategy_holds_like_a_finger(self, name: str) -> None:
        strat = WindMouseTrajectory(seed=5) if name == "windmouse" else BezierTrajectory(seed=5)
        holds = [strat.click_hold_ms() for _ in range(200)]
        assert all(DEFAULT_CLICK_HOLD_MIN_MS <= h <= DEFAULT_CLICK_HOLD_MAX_MS for h in holds)
        # Tens to hundreds of ms, not the 1.4-3.4 ms of a bare round trip.
        assert 40.0 < sum(holds) / len(holds) < 200.0
        # A draw per click, so no two clicks are the same length.
        assert len(set(holds)) > 150

    def test_the_learned_strategy_holds_too(self, mdn_export: Path) -> None:
        """The models know nothing about buttons; the hold is still a human one."""
        strat = LearnedTrajectory(weights_dir=str(mdn_export), seed=2)
        holds = [strat.click_hold_ms() for _ in range(50)]
        assert all(DEFAULT_CLICK_HOLD_MIN_MS <= h <= DEFAULT_CLICK_HOLD_MAX_MS for h in holds)
        assert len(set(holds)) == 50

    @pytest.mark.parametrize("name", ["windmouse", "bezier"])
    def test_the_hold_replays_from_the_seed(self, name: str) -> None:
        def make() -> Any:
            return WindMouseTrajectory(seed=9) if name == "windmouse" else BezierTrajectory(seed=9)

        assert [make().click_hold_ms() for _ in range(3)] == [
            make().click_hold_ms() for _ in range(3)
        ]

    def test_a_strategy_that_forgets_is_told_so(self) -> None:
        """No base default: a duration nobody chose is exactly the defect.

        Called unbound rather than through a throwaway subclass — subclassing
        the base *registers* it, and the registry is process-wide.
        """
        strategy = InstantTrajectory()
        with pytest.raises(
            NotImplementedError, match="InstantTrajectory must implement click_hold_ms"
        ):
            MouseTrajectoryBase.click_hold_ms(strategy)

    def test_the_distribution_is_one_uniform_draw(self) -> None:
        """Inverse-CDF, so any RNG that spells ``random()`` drives it."""

        class _Fixed:
            def __init__(self, value: float) -> None:
                self.value = value

            def random(self) -> float:
                return self.value

        def hold(u: float) -> float:
            return sample_click_hold_ms(
                _Fixed(u),
                median_ms=DEFAULT_CLICK_HOLD_MEDIAN_MS,
                sigma=DEFAULT_CLICK_HOLD_SIGMA,
                min_ms=DEFAULT_CLICK_HOLD_MIN_MS,
                max_ms=DEFAULT_CLICK_HOLD_MAX_MS,
            )

        assert hold(0.5) == pytest.approx(DEFAULT_CLICK_HOLD_MEDIAN_MS)
        # The tails clamp rather than running away — including at the closed
        # ends of (0, 1), where inv_cdf is undefined and random() can land.
        assert hold(0.0) == DEFAULT_CLICK_HOLD_MIN_MS
        assert hold(1.0) == DEFAULT_CLICK_HOLD_MAX_MS
        assert hold(0.9) > hold(0.1)

    @pytest.mark.parametrize("name", ["windmouse", "bezier"])
    def test_the_hold_is_the_strategys_own_configured_distribution(self, name: str) -> None:
        """The numbers are constructor knobs, i.e. fields of the YAML block.

        A module constant would mean a run cannot tune the one behaviour this
        whole defect is about without editing a ``.py``.
        """
        kwargs: dict[str, Any] = {
            "seed": 4,
            "click_hold_median_ms": 250.0,
            "click_hold_sigma": 0.1,
            "click_hold_min_ms": 200.0,
            "click_hold_max_ms": 300.0,
        }
        strat = WindMouseTrajectory(**kwargs) if name == "windmouse" else BezierTrajectory(**kwargs)
        holds = [strat.click_hold_ms() for _ in range(100)]
        assert all(200.0 <= h <= 300.0 for h in holds)
        assert 220.0 < sum(holds) / len(holds) < 280.0

    def test_a_zero_sigma_holds_for_exactly_the_median(self) -> None:
        strat = BezierTrajectory(seed=1, click_hold_median_ms=140.0, click_hold_sigma=0.0)
        assert [strat.click_hold_ms() for _ in range(5)] == [140.0] * 5


class TestTheControllerSpendsIt:
    async def test_instant_click_still_sleeps_nothing(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(cdp, session_id_getter=lambda: None)  # instant by default
        await mouse.click(55, 70, "left", 1)
        assert cdp.types() == ["mouseMoved", "mousePressed", "mouseReleased"]
        assert no_sleep == []

    async def test_the_release_waits_out_the_hold(self, clock: VirtualClock) -> None:
        cdp = RecordCDP(clock)
        strategy = _FixedHold(120.0)
        mouse = HumanMouse(cdp, session_id_getter=lambda: "sid", trajectory=strategy)
        await mouse.initialize()
        cdp.calls.clear()
        start = clock.now
        await mouse.click(300, 200, "left", 1)

        assert cdp.types() == ["mouseMoved", "mousePressed", "mouseReleased"]
        assert clock.sleeps == pytest.approx([0.120])
        assert clock.now - start == pytest.approx(0.120)
        assert strategy.holds_asked == 1  # one draw per click, not per event

    async def test_the_press_round_trip_is_spent_inside_the_hold(self, clock: VirtualClock) -> None:
        """The whole reason this goes through the deadline scheduler.

        A 30 ms round trip plus a 120 ms sleep would hold the button for 150
        ms — the transport deciding the behaviour, which is the bug the
        scheduler was introduced to kill. On the schedule the release is
        due 120 ms after the press was *due*, so the round trip comes out of
        the hold and the hold is the hold.
        """
        cdp = RecordCDP(clock, cost_ms=30.0)
        mouse = HumanMouse(cdp, session_id_getter=lambda: "sid", trajectory=_FixedHold(120.0))
        await mouse.click(300, 200, "left", 1)

        assert clock.sleeps == pytest.approx([0.090])  # 120 ms hold - 30 ms press
        press = next(i for i, t in enumerate(cdp.types()) if t == "mousePressed")
        assert cdp.types()[press + 1] == "mouseReleased"

    async def test_a_transport_slower_than_the_hold_never_loses_the_press(
        self, clock: VirtualClock
    ) -> None:
        """Shorten the hold, never skip the press — a release with no press is a bug."""
        cdp = RecordCDP(clock, cost_ms=200.0)
        mouse = HumanMouse(cdp, session_id_getter=lambda: "sid", trajectory=_FixedHold(50.0))
        await mouse.initialize()
        cdp.calls.clear()
        await mouse.click(300, 200, "left", 1)
        assert cdp.types() == ["mouseMoved", "mousePressed", "mouseReleased"]
        assert clock.sleeps == []  # the hold was gone before it started

    async def test_the_hold_comes_from_the_strategy_in_use(self, clock: VirtualClock) -> None:
        cdp = RecordCDP(clock)
        strategy = WindMouseTrajectory(seed=13, max_points=4)
        mouse = HumanMouse(cdp, session_id_getter=lambda: "sid", trajectory=strategy)
        await mouse.click(700, 400, "left", 1, target_w=40, target_h=20)
        # The path's own ticks, then one last sleep: the button's hold, drawn
        # from this strategy's stream right after it drew the path.
        expected = WindMouseTrajectory(seed=13, max_points=4)
        expected.generate(640, 360, 700, 400, target_w=40, target_h=20)
        assert clock.sleeps[-1] == pytest.approx(expected.click_hold_ms() / 1000.0)

    def test_the_mouse_owns_no_clock_of_its_own(self) -> None:
        """One scheduler in ``input/``; a second timing path would drift from it."""
        assert not hasattr(mouse_module, "asyncio")

    def test_the_mouse_has_no_pause_knob_of_its_own(self) -> None:
        """``dwell_ms`` is deleted, not defaulted to 0.

        It was never set by anything and never reachable from YAML, and it
        overlapped the pause a strategy already models as repeated waypoints.
        A pre-click pause belongs to the strategy; this pins that it does not
        come back as a second one here.
        """
        params = inspect.signature(HumanMouse.__init__).parameters
        assert "dwell_ms" not in params
