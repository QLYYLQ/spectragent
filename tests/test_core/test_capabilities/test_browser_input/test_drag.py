"""A drag holds its button on every move and releases after interruption."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from spectragent.core.capabilities.browser.input import mouse as mouse_module
from spectragent.core.capabilities.browser.input.bezier import BezierTrajectory
from spectragent.core.capabilities.browser.input.instant import InstantTrajectory
from spectragent.core.capabilities.browser.input.learned import LearnedTrajectory
from spectragent.core.capabilities.browser.input.mouse import HumanMouse
from spectragent.core.capabilities.browser.input.windmouse import WindMouseTrajectory

from .conftest import RecordCDP


@pytest.mark.parametrize("strategy", ["instant", "windmouse", "bezier", "learned"])
@pytest.mark.parametrize("button,mask", [("left", 1), ("right", 2), ("middle", 4)])
async def test_every_drag_move_keeps_the_pressed_button(
    strategy, button, mask, no_sleep, mdn_export
):
    trajectory = {
        "instant": lambda: InstantTrajectory(),
        "windmouse": lambda: WindMouseTrajectory(seed=7),
        "bezier": lambda: BezierTrajectory(seed=7),
        "learned": lambda: LearnedTrajectory(weights_dir=str(mdn_export), seed=7),
    }[strategy]()
    cdp = RecordCDP()
    mouse = HumanMouse(cdp, session_id_getter=lambda: "sid", trajectory=trajectory)
    await mouse.drag(100, 150, 500, 300, button)
    events = cdp.mouse_events()
    down = next(i for i, event in enumerate(events) if event["type"] == "mousePressed")
    assert all(e["buttons"] == 0 for e in events[:down])
    assert all(e["button"] == "none" for e in events[:down])
    assert events[down]["buttons"] == mask
    assert events[down]["button"] == button
    assert events[down + 1 : -1]
    assert all(e["type"] == "mouseMoved" and e["buttons"] == mask for e in events[down + 1 : -1])
    assert all(e["button"] == button for e in events[down + 1 : -1])
    assert events[-1] == {
        "type": "mouseReleased",
        "x": 500,
        "y": 300,
        "button": button,
        "buttons": 0,
        "clickCount": 1,
    }
    await mouse.move_to(600, 300)
    assert cdp.mouse_events()[-1]["buttons"] == 0


class InterruptedCDP(RecordCDP):
    def __init__(self, phase: str, *, cancellation: bool = False, release_fails: bool = False):
        super().__init__()
        self.phase = phase
        self.cancellation = cancellation
        self.release_fails = release_fails
        self.interrupted = False

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        await super().send(method, params, session_id, timeout)
        params = params or {}
        if params.get("type") == "mouseReleased" and self.release_fails:
            raise RuntimeError("release failed")
        phase = params.get("type")
        matched = phase == self.phase and (phase != "mouseMoved" or params.get("buttons") == 1)
        if matched and not self.interrupted:
            self.interrupted = True
            if self.cancellation:
                raise asyncio.CancelledError()
            raise ValueError("primary failure")
        return {}


@pytest.mark.parametrize("phase", ["mousePressed", "mouseMoved"])
@pytest.mark.parametrize("cancellation", [False, True])
async def test_interrupted_press_or_drag_still_releases(phase, cancellation):
    cdp = InterruptedCDP(phase, cancellation=cancellation)
    mouse = HumanMouse(cdp, session_id_getter=lambda: "sid")
    with pytest.raises(asyncio.CancelledError if cancellation else ValueError):
        await mouse.drag(100, 150, 500, 300)
    assert cdp.mouse_events()[-1]["type"] == "mouseReleased"
    assert cdp.mouse_events()[-1]["buttons"] == 0
    await mouse.move_to(200, 200)
    assert cdp.mouse_events()[-1]["buttons"] == 0


async def test_cleanup_failure_does_not_replace_the_original_error():
    mouse = HumanMouse(
        InterruptedCDP("mouseMoved", release_fails=True), session_id_getter=lambda: "sid"
    )
    with pytest.raises(ValueError, match="primary failure"):
        await mouse.drag(100, 150, 500, 300)


@pytest.mark.parametrize("release_fails", [False, True])
async def test_cancellation_during_release_waits_for_release_to_finish(release_fails):
    started, finish = asyncio.Event(), asyncio.Event()

    class SlowRelease(RecordCDP):
        async def send(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            session_id: str | None = None,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            if (params or {}).get("type") == "mouseReleased":
                started.set()
                await finish.wait()
                if release_fails:
                    await super().send(method, params, session_id, timeout)
                    raise RuntimeError("release failed")
            return await super().send(method, params, session_id, timeout)

    cdp = SlowRelease()
    mouse = HumanMouse(cdp, session_id_getter=lambda: "sid")
    task = asyncio.create_task(mouse.drag(100, 150, 500, 300))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # A second cancellation must not cancel the release command.
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cdp.mouse_events()[-1]["type"] == "mouseReleased"


@pytest.mark.parametrize("interruption", ["none", "move_error", "cancel", "move_error_then_cancel"])
async def test_an_unanswered_release_has_a_real_deadline(monkeypatch, interruption):
    monkeypatch.setattr(mouse_module, "_DRAG_RELEASE_TIMEOUT_S", 0.03)
    started, stopped = asyncio.Event(), asyncio.Event()

    class UnansweredRelease(RecordCDP):
        async def send(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            session_id: str | None = None,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            await super().send(method, params, session_id, timeout)
            params = params or {}
            if (
                interruption.startswith("move_error")
                and params.get("type") == "mouseMoved"
                and params.get("buttons")
            ):
                raise ValueError("primary movement failure")
            if params.get("type") == "mouseReleased":
                started.set()
                try:
                    await asyncio.Future()  # Never acknowledged, not manually released by the test.
                finally:
                    stopped.set()
            return {}

    cdp = UnansweredRelease()
    mouse = HumanMouse(cdp, session_id_getter=lambda: "sid")
    began = time.monotonic()
    task = asyncio.create_task(mouse.drag(100, 150, 500, 300))
    await asyncio.wait_for(started.wait(), 0.5)
    if interruption in ("cancel", "move_error_then_cancel"):
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    expected = {
        "none": TimeoutError,
        "move_error": ValueError,
        "cancel": asyncio.CancelledError,
        "move_error_then_cancel": asyncio.CancelledError,
    }[interruption]
    with pytest.raises(expected):
        await asyncio.wait_for(task, 0.5)
    assert time.monotonic() - began < 0.4
    assert stopped.is_set(), "the bounded cleanup must cancel its unanswered CDP call"
    assert cdp.types().count("mouseReleased") == 1
