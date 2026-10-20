"""Capability side of ``browser.click_coordinate``: its space and its target box.

* The screenshot fragment reports the image's own pixel size next to the CSS
  viewport, so an agent can convert a point read off the image — the two
  differ by the device scale factor — without a second CDP call.
* A humanized coordinate click hit-tests the point and hands the trajectory a
  box centred on it (``learned`` is conditioned on the target size); the instant
  jump stays the legacy three input events with no extra calls.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.click_coordinate import (
    ClickCoordinateAction,
)
from spectragent.core.capabilities.browser.input import WindMouseTrajectory
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    ClickCoordinateEvent,
)
from spectragent.core.capabilities.browser.watchdogs.screenshot_watchdog import (
    ScreenshotWatchdog,
)
from tests.test_core.test_capabilities.test_browser_action_click_robustness import (
    FakeCDP,
    _Call,
    _outcome,
)


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


class _ScreenshotCDP:
    def __init__(self, png: bytes) -> None:
        self._data = base64.b64encode(png).decode()
        self.sent: list[str] = []

    async def send(self, method: str, params: Any = None, *, session_id: Any = None) -> Any:
        self.sent.append(method)
        return {"data": self._data}


class _HitTestCDP(FakeCDP):
    """FakeCDP plus a hit-test answering with one element's border quad."""

    def __init__(self, border: list[float]) -> None:
        super().__init__()
        self._border = border

    async def send(
        self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None
    ) -> dict[str, Any]:
        if method == "DOM.getNodeForLocation":
            self.calls.append(_Call(method=method, params=params or {}))
            return {"backendNodeId": 7}
        if method == "DOM.getBoxModel":
            self.calls.append(_Call(method=method, params=params or {}))
            return {"model": {"border": list(self._border)}}
        return await super().send(method, params, session_id=session_id)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "spectragent.core.capabilities.browser.input._schedule.asyncio.sleep", fake_sleep
    )


class TestCoordinateSpaceIsDeclared:
    def test_descriptor_names_css_viewport_pixels(self) -> None:
        params = ClickCoordinateAction().descriptor().params
        assert set(params) == {"coordinate_x", "coordinate_y"}
        assert all("CSS px" in spec for spec in params.values())

    async def test_screenshot_reports_image_and_viewport_sizes(self) -> None:
        cdp = _ScreenshotCDP(_png(2560, 1440))
        wd = ScreenshotWatchdog(cdp, viewport=(1280, 720))  # type: ignore[arg-type]
        summary = await wd._on_browser_state_request(
            BrowserStateRequestEvent(fragments=frozenset({"browser.screenshot"}))
        )
        assert summary is not None
        shot = summary.fragments["browser.screenshot"]
        assert (shot.image_width, shot.image_height) == (2560, 1440)
        assert (shot.viewport_width, shot.viewport_height) == (1280, 720)
        # Measured from the bytes: no layout query. The one other call is the
        # document stamp read after the capture (M18, §4.14).
        assert cdp.sent == ["Page.captureScreenshot", "Page.getFrameTree"]


class TestTargetBox:
    async def test_instant_click_sends_no_hit_test(self) -> None:
        cdp = _HitTestCDP(border=[0, 0, 100, 0, 100, 40, 0, 40])
        # A spent effect budget: the click's own effect probes send nothing, so
        # the log is the click's (they are pinned in test_browser_actions/).
        wd = ActionWatchdog(input_platform="linux", cdp=cdp, action_effect_timeout_s=0.0)  # type: ignore[arg-type]
        result = await wd._on_click_coordinate(
            ClickCoordinateEvent(coordinate_x=30, coordinate_y=20)
        )
        assert _outcome(result) == {"clicked": True, "x": 30, "y": 20}
        assert [c.method for c in cdp.calls] == ["Input.dispatchMouseEvent"] * 3

    async def test_humanized_click_gets_a_box_centred_on_the_point(
        self, no_sleep: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Element spans x 0..100, y 0..40; the point (30, 20) is 30 px from
        # the left edge and 20 px from top and bottom → a 60 x 40 box.
        cdp = _HitTestCDP(border=[0, 0, 100, 0, 100, 40, 0, 40])
        wd = ActionWatchdog(
            input_platform="linux",
            cdp=cdp,  # type: ignore[arg-type]
            mouse_trajectory=WindMouseTrajectory(seed=1),
        )
        seen: dict[str, float] = {}
        original = wd._mouse.click

        async def _spy(x: float, y: float, *args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return await original(x, y, *args, **kwargs)

        monkeypatch.setattr(wd._mouse, "click", _spy)
        result = await wd._on_click_coordinate(
            ClickCoordinateEvent(coordinate_x=30, coordinate_y=20)
        )
        assert _outcome(result) == {"clicked": True, "x": 30, "y": 20}
        assert (seen["target_w"], seen["target_h"]) == (60.0, 40.0)

    async def test_hit_test_failure_is_size_unknown_not_a_failed_click(
        self, no_sleep: None
    ) -> None:
        cdp = FakeCDP(raise_on={"DOM.getNodeForLocation"})
        wd = ActionWatchdog(
            input_platform="linux",
            cdp=cdp,  # type: ignore[arg-type]
            mouse_trajectory=WindMouseTrajectory(seed=1),
        )
        assert await wd._target_box_at(5, 5) == (0.0, 0.0)
        result = await wd._on_click_coordinate(ClickCoordinateEvent(coordinate_x=5, coordinate_y=5))
        assert _outcome(result) == {"clicked": True, "x": 5, "y": 5}
