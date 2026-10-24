"""The browser's own shortcuts through ``browser.send_keys``, in a real headless Chrome (§4.13).

User ruling 2026-10-24. Against the local effects site only
(``tests/_helpers/effects_site.py``), through the capability's own actions in
one headless Chrome, each chord written as the measured platform binds it
(``input/shortcuts.py``'s :func:`binding`):

- Back and Forward between ``a.html`` and ``b.html``: the tab's history moves,
  and the effect states the URL before and after;
- Reload and Hard reload of ``b.html``: a new document of the same URL, as
  ``performance`` reports a reload, a page variable gone;
- the address bar shortcut on ``fields.html``: the key reaches the page and
  the effect says this browser shows no address bar; on macOS, Meta+ArrowLeft
  in a focused text field goes to the field and the tab does not go back;
- Next tab, Previous tab, a numbered tab and the last tab between
  ``fields.html`` and a second tab: the focus moves, the tab focused is the one
  Chrome shows (``visible``), and the page left never received the key;
- Close tab: the tab goes, the focus returns to the tab that opened it
  (``opener.html``'s ``target=_blank`` link), not to the more recent tab, and
  with no opener to the most recently opened tab; closing the only tab is
  refused, and the tab stays.

The fake-CDP halves are ``test_browser_actions/test_browser_shortcuts.py`` and
``test_browser_shortcut_tabs.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.effects import (
    KeyPressEffect,
    Location,
    ShortcutRan,
    ShortcutWithoutUi,
    TabFacts,
)
from spectragent.core.capabilities.browser.input.shortcuts import binding
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.archive_bench import FIXTURE_SETTLE_QUIET_MS
from tests._helpers.effects_site import EffectsSite, centre, effects_site

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 120


@dataclass
class Session:
    platform: str = ""
    results: dict[str, ActionResult] = field(default_factory=dict)
    #: Facts read off the pages, by name.
    facts: dict[str, Any] = field(default_factory=dict)
    #: CDP target ids, by name.
    tabs: dict[str, str] = field(default_factory=dict)


async def _act(cap: BrowserCapability, name: str, **params: Any) -> ActionResult:
    return await cap.execute_action(Action(name=f"browser.{name}", params=params))


async def _press(cap: BrowserCapability, command: str, tab: int | None = None) -> ActionResult:
    """Press the measured platform's binding for *command*."""
    keys = binding(cap.input_platform, command, tab)
    return await _act(cap, "send_keys", keys=keys)


async def _evaluate(cap: BrowserCapability, expression: str, session_id: str | None = None) -> Any:
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=session_id or cap.default_session_id,
        timeout=5,
    )
    return (reply.get("result") or {}).get("value")


async def _page_urls(cap: BrowserCapability) -> dict[str, str]:
    reply = await cap.cdp.send("Target.getTargets", {}, timeout=5)
    return {
        info["targetId"]: info.get("url", "")
        for info in reply.get("targetInfos", [])
        if info.get("type") == "page"
    }


async def _wait_for_tab(cap: BrowserCapability, url: str) -> str:
    """The target id of the tab showing *url*, once Chrome lists it (at most 5 s)."""
    for _ in range(50):
        for target_id, shown in (await _page_urls(cap)).items():
            if shown == url:
                return target_id
        await asyncio.sleep(0.1)
    raise AssertionError(f"no tab shows {url}")


