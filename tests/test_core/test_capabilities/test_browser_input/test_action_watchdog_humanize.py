"""Locks the ActionWatchdog → HumanMouse wiring (humanized vs instant end-to-end).

Reuses the canary's FakeCDP recorder so a regression that forgets to thread
the ``mouse_trajectory`` strategy from the capability into the watchdog — the
one thing that decides whether the cursor is humanized — is caught.
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser.dom.models import DOMRect
from spectragent.core.capabilities.browser.input import InstantTrajectory, WindMouseTrajectory
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
    ActionWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.events import ClickElementEvent
from tests.test_core.test_capabilities.test_browser_action_click_robustness import (
    FakeCDP,
    _make_node,
)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "spectragent.core.capabilities.browser.input.mouse.asyncio.sleep",
        fake_sleep,
    )


async def test_humanized_click_emits_curved_path(no_sleep: None) -> None:
    node = _make_node(42, DOMRect(x=400.0, y=300.0, width=100.0, height=40.0))
    # Live rect center = (600 + 40/2, 400 + 20/2) = (620, 410).
    cdp = FakeCDP(rect={"x": 600.0, "y": 400.0, "w": 40.0, "h": 20.0})
    wd = ActionWatchdog(
        cdp=cdp,  # type: ignore[arg-type]
        selector_map_getter=lambda: {42: node},
        mouse_trajectory=WindMouseTrajectory(seed=1),
        viewport=(1280, 720),
    )

    result = await wd._on_click_element(ClickElementEvent(backend_node_id=42))
    assert result == {"clicked": True, "x": 620, "y": 410}

    mouse_events = [c for c in cdp.calls if c.method == "Input.dispatchMouseEvent"]
    moves = [c for c in mouse_events if c.params.get("type") == "mouseMoved"]
    # Humanized → many intermediate moves, not a single jump.
    assert len(moves) > 1
    # Press + release still land exactly on the resolved center.
    press = next(c for c in mouse_events if c.params["type"] == "mousePressed")
    release = next(c for c in mouse_events if c.params["type"] == "mouseReleased")
    assert (press.params["x"], press.params["y"]) == (620, 410)
    assert (release.params["x"], release.params["y"]) == (620, 410)


async def test_default_watchdog_click_is_single_jump() -> None:
    """Default (no strategy → instant) watchdog == legacy single mouseMoved."""
    node = _make_node(42, DOMRect(x=400.0, y=300.0, width=100.0, height=40.0))
    cdp = FakeCDP(rect={"x": 600.0, "y": 400.0, "w": 40.0, "h": 20.0})
    wd = ActionWatchdog(
        cdp=cdp,  # type: ignore[arg-type]
        selector_map_getter=lambda: {42: node},
    )

    await wd._on_click_element(ClickElementEvent(backend_node_id=42))

    moves = [
        c
        for c in cdp.calls
        if c.method == "Input.dispatchMouseEvent" and c.params.get("type") == "mouseMoved"
    ]
    assert len(moves) == 1  # one jump, no trajectory


async def test_humanized_follows_the_strategy() -> None:
    cdp = FakeCDP()
    windmouse = ActionWatchdog(cdp=cdp, mouse_trajectory=WindMouseTrajectory())  # type: ignore[arg-type]
    instant = ActionWatchdog(cdp=cdp, mouse_trajectory=InstantTrajectory())  # type: ignore[arg-type]
    unset = ActionWatchdog(cdp=cdp)  # type: ignore[arg-type]
    assert windmouse._mouse.humanized is True
    assert instant._mouse.humanized is False
    assert unset._mouse.humanized is False


@pytest.mark.parametrize(
    ("strategy", "approaches"),
    [(InstantTrajectory(), False), (WindMouseTrajectory(seed=1), True)],
    ids=["instant", "windmouse"],
)
async def test_cursor_approach_before_type_follows_the_strategy(
    no_sleep: None, strategy: object, approaches: bool
) -> None:
    """type/select only move the cursor first when the strategy is humanized."""
    cdp = FakeCDP(rect={"x": 600.0, "y": 400.0, "w": 40.0, "h": 20.0})
    wd = ActionWatchdog(cdp=cdp, mouse_trajectory=strategy)  # type: ignore[arg-type]
    await wd._approach_node(42)
    moves = [
        c
        for c in cdp.calls
        if c.method == "Input.dispatchMouseEvent" and c.params.get("type") == "mouseMoved"
    ]
    assert bool(moves) is approaches
    if approaches:
        assert (moves[-1].params["x"], moves[-1].params["y"]) == (620, 410)
