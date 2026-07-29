"""HumanMouse controller — off-mode equivalence + humanized behavior."""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities.browser.input.mouse import HumanMouse
from spectragent.core.capabilities.browser.input.windmouse import WindMouseTrajectory


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
    """humanize=False must reproduce the legacy instant-jump behavior."""

    async def test_click_is_three_events_at_target_no_sleep(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(cdp, session_id_getter=lambda: None)  # humanize off
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
            humanize=True,
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
            humanize=True,
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
            humanize=True,
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
            humanize=True,
            viewport=(1280, 720),
        )
        assert mouse.position == (640, 360)

    async def test_drag_press_move_release(self, no_sleep: list[float]) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: "sid",
            trajectory=WindMouseTrajectory(seed=2),
            humanize=True,
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


class TestStrategyIsInjectedNotResolved:
    """The strategy arrives constructed; nothing is looked up by name here.

    A misspelled key and an unsupported knob used to be ``ValueError``s
    raised here, at first tab setup — minutes into a run. Both are now
    Pydantic validation errors against the generated
    ``mouse_trajectory`` union, before anything launches; see
    ``TestMouseTrajectoryIsAWiredReceptor``.
    """

    async def test_injected_strategy_is_used_when_humanized(
        self, no_sleep: list[float]
    ) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: None,
            trajectory=WindMouseTrajectory(seed=3),
            humanize=True,
        )
        await mouse.click(400, 300, "left", 1)
        assert cdp.types().count("mouseMoved") > 1

    async def test_strategy_ignored_when_humanize_off(
        self, no_sleep: list[float]
    ) -> None:
        # With humanize off the forced ``instant`` strategy wins, so the
        # configured one never runs — byte-for-byte the legacy jump.
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: None,
            trajectory=WindMouseTrajectory(seed=7),
            humanize=False,
        )
        await mouse.click(40, 50, "left", 1)
        assert cdp.types() == ["mouseMoved", "mousePressed", "mouseReleased"]

    async def test_no_strategy_configured_falls_back_to_instant(
        self, no_sleep: list[float]
    ) -> None:
        cdp = RecordCDP()
        mouse = HumanMouse(
            cdp, session_id_getter=lambda: None, trajectory=None, humanize=True
        )
        await mouse.click(40, 50, "left", 1)
        assert cdp.types() == ["mouseMoved", "mousePressed", "mouseReleased"]
