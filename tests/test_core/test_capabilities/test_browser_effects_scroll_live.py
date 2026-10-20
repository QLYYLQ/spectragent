"""``browser.scroll``'s effect in a real Chrome: the container that moved, how far, its edge.

Against the local effects site only (``tests/_helpers/effects_site.py``),
headless, through the capability's own actions: a page scroll from the
pointer's resting place, scrolls to and past the bottom edge, a scroll inside
a scroll box under the pointer, the same box addressed by its index, and a
page that cannot scroll. Each effect is the measured fact, never a guess from
the request: a scroll that moved nothing says ``0 px`` and the edge.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.effects import ScrollEffect, ScrollOffset
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.effects_site import (
    SCROLL_PAGE_HEIGHT,
    EffectsSite,
    centre,
    effects_site,
)

pytestmark = pytest.mark.requires_chrome

_VIEWPORT_HEIGHT = 720


@asynccontextmanager
async def _browser(negotiation: set[str]) -> AsyncIterator[BrowserCapability]:
    cap = BrowserCapability(viewport_height=_VIEWPORT_HEIGHT)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(negotiation=negotiation), 60)
        yield cap
    finally:
        await cap.teardown()


async def _act(cap: BrowserCapability, name: str, **params: Any) -> ActionResult:
    return await cap.execute_action(Action(name=f"browser.{name}", params=params))


def _effect(result: ActionResult) -> ScrollEffect:
    assert result.success, result.error
    assert isinstance(result.effect, ScrollEffect)
    assert result.effect.effect_id == "browser.scroll"
    return result.effect


@pytest.fixture(scope="module")
def site() -> Any:
    with effects_site() as running:
        yield running


async def test_page_scrolls_report_offsets_and_the_bottom_edge(site: EffectsSite) -> None:
    async with _browser({"browser.browser_meta"}) as cap:
        assert (await _act(cap, "navigate", url=site.url("scroll.html"))).success
        first = _effect(await _act(cap, "scroll", direction="down", amount=300))
        assert first.container == "page"
        assert first.delta == ScrollOffset(x=0, y=300)
        assert first.at_boundary is False

        bottom = SCROLL_PAGE_HEIGHT - _VIEWPORT_HEIGHT
        to_edge = _effect(await _act(cap, "scroll", direction="down", amount=2000))
        assert to_edge.after == ScrollOffset(x=0, y=bottom)
        assert to_edge.at_boundary is True

        past_edge = _effect(await _act(cap, "scroll", direction="down", amount=300))
        assert past_edge.container == "page"
        assert past_edge.delta == ScrollOffset(x=0, y=0)
        assert past_edge.at_boundary is True
        assert past_edge.describe() == (
            f"Scroll down moved the page by 0 px (y {bottom} → {bottom}); it is at its bottom edge."
        )

        # Right after a wheel past the edge, Chrome's elastic overscroll can
        # absorb part of the next wheel: the effect is what moved, not what
        # was asked for.
        up = _effect(await _act(cap, "scroll", direction="up", amount=300))
        assert up.delta is not None and -300 <= up.delta.y < 0
        assert up.at_boundary is False


async def test_a_scroll_box_under_the_pointer_is_the_container_that_moves(
    site: EffectsSite,
) -> None:
    async with _browser({"browser.browser_meta"}) as cap:
        assert (await _act(cap, "navigate", url=site.url("scroll.html"))).success
        x, y = centre("inner")
        # Park the pointer over the box: the wheel goes where the pointer is.
        assert (await _act(cap, "click_coordinate", coordinate_x=x, coordinate_y=y)).success
        inner = _effect(await _act(cap, "scroll", direction="down", amount=300))
        assert inner.container == "element"
        assert inner.element is not None and inner.element.role == "generic"
        assert inner.delta == ScrollOffset(x=0, y=300)
        page = await cap.cdp.send(
            "Runtime.evaluate",
            {"expression": "document.scrollingElement.scrollTop", "returnByValue": True},
            session_id=cap.default_session_id,
        )
        assert page["result"]["value"] == 0  # the page itself did not move


async def test_a_scroll_box_addressed_by_its_index(site: EffectsSite) -> None:
    async with _browser({"browser.dom", "browser.browser_meta"}) as cap:
        assert (await _act(cap, "navigate", url=site.url("scroll.html"))).success
        await cap.begin_observation(1)
        dom = await cap.resolve_fragment("browser.dom")
        index = next(
            handle
            for handle, node in dom.selector_map.items()
            if (node.attributes or {}).get("id") == "inner"
        )
        effect = _effect(await _act(cap, "scroll", direction="down", amount=200, index=index))
        assert effect.container == "element"
        assert effect.delta == ScrollOffset(x=0, y=200)


async def test_a_page_that_cannot_scroll_says_it_did_not_move(site: EffectsSite) -> None:
    async with _browser({"browser.browser_meta"}) as cap:
        assert (await _act(cap, "navigate", url=site.url("short.html"))).success
        effect = _effect(await _act(cap, "scroll", direction="down", amount=300))
        assert effect.container == "page"
        assert effect.delta == ScrollOffset(x=0, y=0)
        assert effect.at_boundary is True
