"""HumanMouse controller — instant-strategy equivalence + humanized behavior."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.input.bezier import BezierTrajectory
from spectragent.core.capabilities.browser.input.instant import InstantTrajectory
from spectragent.core.capabilities.browser.input.learned import LearnedTrajectory
from spectragent.core.capabilities.browser.input.mouse import HumanMouse
from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint
from spectragent.core.capabilities.browser.input.windmouse import WindMouseTrajectory

from .conftest import TINY_SEQ_LEN


class RecordCDP:
    """Records every ``send`` call; returns empty results.

    Structurally satisfies ``agent_cdp.CDPCommandProtocol`` — including
    the positional ``session_id`` and the event-subscription half — so
    HumanMouse can be constructed with it without a type error.
    """

    is_connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        return {}

    def on_event(self, method: str, callback: Any) -> None:
        return None

    def off_event(self, method: str, callback: Any) -> None:
        return None

    def mouse_events(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "Input.dispatchMouseEvent"]

    def types(self) -> list[str]:
        return [p["type"] for p in self.mouse_events()]


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Patch asyncio.sleep in the mouse module to a recording no-op."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(
        "spectragent.core.capabilities.browser.input.mouse.asyncio.sleep",
        fake_sleep,
    )
    return slept


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

    def __init__(self, points: list[TrajectoryPoint]) -> None:
        self.points = points

    @property
    def humanized(self) -> bool:
        return True

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
        # humanized cold start at the viewport centre.
        path = LearnedTrajectory(weights_dir=str(export), seed=4).generate(
            640, 360, 1000, 150, target_w=90, target_h=30
        )
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
        # Every tick's time is spent: one 20 ms sleep per tick, still or not.
        assert no_sleep == pytest.approx([0.02] * (len(path) - 1))

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
