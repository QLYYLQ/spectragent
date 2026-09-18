"""HumanMouse controller — instant-strategy equivalence + humanized behavior."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from spectragent.core.capabilities.browser.input.bezier import BezierTrajectory
from spectragent.core.capabilities.browser.input.instant import InstantTrajectory
from spectragent.core.capabilities.browser.input.learned import LearnedTrajectory
from spectragent.core.capabilities.browser.input.mouse import HumanMouse
from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint
from spectragent.core.capabilities.browser.input.windmouse import WindMouseTrajectory

from .conftest import TINY_SEQ_LEN, RecordCDP, VirtualClock


class TestOffModeEquivalence:
    """The instant strategy (the default) must reproduce the legacy instant jump."""

    async def test_click_is_three_events_at_target_no_sleep(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(cdp, session_id_getter=lambda: None)  # instant by default
        await mouse.click(55, 70, "left", 1)

        assert cdp.types() == ["mouseMoved", "mousePressed", "mouseReleased"]
        for ev in cdp.mouse_events():
            assert (ev["x"], ev["y"]) == (55, 70)
        assert no_sleep == []  # zero sleeps when off

    async def test_click_off_viewport_endpoint_not_clamped(self, no_sleep: list[float]) -> None:
        # The canary fallback clicks (450, 820) on a 1280x720 viewport — the
        # endpoint must dispatch raw, never clamped to the viewport.
        cdp = RecordCDP()
        mouse = HumanMouse(cdp, session_id_getter=lambda: None, viewport=(1280, 720))
        await mouse.click(450, 820, "left", 1)
        for ev in cdp.mouse_events():
            assert (ev["x"], ev["y"]) == (450, 820)

    async def test_wheel_off_is_single_event(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(cdp, session_id_getter=lambda: None)
        await mouse.wheel(100, 200, 0, 300)
        evs = cdp.mouse_events()
        assert len(evs) == 1
        assert evs[0]["type"] == "mouseWheel"
        assert (evs[0]["deltaX"], evs[0]["deltaY"]) == (0, 300)
        assert mouse.position == (100, 200)


class TestHumanizedMode:
    async def test_click_has_many_moves_in_bounds_ending_on_target(
        self, no_sleep: list[float]
    ) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=WindMouseTrajectory(seed=1, max_points=80),
            viewport=(1280, 720),
        )
        await mouse.click(600, 400, "left", 1, target_w=40, target_h=20)

        types = cdp.types()
        assert types[-2:] == ["mousePressed", "mouseReleased"]
        assert types.count("mouseMoved") > 1
        # Press/release land exactly on target.
        last = cdp.mouse_events()[-1]
        assert (last["x"], last["y"]) == (600, 400)
        # Intermediate moves stay inside the viewport.
        for ev in cdp.mouse_events():
            if ev["type"] == "mouseMoved":
                assert 0 <= ev["x"] <= 1279
                assert 0 <= ev["y"] <= 719
        # Bounded event count.
        assert len(cdp.mouse_events()) <= 80 + 2

    async def test_click_sleeps_between_moves(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=WindMouseTrajectory(seed=1),
        )
        await mouse.click(600, 400, "left", 1, target_w=40)
        # Humanized motion sleeps before most intermediate moves.
        assert len(no_sleep) > 1
        assert all(s >= 0 for s in no_sleep)

    async def test_wheel_on_splits_delta_exactly(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=WindMouseTrajectory(seed=1),
            wheel_steps=6,
        )
        await mouse.wheel(300, 300, 0, 480)
        wheels = [e for e in cdp.mouse_events() if e["type"] == "mouseWheel"]
        assert len(wheels) == 6  # split into wheel_steps increments
        # The increments sum exactly to the requested delta.
        assert sum(w["deltaY"] for w in wheels) == 480
        assert sum(w["deltaX"] for w in wheels) == 0

    async def test_humanized_cold_start_is_center(self) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=WindMouseTrajectory(),
            viewport=(1280, 720),
        )
        assert mouse.position == (640, 360)

    async def test_drag_press_move_release(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=WindMouseTrajectory(seed=2),
        )
        await mouse.drag(100, 100, 500, 400)
        types = cdp.types()
        assert types[0] == "mouseMoved"
        assert "mousePressed" in types
        assert types[-1] == "mouseReleased"
        # Press precedes the cross-screen moves which precede release.
        assert types.index("mousePressed") < types.index("mouseReleased")
        last = cdp.mouse_events()[-1]
        assert (last["x"], last["y"]) == (500, 400)


class _ScriptedPath:
    """A strategy that returns a fixed waypoint list (humanized)."""

    def __init__(self, points: list[TrajectoryPoint], hold_ms: float = 0.0) -> None:
        self.points = points
        self.hold_ms = hold_ms

    @property
    def humanized(self) -> bool:
        return True

    def click_hold_ms(self) -> float:
        return self.hold_ms

    def aim_offset_px(self, width: float, height: float) -> tuple[float, float]:
        """The controller never applies this; it is here to satisfy the protocol."""
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
        return list(self.points)


class TestRepeatedWaypoints:
    """A repeated waypoint is stillness: its time is spent, no event is sent."""

    async def test_repeats_sleep_without_dispatching(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        path = [
            TrajectoryPoint(640, 360, 0.0),  # the cold-start position itself
            TrajectoryPoint(700, 380, 20.0),
            TrajectoryPoint(700, 380, 20.0),  # a still tick mid-path
            TrajectoryPoint(760, 400, 20.0),
            TrajectoryPoint(760, 400, 20.0),  # dwelling on the target
            TrajectoryPoint(760, 400, 20.0),
        ]
        mouse = HumanMouse(cdp, session_id_getter=lambda: "sid", trajectory=_ScriptedPath(path))
        await mouse.click(760, 400, "left", 1)
        assert cdp.types() == ["mouseMoved", "mouseMoved", "mousePressed", "mouseReleased"]
        assert no_sleep == pytest.approx([0.02] * 5)

    async def test_single_point_path_always_dispatches(self, no_sleep: list[float]) -> None:
        # The instant jump onto the current position still sends its event.
        cdp = RecordCDP()
        mouse = HumanMouse(cdp, session_id_getter=lambda: None)
        await mouse.click(0, 0, "left", 1)
        assert cdp.types() == ["mouseMoved", "mousePressed", "mouseReleased"]


class TestDeadlineSchedule:
    """Waypoints are due at absolute times, so latency comes out of the tick.

    Sleeping ``dt_ms`` and then awaiting the dispatch added the CDP round trip
    to every tick — ~17 ms against headless Chrome, which replayed a 50 Hz
    learned path at 33 Hz and a windmouse path at 5x its own duration. The
    timing is the whole point of a trajectory: the learned models match human
    speed only at the rate they were recorded at.
    """

    @staticmethod
    def _path(ticks: int, dt_ms: float = 20.0) -> list[TrajectoryPoint]:
        """Starts on the humanized cold start, then one 10 px step per tick."""
        return [TrajectoryPoint(640, 360, 0.0)] + [
            TrajectoryPoint(640 + 10 * i, 360, dt_ms) for i in range(1, ticks + 1)
        ]

    def _mouse(self, cdp: RecordCDP, path: list[TrajectoryPoint]) -> HumanMouse:
        return HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=_ScriptedPath(path),
            viewport=(1280, 720),
        )

    async def test_dispatch_latency_is_absorbed_into_the_tick(self, clock: VirtualClock) -> None:
        cdp = RecordCDP(clock, cost_ms=8.0)
        mouse = self._mouse(cdp, self._path(10))
        start = clock.now
        await mouse.move_to(740, 360)

        # 10 ticks of 20 ms take the path's own 200 ms plus the last round trip
        # (nothing follows it to absorb it), not 200 + 10 x 8.
        assert clock.now - start == pytest.approx(0.208)
        # Every tick but the first pays for the round trip out of its own slot.
        assert clock.sleeps == pytest.approx([0.020] + [0.012] * 9)
        moves = [(e["x"], e["y"]) for e in cdp.mouse_events()]
        assert len(moves) == 10  # nothing dropped while the transport keeps up
        assert moves[-1] == (740, 360)

    async def test_a_dispatch_slower_than_the_tick_drops_waypoints_not_time(
        self, clock: VirtualClock
    ) -> None:
        cdp = RecordCDP(clock, cost_ms=47.0)
        mouse = self._mouse(cdp, self._path(10))
        start = clock.now
        await mouse.move_to(740, 360)

        # Each waypoint whose whole 20 ms slot had already passed is dropped,
        # so the ones that are sent are sent at their own time. Playing every
        # waypoint would have taken 10 x (20 + 47) = 670 ms for a 200 ms path.
        assert [e["x"] for e in cdp.mouse_events()] == [650, 670, 690, 720, 740]
        assert clock.now - start == pytest.approx(0.255)
        assert clock.sleeps == pytest.approx([0.020])  # only the first tick had slack

    async def test_the_endpoint_survives_a_transport_that_cannot_keep_up(
        self, clock: VirtualClock
    ) -> None:
        # 1 ms waypoints against a 40 ms round trip — the shape of a Fitts
        # ease-out tail. Everything intermediate goes; the endpoint never does.
        cdp = RecordCDP(clock, cost_ms=40.0)
        mouse = self._mouse(cdp, self._path(10, dt_ms=1.0))
        start = clock.now
        await mouse.move_to(740, 360)

        assert [(e["x"], e["y"]) for e in cdp.mouse_events()] == [(650, 360), (740, 360)]
        assert mouse.position == (740, 360)
        # The overshoot is the one dispatch that could not be skipped, not ten.
        assert clock.now - start == pytest.approx(0.081)

    async def test_a_still_waypoint_waits_out_its_slot_under_latency(
        self, clock: VirtualClock
    ) -> None:
        # The model's pre-click dwell: three repeats of the target. They send
        # nothing, so they cost no latency and keep their time exactly.
        path = [*self._path(3), *([TrajectoryPoint(670, 360, 20.0)] * 3)]
        cdp = RecordCDP(clock, cost_ms=8.0)
        mouse = self._mouse(cdp, path)
        start = clock.now
        await mouse.move_to(670, 360)

        assert len(cdp.mouse_events()) == 3
        assert clock.sleeps == pytest.approx([0.020] + [0.012] * 3 + [0.020] * 2)
        # The dwell absorbs the last move's round trip, so the click that
        # follows lands exactly at the path's own duration.
        assert clock.now - start == pytest.approx(0.120)


class TestWheelDeadlineSchedule:
    """An eased scroll keeps its own rhythm, not the transport's.

    ``wheel`` used to sleep ``wheel_step_delay_ms`` and *then* await the
    dispatch, so every step was stretched by the CDP round trip — measured
    against headless Chrome, a 12 ms step took ~28 ms and a 20-step scroll
    2.5x the duration it asked for. The step rhythm is exactly the behavioural
    signal the segmented scroll exists to produce, so it runs on the same
    absolute-deadline schedule as ``move_to``.
    """

    #: The ease-out split of 600 into 6 steps, which the tests below regroup.
    INCREMENTS = [183, 150, 117, 83, 50, 17]

    def _mouse(self, cdp: RecordCDP, *, steps: int = 6, delay: float = 12.0) -> HumanMouse:
        # A one-waypoint humanized path: the approach move costs one round
        # trip and no time of its own, so what follows is the scroll alone.
        return HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=_ScriptedPath([TrajectoryPoint(300, 300, 0.0)]),
            viewport=(1280, 720),
            wheel_steps=steps,
            wheel_step_delay_ms=delay,
        )

    @staticmethod
    def _wheels(cdp: RecordCDP) -> list[int]:
        return [e["deltaY"] for e in cdp.mouse_events() if e["type"] == "mouseWheel"]

    async def test_the_split_is_an_ease_out_summing_to_the_delta(self, clock: VirtualClock) -> None:
        cdp = RecordCDP(clock)
        await self._mouse(cdp).wheel(300, 300, 0, 600)
        assert self._wheels(cdp) == self.INCREMENTS
        assert sum(self.INCREMENTS) == 600
        # Decelerating into the stop — that is what "eased" means here.
        assert all(a > b for a, b in zip(self.INCREMENTS, self.INCREMENTS[1:], strict=False))

    async def test_dispatch_latency_is_absorbed_into_the_step(self, clock: VirtualClock) -> None:
        cdp = RecordCDP(clock, cost_ms=8.0)
        start = clock.now
        await self._mouse(cdp).wheel(300, 300, 0, 600)

        assert self._wheels(cdp) == self.INCREMENTS
        # Five 12 ms gaps is what a six-step scroll asks for; each pays for its
        # own round trip out of its own slot, so only the last one is added.
        # Sleeping the delay and then dispatching took 5 x 12 + 6 x 8 = 108 ms.
        assert clock.sleeps == pytest.approx([0.004] * 5)
        assert clock.now - start == pytest.approx(0.008 + 0.060 + 0.008)

    async def test_the_first_step_is_due_immediately(self, clock: VirtualClock) -> None:
        # The requested duration is (steps - 1) delays: no leading pause, so a
        # scroll starts the moment the cursor arrives.
        cdp = RecordCDP(clock)
        await self._mouse(cdp, steps=4).wheel(300, 300, 0, 600)
        assert clock.sleeps == pytest.approx([0.012] * 3)

    async def test_a_slow_dispatch_merges_steps_rather_than_stretching_them(
        self, clock: VirtualClock
    ) -> None:
        cdp = RecordCDP(clock, cost_ms=40.0)
        start = clock.now
        await self._mouse(cdp).wheel(300, 300, 0, 600)

        # Steps 2-4 and 6 never got a slot of their own; their deltas ride
        # along with the next step that is sent, so nothing is lost and the
        # scroll coarsens instead of running 5 x 12 + 6 x 40 = 300 ms long.
        assert self._wheels(cdp) == [183, 150 + 117 + 83, 50 + 17]
        assert sum(self._wheels(cdp)) == 600
        assert clock.sleeps == []  # never ahead of schedule, never waiting
        assert clock.now - start == pytest.approx(0.040 + 0.120)

    async def test_the_last_step_is_never_dropped(self, clock: VirtualClock) -> None:
        # A round trip an order of magnitude past the step. Nothing is late
        # before the first dispatch, so step 1 goes out; everything after it
        # merges into the last step, which is never dropped and therefore
        # still carries the exact remaining delta, on both axes.
        cdp = RecordCDP(clock, cost_ms=400.0)
        await self._mouse(cdp).wheel(300, 300, -25, 600)
        wheels = [e for e in cdp.mouse_events() if e["type"] == "mouseWheel"]
        assert len(wheels) == 2
        assert sum(w["deltaY"] for w in wheels) == 600
        assert sum(w["deltaX"] for w in wheels) == -25
        assert (wheels[0]["deltaX"], wheels[0]["deltaY"]) == (-8, 183)

    async def test_zero_delay_still_sends_every_step(self, clock: VirtualClock) -> None:
        # A schedule of zero-length slots asks for no time, so no slot can be
        # late: "as fast as the transport goes", not "collapse into one event".
        cdp = RecordCDP(clock, cost_ms=8.0)
        await self._mouse(cdp, delay=0.0).wheel(300, 300, 0, 600)
        assert self._wheels(cdp) == self.INCREMENTS
        assert clock.sleeps == []

    async def test_a_single_step_scroll_is_one_event_with_the_whole_delta(
        self, clock: VirtualClock
    ) -> None:
        cdp = RecordCDP(clock, cost_ms=8.0)
        await self._mouse(cdp, steps=1).wheel(300, 300, 0, 600)
        assert self._wheels(cdp) == [600]
        assert clock.sleeps == []


@pytest.mark.parametrize("kind", ["v1", "mdn", "gan_ar"])
class TestLearnedStrategy:
    """A ``learned`` click through the controller, on each tiny export kind."""

    async def test_click_dispatches_the_sampled_path_onto_the_target(
        self, no_sleep: list[float], make_export: Callable[..., Path], kind: str
    ) -> None:
        export = make_export(kind)
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=LearnedTrajectory(weights_dir=str(export), seed=4),
            viewport=(1280, 720),
        )
        await mouse.click(1000, 150, "left", 1, target_w=90, target_h=30)

        # The same seed replays the path the controller was handed, from the
        # humanized cold start at the viewport centre. Drawn in the same order
        # as the real click — path first, then the hold — so this replica's
        # hold is the one the click spent.
        replica = LearnedTrajectory(weights_dir=str(export), seed=4)
        path = replica.generate(640, 360, 1000, 150, target_w=90, target_h=30)
        assert 1 < len(path) <= TINY_SEQ_LEN + 1

        events = cdp.mouse_events()
        assert cdp.types()[-2:] == ["mousePressed", "mouseReleased"]
        assert all((e["x"], e["y"]) == (1000, 150) for e in events[-2:])
        # Every tick that moves the cursor is one mouseMoved — intermediate
        # points clamped into the viewport, the endpoint raw — and a tick that
        # holds still sends nothing.
        expected: list[tuple[int, int]] = []
        cursor = (640, 360)
        for i, pt in enumerate(path):
            is_last = i == len(path) - 1
            xy = (pt.x, pt.y) if is_last else (min(max(pt.x, 0), 1279), min(max(pt.y, 0), 719))
            if xy != cursor:
                expected.append(xy)
                cursor = xy
        moves = [(e["x"], e["y"]) for e in events if e["type"] == "mouseMoved"]
        assert moves == expected
        assert moves[-1] == (1000, 150)
        # Every tick's time is spent: one 20 ms sleep per tick, still or not,
        # and then the button's own hold between press and release.
        hold_s = replica.click_hold_ms() / 1000.0
        assert hold_s > 0.0
        assert no_sleep == pytest.approx([0.02] * (len(path) - 1) + [hold_s])

    async def test_off_viewport_endpoint_stays_exact(
        self, no_sleep: list[float], make_export: Callable[..., Path], kind: str
    ) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=LearnedTrajectory(weights_dir=str(make_export(kind)), seed=9),
            viewport=(1280, 720),
        )
        await mouse.click(450, 820, "left", 1)
        last_move = [e for e in cdp.mouse_events() if e["type"] == "mouseMoved"][-1]
        assert (last_move["x"], last_move["y"]) == (450, 820)
        assert mouse.position == (450, 820)
        assert cdp.types()[-2:] == ["mousePressed", "mouseReleased"]


class TestStrategyIsInjectedNotResolved:
    """The strategy arrives constructed; nothing is looked up by name here.

    A misspelled key and an unsupported knob used to be ``ValueError``s
    raised here, at first tab setup — minutes into a run. Both are now
    Pydantic validation errors against the generated
    ``mouse_trajectory`` union, before anything launches; see
    ``TestMouseTrajectoryIsAWiredReceptor``.
    """

    async def test_injected_strategy_is_used_when_humanized(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: None,
            trajectory=WindMouseTrajectory(seed=3),
        )
        await mouse.click(400, 300, "left", 1)
        assert cdp.types().count("mouseMoved") > 1

    async def test_instant_strategy_is_the_off_switch(self, no_sleep: list[float]) -> None:
        # There is no separate humanize flag to disagree with the strategy:
        # ``instant`` alone means "not humanized", with every legacy behaviour.
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: None,
            trajectory=InstantTrajectory(),
            viewport=(1280, 720),
        )
        assert mouse.humanized is False
        assert mouse.position == (0, 0)  # legacy cold start, not the centre
        await mouse.click(40, 50, "left", 1)
        await mouse.wheel(40, 50, 0, 300)
        assert cdp.types() == ["mouseMoved", "mousePressed", "mouseReleased", "mouseWheel"]
        assert no_sleep == []

    def test_every_path_strategy_is_humanized(self, v1_export: Path, mdn_export: Path) -> None:
        for strategy in (
            WindMouseTrajectory(seed=1),
            BezierTrajectory(seed=1),
            LearnedTrajectory(weights_dir=str(v1_export)),
            LearnedTrajectory(weights_dir=str(mdn_export)),
        ):
            mouse = HumanMouse(RecordCDP(), session_id_getter=lambda: None, trajectory=strategy)
            assert mouse.humanized is True, strategy.trajectory_type

    async def test_no_strategy_configured_falls_back_to_instant(
        self, no_sleep: list[float]
    ) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(cdp, session_id_getter=lambda: None, trajectory=None)
        await mouse.click(40, 50, "left", 1)
        assert cdp.types() == ["mouseMoved", "mousePressed", "mouseReleased"]
