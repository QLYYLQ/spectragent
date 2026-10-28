"""Real hover changes cannot redirect the press onto a replacement target."""

from __future__ import annotations

import json
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.input.trajectory import TrajectoryPoint
from spectragent.schemas.action import Action

pytestmark = pytest.mark.requires_chrome


class ThroughMenu:
    """The cursor crosses a real hover trigger before reaching its exact target."""

    humanized = True

    def generate(self, sx, sy, ex, ey, **kwargs):
        return [TrajectoryPoint(200, 100), TrajectoryPoint(round(ex), round(ey), 80)]

    def click_hold_ms(self):
        return 0.0

    def aim_offset_px(self, width, height):
        return 0.0, 0.0


async def read(cap: BrowserCapability, expression: str) -> Any:
    result = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=cap.default_session_id,
        timeout=5,
    )
    assert not result.get("exceptionDetails"), result
    return result["result"].get("value")


async def scene(cap: BrowserCapability, *, scope: str, replacement: bool, editable: bool = False):
    script = """
    document.body.style.margin = '0';
    document.body.innerHTML = '<div id="trigger" style="position:absolute;left:150px;top:50px;'
      + 'width:100px;height:100px;background:lightblue">Menu</div>';
    window.events = []; window.changed = false;
    let root = document.body;
    if (SCOPE === 'shadow') {
      const host = document.createElement('div');
      document.body.appendChild(host); root = host.attachShadow({mode:'open'});
    } else if (SCOPE === 'frame') {
      const frame = document.createElement('iframe');
      frame.style.cssText = 'position:absolute;left:500px;top:200px;width:300px;height:200px;border:0';
      document.body.appendChild(frame);
      root = frame.contentDocument.body;
      root.style.margin = '0';
    }
    const target = document.createElement(EDITABLE ? 'input' : 'button');
    target.id = 'target'; target.setAttribute('aria-label', 'Search');
    target.style.cssText = 'position:absolute;box-sizing:border-box;width:100px;height:60px;'
      + (SCOPE === 'frame' ? 'left:100px;top:70px' : 'left:600px;top:270px');
    if (!EDITABLE) target.innerHTML = '<span tabindex="-1" onclick="void 0">Search</span>';
    root.appendChild(target); window.target = target;
    const docs = new Set([document, target.ownerDocument]);
    for (const doc of docs) {
      for (const name of ['mousedown','mouseup','click','keydown','input']) {
        doc.addEventListener(name, e => events.push(name), true);
      }
    }
    document.getElementById('trigger').addEventListener('mouseenter', () => {
      changed = true;
      if (REPLACEMENT) {
        const cover = document.createElement('button');
        cover.setAttribute('aria-label', 'Search'); cover.textContent = 'Search';
        cover.id = 'replacement';
        cover.style.cssText = 'position:absolute;left:600px;top:270px;width:100px;height:60px;z-index:10';
        document.body.appendChild(cover);
      } else {
        target.innerHTML = '<span tabindex="-1" onclick="void 0"><b>Search</b></span>';
      }
    }, {once:true});
    true;
    """
    script = (
        script.replace("SCOPE", json.dumps(scope))
        .replace("REPLACEMENT", str(replacement).lower())
        .replace("EDITABLE", str(editable).lower())
    )
    await read(cap, script)


async def capability() -> BrowserCapability:
    cap = BrowserCapability(viewport_width=900, viewport_height=600, settle_quiet_ms=20)
    cap.backend = LocalChromeProvider(headless=True, force_device_scale_factor=1)  # type: ignore[assignment]
    cap.mouse_trajectory = ThroughMenu()  # type: ignore[assignment]
    await cap.setup(negotiation={"browser.dom", "browser.screenshot"})
    return cap


@pytest.mark.parametrize("action", ["click_coordinate", "click", "type_coordinate"])
async def test_hover_replacement_with_the_same_name_and_role_refuses_all_clicking_paths(action):
    cap = await capability()
    try:
        await scene(cap, scope="page", replacement=True, editable=action == "type_coordinate")
        params: dict[str, Any] = {"coordinate_x": 650, "coordinate_y": 300}
        if action == "click":
            await cap.begin_observation(0)
            await cap.resolve_fragment("browser.dom")
            index = next(
                index
                for index, node in cap.selector_map.items()
                if node.attributes.get("id") == "target"
            )
            params = {"index": index}
        elif action == "type_coordinate":
            params.update(text="must never be typed", clear=True)
        result = await cap.execute_action(Action(name=f"browser.{action}", params=params))
        assert not result.success and result.error and "changed" in result.error.lower()
        assert "observ" in result.error.lower()
        assert await read(cap, "changed") is True
        assert await read(cap, "events") == []
        assert await read(cap, "document.elementFromPoint(650,300).id") == "replacement"
        if action == "type_coordinate":
            assert await read(cap, "target.value") == ""
        # The next observation captures the menu that made the earlier target stale.
        await cap.begin_observation(1)
        await cap.resolve_fragment("browser.dom")
        assert any(node.attributes.get("id") == "replacement" for node in cap.selector_map.values())
    finally:
        await cap.teardown()


@pytest.mark.parametrize("scope", ["page", "shadow", "frame"])
@pytest.mark.parametrize(
    "replacement", [False, True], ids=["same-control-new-child", "new-control"]
)
async def test_comparison_tracks_control_identity_through_supported_dom_boundaries(
    scope, replacement
):
    cap = await capability()
    try:
        await scene(cap, scope=scope, replacement=replacement)
        result = await cap.execute_action(
            Action(
                name="browser.click_coordinate", params={"coordinate_x": 650, "coordinate_y": 300}
            )
        )
        assert await read(cap, "changed") is True
        assert result.success is not replacement, result.error
        assert await read(cap, "events") == (
            [] if replacement else ["mousedown", "mouseup", "click"]
        )
    finally:
        await cap.teardown()
