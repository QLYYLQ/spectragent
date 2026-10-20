"""An index click on an element whose centre is outside the viewport, in a real Chrome.

A press outside the viewport reaches nothing: Chrome drops it and the action
used to report success (a live Skyvern run pressed (1692, 60) in a 1280-wide
viewport, on the close button of a closed side panel). The element is now
scrolled into view first, centred by script when the browser's own
scroll-into-view leaves its centre outside, and the press lands on it. An
element no scrolling can reach (pinned off-screen) is refused, with nothing
pressed. Local fixture pages only (``tests/_helpers/effects_site.py``), the
Skyvern cleaner, which indexes elements outside the viewport, headless.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerRegistrar
from spectragent.core.capabilities.browser.effects import ElementFacts, PointerEffect, ViewportPoint
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.effects_site import EffectsSite, effects_site

pytestmark = pytest.mark.requires_chrome

_VIEWPORT = (1280, 720)


@pytest.fixture(scope="module")
def site() -> Any:
    with effects_site() as running:
        yield running


@asynccontextmanager
async def _browser() -> AsyncIterator[BrowserCapability]:
    cap = BrowserCapability(viewport_width=_VIEWPORT[0], viewport_height=_VIEWPORT[1])
    cap.dom_cleaner = DOMCleanerRegistrar.get("skyvern")()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(negotiation={"browser.dom", "browser.browser_meta"}), 60)
        yield cap
    finally:
        await cap.teardown()


async def _act(cap: BrowserCapability, name: str, **params: Any) -> ActionResult:
    return await cap.execute_action(Action(name=f"browser.{name}", params=params))


async def _far_index(cap: BrowserCapability, cycle: int) -> int:
    await cap.begin_observation(cycle)
    dom = await cap.resolve_fragment("browser.dom")
    return next(
        handle
        for handle, node in dom.selector_map.items()
        if (node.attributes or {}).get("id") == "far"
    )


async def _clicks(cap: BrowserCapability) -> int:
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": "window.__farClicks || 0", "returnByValue": True},
        session_id=cap.default_session_id,
    )
    return int(reply["result"]["value"])


@pytest.mark.parametrize(
    "page",
    ["far.html", "far_hidden.html", "far_strip.html", "far_edge.html", "far_tall.html"],
)
async def test_the_element_is_scrolled_into_view_and_the_press_lands_on_it(
    site: EffectsSite, page: str
) -> None:
    async with _browser() as cap:
        assert (await _act(cap, "navigate", url=site.url(page))).success
        index = await _far_index(cap, 1)
        result = await _act(cap, "click", index=index)
        assert result.success, result.error
        assert await _clicks(cap) == 1
        effect = result.effect
        assert isinstance(effect, PointerEffect)
        assert isinstance(effect.point, ViewportPoint)
        assert 0 <= effect.point.x < _VIEWPORT[0] and 0 <= effect.point.y < _VIEWPORT[1]
        assert effect.hit == ElementFacts(role="button", name="Far button")
        assert effect.on_target is True


async def test_an_element_no_scrolling_can_reach_is_refused_and_nothing_is_pressed(
    site: EffectsSite,
) -> None:
    async with _browser() as cap:
        assert (await _act(cap, "navigate", url=site.url("far_fixed.html"))).success
        index = await _far_index(cap, 1)
        clicked = await _act(cap, "click", index=index)
        assert clicked.success is False
        assert clicked.error is not None and "outside the viewport" in clicked.error
        assert clicked.effect is None
        scrolled = await _act(cap, "scroll", direction="down", index=index)
        assert scrolled.success is False
        assert scrolled.error is not None and "outside the viewport" in scrolled.error
        assert await _clicks(cap) == 0
