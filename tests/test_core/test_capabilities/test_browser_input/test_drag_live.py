"""Real controls must accept a production drag, not merely its CDP commands."""

from __future__ import annotations

import asyncio
import json

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.input.instant import InstantTrajectory
from spectragent.core.capabilities.browser.input.windmouse import WindMouseTrajectory
from spectragent.schemas.action import Action

pytestmark = pytest.mark.requires_chrome

STYLE = """
body{margin:0}input{appearance:none;background:transparent;position:absolute;left:120px;
top:160px;width:600px;height:24px;margin:0}input::-webkit-slider-runnable-track{height:8px;
background:#aaa}input::-webkit-slider-thumb{appearance:none;box-sizing:border-box;width:24px;
height:24px;border:2px solid #16506c;background:cyan;border-radius:50%;margin-top:-8px}
"""
HTML = """
<input id="slider" type="range" min="0" max="100" step="1" value="20">
<div id="target" style="position:absolute;left:570px;top:320px;width:160px;height:160px;background:orange"></div>
<div id="block" style="position:absolute;left:180px;top:360px;width:80px;height:80px;background:blue;touch-action:none"></div>
"""
HANDLERS = """
window.moves=[];window.dropped=false;
document.addEventListener('pointermove',e=>moves.push(e.buttons));
const block=document.getElementById('block');let start=null;
block.onpointerdown=e=>{start={x:e.clientX,y:e.clientY,left:block.offsetLeft,top:block.offsetTop};block.setPointerCapture(e.pointerId)};
block.onpointermove=e=>{if(start){block.style.left=(start.left+e.clientX-start.x)+'px';block.style.top=(start.top+e.clientY-start.y)+'px'}};
block.onpointerup=e=>{if(start){const a=block.getBoundingClientRect(),b=document.getElementById('target').getBoundingClientRect();window.dropped=a.left>=b.left&&a.right<=b.right&&a.top>=b.top&&a.bottom<=b.bottom;start=null}};
true
"""


async def read(cap, expression):
    result = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=cap.default_session_id,
        timeout=5,
    )
    return result["result"].get("value")


@pytest.mark.parametrize("humanized", [False, True], ids=["instant", "windmouse"])
async def test_drag_changes_native_range_and_completes_pointer_capture_drop(humanized):
    cap = BrowserCapability(viewport_width=900, viewport_height=600)
    cap.backend = LocalChromeProvider(headless=True, force_device_scale_factor=1.0)  # type: ignore[assignment]
    cap.mouse_trajectory = WindMouseTrajectory(seed=7) if humanized else InstantTrajectory()
    try:
        await cap.setup(negotiation={"browser.screenshot"})
        await read(
            cap,
            f"document.head.innerHTML={json.dumps('<style>' + STYLE + '</style>')};document.body.innerHTML={json.dumps(HTML)};{HANDLERS}",
        )
        result = await cap.execute_action(
            Action(
                name="browser.drag",
                params={"start_x": 247, "start_y": 172, "end_x": 593, "end_y": 172},
            )
        )
        assert result.success, result.error
        assert await read(cap, "Number(document.getElementById('slider').value)") == 80
        assert 1 in await read(cap, "moves")
        await read(cap, "moves=[]")
        result = await cap.execute_action(
            Action(
                name="browser.drag",
                params={"start_x": 220, "start_y": 400, "end_x": 650, "end_y": 400},
            )
        )
        assert result.success, result.error
        assert await read(cap, "dropped") is True
        assert 1 in await read(cap, "moves")
    finally:
        await cap.teardown()


async def test_unanswered_release_is_an_action_failure_and_does_not_block_teardown(monkeypatch):
    from spectragent.core.capabilities.browser.input import mouse as mouse_module

    monkeypatch.setattr(mouse_module, "_DRAG_RELEASE_TIMEOUT_S", 0.03)
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await cap.setup(negotiation={"browser.screenshot"})
        send = cap.cdp.send

        async def unanswered(method, params=None, *args, **kwargs):
            if (
                method == "Input.dispatchMouseEvent"
                and (params or {}).get("type") == "mouseReleased"
            ):
                await asyncio.Future()
            return await send(method, params, *args, **kwargs)

        monkeypatch.setattr(cap.cdp, "send", unanswered)
        result = await asyncio.wait_for(
            cap.execute_action(
                Action(
                    name="browser.drag",
                    params={"start_x": 100, "start_y": 100, "end_x": 200, "end_y": 200},
                )
            ),
            1,
        )
        assert not result.success
        assert result.error and "TimeoutError" in result.error
    finally:
        await asyncio.wait_for(cap.teardown(), 10)