async def _run(site: EffectsSite) -> Session:
    session = Session()
    results, facts, tabs = session.results, session.facts, session.tabs
    cap = BrowserCapability(settle_quiet_ms=FIXTURE_SETTLE_QUIET_MS, new_tabs="stay")
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(), 60)
        session.platform = cap.input_platform

        # ── history and reload ──
        assert (await _act(cap, "navigate", url=site.url("a.html"))).success
        assert (await _act(cap, "navigate", url=site.url("b.html"))).success
        results["back"] = await _press(cap, "back")
        facts["after back"] = await _evaluate(cap, "location.href")
        results["forward"] = await _press(cap, "forward")
        facts["after forward"] = await _evaluate(cap, "location.href")
        await _evaluate(cap, "window.__mark = 1")
        results["reload"] = await _press(cap, "reload")
        facts["after reload"] = await _evaluate(
            cap,
            "[typeof window.__mark, performance.getEntriesByType('navigation')[0].type,"
            " location.href]",
        )
        await _evaluate(cap, "window.__mark = 1")
        results["hard reload"] = await _press(cap, "hard_reload")
        facts["after hard reload"] = await _evaluate(
            cap, "[typeof window.__mark, performance.getEntriesByType('navigation')[0].type]"
        )

        # ── a shortcut for browser UI, and a caret key in a field ──
        assert (await _act(cap, "navigate", url=site.url("fields.html"))).success
        tabs["fields"] = cap.focused_tab_id
        facts["fields session"] = cap.default_session_id
        results["address bar"] = await _press(cap, "address_bar")
        facts["keys after address bar"] = await _evaluate(cap, "window.__keys.slice()")
        facts["tabs after address bar"] = sorted(await _page_urls(cap))
        if session.platform == "macos":
            x, y = centre("text_input")
            assert (
                await _act(cap, "type_coordinate", coordinate_x=x, coordinate_y=y, text="Ada")
            ).success
            await _evaluate(cap, "window.__keys = []")
            results["caret"] = await _act(cap, "send_keys", keys="Meta+ArrowLeft")
            facts["after caret"] = await _evaluate(
                cap, "[location.href, document.activeElement.id, window.__keys.slice()]"
            )
        await _evaluate(cap, "window.__keys = []; document.activeElement.blur()")

        # ── switching tabs ──
        await cap.create_tab(site.url("second.html"))
        tabs["second"] = await _wait_for_tab(cap, site.url("second.html"))
        results["next"] = await _press(cap, "next_tab")
        facts["visible after next"] = await _evaluate(cap, "document.visibilityState")
        facts["fields keys after next"] = await _evaluate(
            cap, "window.__keys.slice()", facts["fields session"]
        )
        results["previous"] = await _press(cap, "previous_tab")
        facts["visible after previous"] = await _evaluate(cap, "document.visibilityState")
        results["select 2"] = await _press(cap, "select_tab", 2)
        results["last"] = await _press(cap, "last_tab")
        facts["focused after last"] = cap.focused_tab_id

        # ── closing: no opener, then an opener, then the only tab ──
        results["close second"] = await _press(cap, "close_tab")
        facts["tabs after close second"] = sorted(await _page_urls(cap))
        facts["focused after close second"] = cap.focused_tab_id
        facts["visible after close second"] = await _evaluate(cap, "document.visibilityState")

        assert (await _act(cap, "navigate", url=site.url("opener.html"))).success
        x, y = centre("newtab")
        assert (await _act(cap, "click_coordinate", coordinate_x=x, coordinate_y=y)).success
        tabs["opened"] = await _wait_for_tab(cap, site.url("second.html"))
        await cap.create_tab(site.url("b.html"))
        tabs["later"] = await _wait_for_tab(cap, site.url("b.html"))
        facts["tab order"] = await cap.tab_ids()
        results["select opened"] = await _press(cap, "select_tab", 2)
        results["close opened"] = await _press(cap, "close_tab")
        facts["focused after close opened"] = cap.focused_tab_id
        facts["tabs after close opened"] = sorted(await _page_urls(cap))

        results["select later"] = await _press(cap, "last_tab")
        results["close later"] = await _press(cap, "close_tab")
        results["close only"] = await _press(cap, "close_tab")
        facts["tabs after close only"] = sorted(await _page_urls(cap))
        facts["focused after close only"] = cap.focused_tab_id
    finally:
        await cap.teardown()
    return session


@pytest.fixture(scope="module")
def site() -> Iterator[EffectsSite]:
    with effects_site() as running:
        yield running


@pytest.fixture(scope="module")
def session(site: EffectsSite) -> Session:
    return asyncio.run(asyncio.wait_for(_run(site), _TIMEOUT_S))


def _ran(result: ActionResult, command: str) -> ShortcutRan:
    assert result.success, result.error
    assert isinstance(result.effect, KeyPressEffect)
    shortcut = result.effect.shortcut
    assert isinstance(shortcut, ShortcutRan), shortcut
    assert shortcut.command == command
    assert result.effect.keys == (), "no key reached the page"
    return shortcut


