"""Locks the ActionWatchdog → HumanMouse wiring (humanize on/off end-to-end).

Reuses the canary's FakeCDP recorder so a regression that forgets to thread
``humanize_mouse`` from the capability factory into the watchdog is caught.
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser.dom.models import DOMRect
from spectragent.core.capabilities.browser.input import WindMouseTrajectory
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
        humanize_mouse=True,
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
    """Default (humanize off) watchdog == legacy single mouseMoved."""
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


async def test_humanize_flag_exposed_on_mouse() -> None:
    cdp = FakeCDP()
    on = ActionWatchdog(cdp=cdp, humanize_mouse=True)  # type: ignore[arg-type]
    off = ActionWatchdog(cdp=cdp)  # type: ignore[arg-type]
    assert on._mouse.humanized is True
    assert off._mouse.humanized is False
