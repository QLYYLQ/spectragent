"""Literal named-key cancellation releases the key within a finite deadline."""

import asyncio
import time
from typing import Any

import pytest

from spectragent.core.capabilities.browser.input import keyboard as keyboard_module
from spectragent.core.capabilities.browser.input.keyboard import HumanKeyboard

from .conftest import RecordCDP


@pytest.mark.parametrize(
    "keyup_stalls,keydown_fails", [(False, False), (True, False), (True, True)]
)
async def test_cancelled_keydown_still_attempts_bounded_keyup(
    monkeypatch, keyup_stalls, keydown_fails
):
    monkeypatch.setattr(keyboard_module, "_KEY_RELEASE_TIMEOUT_S", 0.03)
    down_started, up_started, up_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class ControlledCDP(RecordCDP):
        async def send(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            session_id: str | None = None,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            await super().send(method, params, session_id, timeout)
            if (params or {}).get("type") == "keyDown":
                down_started.set()
                if keydown_fails:
                    raise ValueError("primary keydown failure")
                await asyncio.Future()
            if (params or {}).get("type") == "keyUp":
                up_started.set()
                try:
                    if keyup_stalls:
                        await asyncio.Future()
                finally:
                    up_stopped.set()
            return {}

    cdp = ControlledCDP()
    keyboard = HumanKeyboard(cdp, input_platform="macos")
    began = time.monotonic()
    task = asyncio.create_task(
        keyboard.press_key(
            {"key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65},
            modifiers=4,
            char_text="a",
            session_id="sid",
        )
    )
    await asyncio.wait_for(down_started.wait(), 0.5)
    task.cancel()
    await asyncio.wait_for(up_started.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)
    assert time.monotonic() - began < 0.4
    assert up_stopped.is_set()
    assert [event["type"] for event in cdp.key_events()] == ["keyDown", "keyUp"]