def test_back_and_forward_move_through_the_history(session: Session, site: EffectsSite) -> None:
    back = _ran(session.results["back"], "back")
    assert (back.before, back.after) == (
        Location(url=site.url("b.html")),
        Location(url=site.url("a.html")),
    )
    assert session.facts["after back"] == site.url("a.html")
    forward = _ran(session.results["forward"], "forward")
    assert forward.after == Location(url=site.url("b.html"))
    assert session.facts["after forward"] == site.url("b.html")


def test_reload_and_hard_reload_load_a_new_document(session: Session, site: EffectsSite) -> None:
    reload = _ran(session.results["reload"], "reload")
    assert (reload.before, reload.after) == (Location(url=site.url("b.html")),) * 2
    assert reload.document_loaded in (True, False), "measured, not unknown"
    assert session.facts["after reload"] == ["undefined", "reload", site.url("b.html")]
    _ran(session.results["hard reload"], "hard_reload")
    assert session.facts["after hard reload"] == ["undefined", "reload"]


def test_the_address_bar_shortcut_reaches_the_page_and_says_there_is_none(
    session: Session,
) -> None:
    result = session.results["address bar"]
    assert result.success, result.error
    assert isinstance(result.effect, KeyPressEffect)
    assert isinstance(result.effect.shortcut, ShortcutWithoutUi)
    assert "this browser shows no address bar" in result.effect.describe()
    assert [key.lower() for key in session.facts["keys after address bar"]] == ["l"]
    assert len(session.facts["tabs after address bar"]) == 1, "no tab opened, none closed"


def test_on_macos_meta_arrow_left_in_a_text_field_is_the_fields(
    session: Session, site: EffectsSite
) -> None:
    """The key goes to the field (where it is the caret's), and the tab does not go back."""
    if session.platform != "macos":
        pytest.skip("Meta+ArrowLeft is the browser's Back only on macOS")
    result = session.results["caret"]
    assert result.success, result.error
    assert isinstance(result.effect, KeyPressEffect)
    assert result.effect.shortcut is None and result.effect.keys == ("Meta+ArrowLeft",)
    assert session.facts["after caret"] == [site.url("fields.html"), "text_input", ["ArrowLeft"]]


def test_tab_shortcuts_move_the_focus_and_never_reach_the_page(session: Session) -> None:
    tabs = session.tabs
    following = _ran(session.results["next"], "next_tab")
    assert isinstance(following.before, TabFacts) and isinstance(following.after, TabFacts)
    assert (following.before.tab_id, following.after.tab_id) == (tabs["fields"], tabs["second"])
    assert session.facts["visible after next"] == "visible"
    assert session.facts["fields keys after next"] == [], "the page left never saw the key"
    previous = _ran(session.results["previous"], "previous_tab")
    assert isinstance(previous.after, TabFacts) and previous.after.tab_id == tabs["fields"]
    assert session.facts["visible after previous"] == "visible"
    numbered = _ran(session.results["select 2"], "select_tab")
    assert isinstance(numbered.after, TabFacts) and numbered.after.tab_id == tabs["second"]
    _ran(session.results["last"], "last_tab")
    assert session.facts["focused after last"] == tabs["second"]


def test_close_tab_closes_it_and_the_focus_moves_where_chrome_moves_it(
    session: Session,
) -> None:
    tabs = session.tabs
    closed = _ran(session.results["close second"], "close_tab")
    assert isinstance(closed.before, TabFacts) and closed.before.tab_id == tabs["second"]
    assert session.facts["tabs after close second"] == [tabs["fields"]]
    assert session.facts["focused after close second"] == tabs["fields"]
    assert session.facts["visible after close second"] == "visible"

    assert session.facts["tab order"] == [tabs["fields"], tabs["opened"], tabs["later"]]
    _ran(session.results["select opened"], "select_tab")
    opened = _ran(session.results["close opened"], "close_tab")
    assert isinstance(opened.after, TabFacts)
    assert opened.after.tab_id == tabs["fields"], "its opener, not the more recent tab"
    assert session.facts["focused after close opened"] == tabs["fields"]
    assert session.facts["tabs after close opened"] == sorted([tabs["fields"], tabs["later"]])


def test_closing_the_only_tab_is_refused(session: Session) -> None:
    _ran(session.results["close later"], "close_tab")
    result = session.results["close only"]
    assert result.success is False
    assert "the only tab open, and the run needs a page" in (result.error or "")
    assert session.facts["tabs after close only"] == [session.tabs["fields"]]
    assert session.facts["focused after close only"] == session.tabs["fields"]
