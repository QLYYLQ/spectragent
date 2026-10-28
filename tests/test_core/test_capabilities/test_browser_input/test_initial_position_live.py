"""A real Chrome confirms learned motion's origin before the first observation."""

from __future__ import annotations

from pathlib import Path

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.input.learned import LearnedTrajectory
from spectragent.core.capabilities.browser.observations import BrowserCaptureStateObs, ScreenshotObs
from spectragent.schemas.action import Action
from spectragent.schemas.observation.capture_state import PointerState

pytestmark = pytest.mark.requires_chrome


async def test_learned_first_click_uses_confirmed_origin_and_new_tabs_initialize_independently(
    mdn_export: Path,
) -> None:
    cap = BrowserCapability(screenshot_cursor=True, settle_quiet_ms=30)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    cap.mouse_trajectory = LearnedTrajectory(weights_dir=str(mdn_export), seed=4)  # type: ignore[assignment]
    fragments = {"browser.screenshot", "browser.capture_state"}
    try:
        await cap.setup(negotiation=fragments)
        await cap.begin_observation(0)
        shot = await cap.resolve_fragment("browser.screenshot")
        state = await cap.resolve_fragment("browser.capture_state")
        assert isinstance(shot, ScreenshotObs) and shot.cursor is not None
        assert shot.cursor.position == (640, 360)
        assert isinstance(state, BrowserCaptureStateObs)
        assert state.pointer == PointerState(status="known", x=640, y=360)
        original_target = cap._session_to_target[cap.default_session_id]
        await cap.cdp.send(
            "Runtime.evaluate",
            {
                "expression": """window.moves = []; window.presses = [];
                    document.addEventListener('mousemove', e => moves.push([e.clientX,e.clientY]));
                    document.addEventListener('mousedown', e => presses.push([e.clientX,e.clientY]));""",
            },
            session_id=cap.default_session_id,
        )
        result = await cap.execute_action(
            Action(
                name="browser.click_coordinate",
                params={"coordinate_x": 1000, "coordinate_y": 150},
            )
        )
        assert result.success, result.error
        events = await cap.cdp.send(
            "Runtime.evaluate",
            {"expression": "({moves, presses})", "returnByValue": True},
            session_id=cap.default_session_id,
        )
        value = events["result"]["value"]
        assert len(value["moves"]) > 1
        assert value["moves"][-1] == [1000, 150]
        assert value["presses"] == [[1000, 150]]
        created = await cap.cdp.send("Target.createTarget", {"url": "about:blank"})
        await cap.switch_tab(created["targetId"])
        await cap.begin_observation(1)
        fresh = await cap.resolve_fragment("browser.capture_state")
        assert isinstance(fresh, BrowserCaptureStateObs)
        assert fresh.pointer == PointerState(status="known", x=640, y=360)
        await cap.switch_tab(original_target)
        await cap.begin_observation(2)
        original = await cap.resolve_fragment("browser.capture_state")
        assert isinstance(original, BrowserCaptureStateObs)
        assert original.pointer == PointerState(status="known", x=1000, y=150)
    finally:
        await cap.teardown()
