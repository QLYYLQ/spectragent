"""The browser's ``capture_state`` fragment in a real Chrome (headless, local pages only).

``docs/architecture_design.md`` §4.4, "跨平台采集状态 ``capture_state``".
Against the local effects site (``tests/_helpers/effects_site.py``), one
capability negotiating ``browser.capture_state`` beside the screenshot and
``browser_meta``, observed the way ``Environment.observe`` drives it
(``begin_observation`` then ``resolve_fragment``):

- ``image.html``: the visible image that 404s is counted and named
  ``missing.png``; the one far below the fold is not, until the page is
  scrolled to it; the image that loads never is;
- a page with no failed image measures ``0``;
- the pointer is unknown before any mouse input and the clicked point after a
  coordinate click, with the screenshot's cursor drawing off;
- a ``target=_blank`` link the agent clicked: the next observation shows the
  new tab, its capture state says the foreground changed (``opened``, from the
  opener to the new tab), and that tab's pointer is unknown;
- ``busy.html`` (text changing every 50 ms): the settle wait ends at its cap
  with ``dom`` pending.

Every observation still serves the screenshot, with the same settle and focus
records as the capture state. The fake-CDP half is
``test_browser_capture_state.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import (
    BrowserCaptureStateObs,
    BrowserMetaObs,
    ScreenshotObs,
)
from spectragent.schemas.action import Action
from spectragent.schemas.observation.capture_state import (
    FailedVisibleResources,
    ForegroundChange,
    PointerState,
)
from tests._helpers.effects_site import EffectsSite, centre, effects_site

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 180
FRAGMENTS = frozenset({"browser.capture_state", "browser.screenshot", "browser.browser_meta"})
#: A short cap so the never-quiet page costs one second, and a window it can never fill.
SETTLE_MAX_WAIT_S = 1.0
SETTLE_QUIET_MS = 200
#: Where ``image.html``'s below-the-fold image sits (CSS px from the top).
BELOW_THE_FOLD_TOP = 2400


@dataclass
class Look:
    """One observation: the three fragments, and the tab it captured."""

    state: BrowserCaptureStateObs
    shot: ScreenshotObs
    meta: BrowserMetaObs
    tab: str


@dataclass
class Session:
    looks: dict[str, Look] = field(default_factory=dict)
    opener: str = ""


class _Observer:
    def __init__(self, cap: BrowserCapability) -> None:
        self.cap = cap
        self.cycle = 0

    async def __call__(self) -> Look:
        self.cycle += 1
        await self.cap.begin_observation(self.cycle)
        got = {key: await asyncio.wait_for(self.cap.resolve_fragment(key), 30) for key in FRAGMENTS}
        state, shot, meta = (
            got["browser.capture_state"],
            got["browser.screenshot"],
            got["browser.browser_meta"],
        )
        assert isinstance(state, BrowserCaptureStateObs)
        assert isinstance(shot, ScreenshotObs) and isinstance(meta, BrowserMetaObs)
        return Look(
            state=state,
            shot=shot,
            meta=meta,
            tab=self.cap._session_to_target[self.cap.default_session_id],
        )


async def _act(cap: BrowserCapability, name: str, **params: Any) -> None:
    result = await asyncio.wait_for(
        cap.execute_action(Action(name=f"browser.{name}", params=params)), 60
    )
    assert result.success, f"{name}{params}: {result.error}"


async def _scroll_to(cap: BrowserCapability, y: int) -> None:
    await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": f"window.scrollTo(0, {y})", "returnByValue": True},
        session_id=cap.default_session_id,
        timeout=5,
    )


@pytest.fixture(scope="module")
def site() -> Iterator[EffectsSite]:
    with effects_site() as running:
        yield running


@pytest.fixture(scope="module")
def session(site: EffectsSite) -> Session:
    async def _all() -> Session:
        result = Session()
        cap = BrowserCapability(
            settle_quiet_ms=SETTLE_QUIET_MS,
            settle_max_wait_s=SETTLE_MAX_WAIT_S,
            new_tabs="follow",
        )
        cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
        look = _Observer(cap)
        try:
            await asyncio.wait_for(
                cap.setup(negotiation=set(FRAGMENTS), start_url=site.url("image.html")), 60
            )
            result.looks["images"] = await look()
            await _scroll_to(cap, BELOW_THE_FOLD_TOP - 100)
            result.looks["images scrolled"] = await look()

            await _act(cap, "navigate", url=site.url("click.html"))
            result.looks["no failed image"] = await look()
            x, y = centre("under")  # a button with no handler: nothing navigates
            await _act(cap, "click_coordinate", coordinate_x=x, coordinate_y=y)
            result.looks["clicked"] = await look()

            await _act(cap, "navigate", url=site.url("opener.html"))
            result.looks["opener"] = await look()
            result.opener = result.looks["opener"].tab
            x, y = centre("newtab")
            await _act(cap, "click_coordinate", coordinate_x=x, coordinate_y=y)
            result.looks["followed"] = await look()

            await _act(cap, "navigate", url=site.url("busy.html"))
            result.looks["busy"] = await look()
        finally:
            await cap.teardown()
        return result

    return asyncio.run(asyncio.wait_for(_all(), _TIMEOUT_S))


def test_every_observation_still_serves_the_screenshot_from_the_same_capture(
    session: Session,
) -> None:
    for name, look in session.looks.items():
        assert look.shot.image_bytes, name
        assert look.shot.cursor is None, "the screenshot's cursor drawing stays off"
        assert look.state.settle is not None, name
        assert look.state.settle == look.shot.settle == look.meta.settle, name
        assert look.state.focus_moved == look.shot.focus_moved == look.meta.focus_moved, name


def test_a_visible_image_that_failed_is_counted_and_named(session: Session) -> None:
    images = session.looks["images"]
    assert images.state.failed_visible_resources == FailedVisibleResources(
        status="measured", count=1, identifiers=("missing.png",)
    ), "the image below the fold and the one that loaded are not counted"
    assert images.state.settle_state is not None
    assert images.state.settle_state.outcome == "settled"


def test_the_image_below_the_fold_counts_once_it_is_in_view(session: Session) -> None:
    scrolled = session.looks["images scrolled"]
    assert scrolled.state.failed_visible_resources == FailedVisibleResources(
        status="measured", count=1, identifiers=("missing-below.png",)
    )


def test_a_page_with_no_failed_image_measures_zero(session: Session) -> None:
    assert session.looks["no failed image"].state.failed_visible_resources == (
        FailedVisibleResources(status="measured", count=0)
    )


def test_the_pointer_is_unknown_until_a_move_is_confirmed(session: Session) -> None:
    for name in ("images", "images scrolled", "no failed image"):
        assert session.looks[name].state.pointer == PointerState(status="unknown"), name
    x, y = centre("under")
    assert session.looks["clicked"].state.pointer == PointerState(status="known", x=x, y=y)


def test_a_tab_the_click_opened_is_a_foreground_change(session: Session) -> None:
    opener, followed = session.looks["opener"], session.looks["followed"]
    assert opener.state.foreground_change is None
    assert followed.tab != session.opener
    assert followed.meta.url.endswith("/effects/second.html")
    assert followed.state.foreground_change == ForegroundChange(
        reason="opened", from_surface=session.opener, to_surface=followed.tab
    )
    # The pointer belongs to the tab the capture shows, where nothing moved it yet.
    assert followed.state.pointer == PointerState(status="unknown")
    assert followed.state.failed_visible_resources.status == "measured"


def test_a_page_that_never_goes_quiet_is_captured_at_the_cap(session: Session) -> None:
    busy = session.looks["busy"].state
    settle = busy.settle_state
    assert settle is not None
    assert settle.outcome == "capped" and "dom" in settle.pending
    assert SETTLE_MAX_WAIT_S * 1000 * 0.9 <= settle.waited_ms < SETTLE_MAX_WAIT_S * 1000 + 1500
    assert busy.failed_visible_resources == FailedVisibleResources(status="measured", count=0)
