"""Clean screenshot evidence and reproducible model presentation share one capture."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from spectragent.agents.browser_use import BrowserUseDomVisionAgent
from spectragent.agents.skyvern import SkyvernDomAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.cursor import ScreenshotCursor
from spectragent.core.capabilities.browser.input.mouse import HumanMouse, PointerPosition
from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint
from spectragent.core.capabilities.browser.observations import DOMObs, ScreenshotObs
from spectragent.core.capabilities.browser.restoration import measure
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.base import WatchdogContext
from spectragent.core.capabilities.browser.watchdogs.events import BrowserStateRequestEvent
from spectragent.core.capabilities.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
from spectragent.core.llm.messages import ContentPartImageParam
from spectragent.core.loops.coordinate_frames import step_frame
from spectragent.core.trace.encoding import decode_fragment, encode_fragment
from spectragent.schemas.observation.image import read_model_image
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.action_spaces import browser_space


def png(color: str = "white", size: tuple[int, int] = (240, 160)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def shot(*, cursor: ScreenshotCursor | None = None) -> ScreenshotObs:
    return ScreenshotObs(
        image_bytes=png(),
        viewport_width=240,
        viewport_height=160,
        image_width=240,
        image_height=160,
        cursor=cursor,
    )


class CDP:
    is_connected = True

    def on_event(self, method: str, callback: Any) -> None:
        return None

    def off_event(self, method: str, callback: Any) -> None:
        return None

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_move: int | None = None
        self.moves = 0

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        params = params or {}
        self.calls.append((method, params or {}))
        if method == "Input.dispatchMouseEvent" and params["type"] == "mouseMoved":
            self.moves += 1
            if self.moves == self.fail_move:
                raise TimeoutError("no movement acknowledgement")
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(png()).decode()}
        return {}


async def test_confirmed_position_never_guesses_a_target_or_wheel_location() -> None:
    cdp = CDP()
    state = PointerPosition()
    mouse = HumanMouse(cdp, session_id_getter=lambda: "tab", pointer_position=state)
    assert mouse.observed_position is None
    await mouse.wheel(100, 80, 0, 120)
    assert mouse.observed_position is None
    await mouse.move_to(20, 30)
    assert mouse.observed_position == (20, 30)
    await mouse.wheel(50, 60, 0, 120)
    assert mouse.observed_position == (20, 30)
    cdp.fail_move = cdp.moves + 1
    with pytest.raises(TimeoutError):
        await mouse.move_to(200, 100)
    assert state.snapshot() is None
    cdp.fail_move = None
    await mouse.move_to(70, 80)
    assert state.snapshot() == (70, 80)
    assert HumanMouse(cdp, session_id_getter=lambda: "another").observed_position is None


async def test_partial_trajectory_lost_acknowledgement_makes_current_position_unknown() -> None:
    class Path:
        humanized = True

        def generate(self, *args, **kwargs):
            return [TrajectoryPoint(10, 10), TrajectoryPoint(20, 20), TrajectoryPoint(30, 30)]

    cdp = CDP()
    cdp.fail_move = 2
    mouse = HumanMouse(cdp, session_id_getter=lambda: "tab", trajectory=Path())  # type: ignore[arg-type]
    with pytest.raises(TimeoutError):
        await mouse.move_to(30, 30)
    assert mouse.observed_position is None


@pytest.mark.parametrize("enabled", [False, True])
async def test_watchdogs_share_only_their_tab_confirmed_position(enabled: bool) -> None:
    cap = BrowserCapability(screenshot_cursor=enabled, screenshot_cursor_size=40)
    cdp = CDP()
    ctx = WatchdogContext(
        cdp=cdp,
        profile=cap.profile_config,
        input_platform="macos",
        viewport=(240, 160),
        screenshot_cursor=cap.screenshot_cursor,
        screenshot_cursor_size=cap.screenshot_cursor_size,
    )  # type: ignore[arg-type]
    action = ActionWatchdog.build(ctx)
    screenshot = ScreenshotWatchdog.build(ctx)
    event = BrowserStateRequestEvent(fragments=frozenset({"browser.screenshot"}))
    before = await screenshot._on_browser_state_request(event)
    assert before is not None
    first = before.fragments["browser.screenshot"]
    assert isinstance(first, ScreenshotObs)
    assert (first.cursor is not None) == enabled
    if enabled:
        assert first.cursor is not None and first.cursor.position is None
    # Drive the real controller, not an invented action target snapshot.
    await action._mouse.move_to(80, 70)
    after = await screenshot._on_browser_state_request(event)
    assert after is not None
    captured = after.fragments["browser.screenshot"]
    assert isinstance(captured, ScreenshotObs)
    assert captured.image_bytes == png()
    assert captured.trace_binary() == (".png", png())
    assert ctx.pointer_position.snapshot() == (80, 70)
    if enabled:
        assert captured.cursor == ScreenshotCursor(position=(80, 70), size_css_px=40)
        assert read_model_image(captured) != captured.image_bytes
    else:
        assert read_model_image(captured) is captured.image_bytes
    other = WatchdogContext(cdp=cdp, profile=cap.profile_config)  # type: ignore[arg-type]
    assert other.pointer_position.snapshot() is None
    assert other.pointer_position is not ctx.pointer_position
    assert len([call for call in cdp.calls if call[0] == "Page.captureScreenshot"]) == 2


def test_fragment_trace_roundtrip_and_cache_use_all_presentation_inputs() -> None:
    original = shot(cursor=ScreenshotCursor(position=(50, 40)))
    model_image = original.model_image_bytes()
    assert original.model_image_bytes() is model_image
    encoded = encode_fragment(0, original)
    assert encoded is not None and encoded.artifact is not None
    payload = json.loads(encoded.file.data)
    assert "image_bytes" not in payload
    assert payload["cursor"]["position"] == [50, 40]
    restored = decode_fragment(encoded.key, ScreenshotObs, payload, encoded.artifact.data)
    assert restored.image_bytes == original.image_bytes
    assert restored.model_image_bytes() == model_image
    original.cursor = ScreenshotCursor(position=(90, 80))
    assert original.model_image_bytes() != model_image
    previous = original.model_image_bytes()
    original.viewport_width = 480
    assert original.model_image_bytes() != previous
    previous = original.model_image_bytes()
    original.image_bytes = png("blue")
    assert original.model_image_bytes() != previous
    original.cursor = None
    assert original.model_image_bytes() is original.image_bytes


def test_restoration_measures_clean_scene_when_only_cursor_differs() -> None:
    before = shot(cursor=ScreenshotCursor(position=(30, 30)))
    after = shot(cursor=ScreenshotCursor(position=(160, 100)))
    assert before.model_image_bytes() != after.model_image_bytes()
    evidence = measure(
        SimpleNamespace(screenshot=before),
        SimpleNamespace(screenshot=after),
        capability_id="browser",
        document_misses=None,
        media=None,
    )
    assert evidence.screenshot_distance == 0


@pytest.mark.parametrize("agent", [BrowserUseDomVisionAgent(), UITarsAgent()])
async def test_existing_image_consumers_send_model_view_on_unchanged_grid(agent) -> None:
    image = shot(cursor=ScreenshotCursor(position=(30, 40)))
    observation = SimpleNamespace(
        task=SimpleNamespace(user_request="click", follow_ups=[]),
        history=SimpleNamespace(items=[]),
        screenshot=image,
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        dom=DOMObs(),
    )
    messages = await agent.build_react_prompt(
        observation,
        context=ReactPromptContext(step=0, phase="policy"),
    )
    images = [
        part
        for message in messages
        if isinstance(message.content, list)
        for part in message.content
        if isinstance(part, ContentPartImageParam)
    ]
    assert len(images) == 1
    assert base64.b64decode(images[0].image_url.url.split(",", 1)[1]) == image.model_image_bytes()
    # The cursor drawn on the model view does not move the frame the framework
    # converts the reply's points with (``core/loops/coordinate_frames.py``).
    space = browser_space("click_coordinate", coordinate_space="screenshot_pixels")
    grid_with_cursor = step_frame(space, observation)
    assert grid_with_cursor is not None and grid_with_cursor.grid is not None
    observation.screenshot = image.model_copy(update={"cursor": None})
    assert step_frame(space, observation) == grid_with_cursor


async def test_skyvern_remains_text_when_screenshot_is_available() -> None:
    observation = SimpleNamespace(
        task=SimpleNamespace(user_request="click", follow_ups=[]),
        screenshot=shot(cursor=ScreenshotCursor(position=(20, 20))),
        dom=DOMObs(),
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
    )
    messages = await SkyvernDomAgent().build_react_prompt(
        observation,
        context=ReactPromptContext(step=0, phase="policy"),
    )
    assert all(isinstance(message.content, str) for message in messages)


def test_public_model_image_contract_refuses_an_unimplemented_fragment() -> None:
    with pytest.raises(TypeError, match="ModelImage.model_image_bytes"):
        read_model_image(SimpleNamespace(image_bytes=png()))


async def test_cancelled_movement_is_unknown_and_renders_no_cursor() -> None:
    import asyncio

    started = asyncio.Event()

    class Unanswered(CDP):
        async def send(self, method, params=None, session_id=None, timeout=None):
            if method == "Input.dispatchMouseEvent" and (params or {}).get("x") == 90:
                started.set()
                await asyncio.Future()
            return await super().send(method, params, session_id, timeout)

    state = PointerPosition()
    mouse = HumanMouse(Unanswered(), session_id_getter=lambda: "tab", pointer_position=state)
    await mouse.move_to(30, 40)
    assert state.snapshot() == (30, 40)
    task = asyncio.create_task(mouse.move_to(90, 100))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mouse.observed_position is None
    image = shot(cursor=ScreenshotCursor(position=state.snapshot()))
    assert image.model_image_bytes() is image.image_bytes


async def test_debug_overlay_callback_failure_does_not_erase_confirmed_movement() -> None:
    async def broken_debug_overlay(x: int, y: int) -> None:
        raise RuntimeError("debug page injection failed")

    mouse = HumanMouse(CDP(), session_id_getter=lambda: "tab", move_callback=broken_debug_overlay)
    await mouse.move_to(30, 40)
    assert mouse.observed_position == (30, 40)
