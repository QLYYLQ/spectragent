"""The browser's Close tab and tab-switching shortcuts, through ``browser.send_keys`` (§4.13).

User ruling 2026-10-24. On the fake browser (``tests/_helpers/fake_cdp.py``)
with a real ``BrowserCapability``, whose input platform is measured from the
fake's ``chrome://version`` OS cell:

- next / previous tab switch in the order ``browser_meta`` lists the tabs (the
  order they opened) and wrap around; a numbered tab and the last tab select
  that position; a switch goes through ``BrowserCapability.switch_tab``, the
  code ``browser.switch_tab`` uses, so it restarts the follow window as that
  action does;
- Close tab closes the focused tab and the focus moves where it moves when a
  focused tab closes itself: to the tab that opened it if still open, else to
  the most recently opened tab still open; what the capability held for the
  closed tab is released;
- closing the only tab is refused: the run needs a page;
- none of these chords is sent to the page.

The navigation commands are ``test_browser_actions/test_browser_shortcuts.py``;
the real-Chrome half is ``test_browser_shortcuts_live.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spectragent.core.capabilities.browser import capability as capability_module
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.effects import (
    KeyPressEffect,
    Location,
    ShortcutRan,
    TabFacts,
)
from spectragent.core.capabilities.browser.observations import BrowserCaptureFragment
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.browser_archive import OwnedBrowser
from tests._helpers.fake_cdp import FakeBrowser

FIRST = "page-1"
FRAGMENTS = ("browser.browser_meta", "browser.dom", "browser.screenshot")
OS_CELL = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}


def _browser(*tabs: tuple[str, str | None], platform: str = "macos") -> FakeBrowser:
    """A browser with ``page-1`` and *tabs* (``(target id, opener id or None)``), in that order."""
    browser = FakeBrowser()
    browser.browser_os = OS_CELL[platform]
    for target_id, opener in tabs:
        browser.open_target(
            target_id, "page", url=f"https://shop.test/{target_id}", opener_id=opener
        )
    return browser


async def _capability(browser: FakeBrowser) -> BrowserCapability:
    cap = BrowserCapability()
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    await cap.setup()
    cap.apply_negotiation(set(FRAGMENTS))
    return cap


async def _press(cap: BrowserCapability, keys: str) -> ActionResult:
    return await cap.execute_action(Action(name="browser.send_keys", params={"keys": keys}))


def _ran(result: ActionResult) -> ShortcutRan:
    assert result.success, result.error
    assert isinstance(result.effect, KeyPressEffect)
    assert result.effect.keys == ()
    shortcut = result.effect.shortcut
    assert isinstance(shortcut, ShortcutRan), shortcut
    return shortcut


def _tab(shortcut_fact: Any) -> str:
    assert isinstance(shortcut_fact, TabFacts), shortcut_fact
    return shortcut_fact.tab_id


def _key_events(browser: FakeBrowser) -> list[Any]:
    return [s for s in browser.control.sent if s.method == "Input.dispatchKeyEvent"]


def _activated(browser: FakeBrowser) -> list[str]:
    return [
        s.params["targetId"] for s in browser.control.sent if s.method == "Target.activateTarget"
    ]


class TestSwitching:
    @pytest.mark.parametrize(
        ("platform", "keys"),
        [
            ("macos", "Control+Tab"),
            ("macos", "Meta+Shift+]"),
            ("macos", "Control+PageDown"),
            ("macos", "Meta+Alt+ArrowRight"),
            ("windows", "Control+Tab"),
            ("windows", "Control+PageDown"),
            ("linux", "Control+Tab"),
        ],
    )
    async def test_next_tab_moves_to_the_tab_opened_after(self, platform: str, keys: str) -> None:
        browser = _browser(("page-2", None), ("page-3", None), platform=platform)
        cap = await _capability(browser)
        try:
            assert cap.input_platform == platform
            shortcut = _ran(await _press(cap, keys))
            assert shortcut.command == "next_tab"
            assert (_tab(shortcut.before), _tab(shortcut.after)) == (FIRST, "page-2")
            assert shortcut.after == TabFacts(
                tab_id="page-2", location=Location(url="https://shop.test/page-2")
            )
            assert cap.focused_tab_id == "page-2"
            assert _activated(browser)[-1] == "page-2"
            assert not _key_events(browser), "the key does not reach the page"
        finally:
            await cap.teardown()

    async def test_next_and_previous_wrap_around(self) -> None:
        cap = await _capability(_browser(("page-2", None), ("page-3", None)))
        try:
            assert _tab(_ran(await _press(cap, "Control+Shift+Tab")).after) == "page-3"
            assert _tab(_ran(await _press(cap, "Control+Tab")).after) == FIRST
            assert _tab(_ran(await _press(cap, "Meta+Shift+[")).after) == "page-3"
        finally:
            await cap.teardown()

    @pytest.mark.parametrize(
        ("platform", "keys", "tab"),
        [
            ("macos", "Meta+2", "page-2"),
            ("macos", "Meta+1", FIRST),
            ("macos", "Meta+9", "page-3"),
            ("windows", "Control+3", "page-3"),
            ("windows", "Control+9", "page-3"),
            ("linux", "Control+2", "page-2"),
            ("linux", "Alt+2", "page-2"),
        ],
    )
    async def test_a_numbered_tab_and_the_last_tab(
        self, platform: str, keys: str, tab: str
    ) -> None:
        browser = _browser(("page-2", None), ("page-3", None), platform=platform)
        cap = await _capability(browser)
        try:
            shortcut = _ran(await _press(cap, keys))
            assert _tab(shortcut.after) == tab == cap.focused_tab_id
            assert shortcut.command == ("last_tab" if keys.endswith("9") else "select_tab")
            assert not _key_events(browser)
        finally:
            await cap.teardown()

    async def test_a_number_past_the_last_tab_is_refused(self) -> None:
        cap = await _capability(_browser(("page-2", None)))
        try:
            result = await _press(cap, "Meta+5")
            assert result.success is False
            assert result.error == (
                "Meta+5 is the browser's Select tab 5 shortcut, and it did not run: "
                "only 2 tabs are open"
            )
            assert cap.focused_tab_id == FIRST
        finally:
            await cap.teardown()

    async def test_next_tab_with_one_tab_open_is_refused(self) -> None:
        browser = _browser()
        cap = await _capability(browser)
        try:
            result = await _press(cap, "Control+Tab")
            assert result.success is False
            assert result.error == (
                "Control+Tab is the browser's Next tab shortcut, and it did not run: "
                "only one tab is open"
            )
            assert not _key_events(browser)
        finally:
            await cap.teardown()

    async def test_a_switch_restarts_the_follow_window_as_switch_tab_does(self) -> None:
        """The same code as ``browser.switch_tab``: a tab the tab left opens later is not followed."""
        browser = _browser(("page-2", None))
        cap = await _capability(browser)
        try:
            _ran(await _press(cap, "Control+Tab"))
            assert cap._follow_window is not None
            assert cap._follow_window.focused == "page-2"
        finally:
            await cap.teardown()


class TestClosing:
    @pytest.mark.parametrize(
        ("platform", "keys"),
        [
            ("macos", "Meta+W"),
            ("windows", "Control+W"),
            ("windows", "Control+F4"),
            ("linux", "Control+W"),
        ],
    )
    async def test_the_focus_returns_to_the_tab_that_opened_it(
        self, platform: str, keys: str
    ) -> None:
        browser = _browser(("page-2", FIRST), ("page-3", None), platform=platform)
        cap = await _capability(browser)
        try:
            assert (
                await cap.execute_action(
                    Action(name="browser.switch_tab", params={"target_id": "page-2"})
                )
            ).success
            result = await _press(cap, keys)
            shortcut = _ran(result)
            assert shortcut.command == "close_tab"
            assert shortcut.before == TabFacts(
                tab_id="page-2", location=Location(url="https://shop.test/page-2")
            )
            assert _tab(shortcut.after) == FIRST, "its opener, not the most recent page-3"
            assert "page-2" not in browser.targets
            assert cap.focused_tab_id == FIRST and _activated(browser)[-1] == FIRST
            assert "page-2" not in cap._target_to_session, "what it held is released"
            assert await cap.tab_ids() == [FIRST, "page-3"]
            assert not _key_events(browser)
            assert result.effect is not None
            assert result.effect.describe().startswith(
                f"{keys} ran the browser's Close tab; closed tab page-2 (https://shop.test/page-2), "
                f"and the focus moved to tab {FIRST} ("
            )
        finally:
            await cap.teardown()

    async def test_else_to_the_most_recently_opened_tab_still_open(self) -> None:
        cap = await _capability(_browser(("page-2", None), ("page-3", None)))
        try:
            assert _tab(_ran(await _press(cap, "Meta+W")).after) == "page-3"
            assert await cap.tab_ids() == ["page-2", "page-3"]
        finally:
            await cap.teardown()

    async def test_the_next_observation_captures_the_tab_the_focus_moved_to(self) -> None:
        browser = _browser(("page-2", FIRST))
        cap = await _capability(browser)
        try:
            assert (
                await cap.execute_action(
                    Action(name="browser.switch_tab", params={"target_id": "page-2"})
                )
            ).success
            _ran(await _press(cap, "Meta+W"))
            await cap.begin_observation(1)
            captured = await asyncio.wait_for(cap.resolve_fragment("browser.screenshot"), 5.0)
            assert isinstance(captured, BrowserCaptureFragment)
            assert captured.focus_moved is None, "the agent's own command, not a capability move"
            assert cap.focused_tab_id == FIRST
        finally:
            await cap.teardown()

    async def test_closing_the_only_tab_is_refused(self) -> None:
        browser = _browser()
        cap = await _capability(browser)
        try:
            result = await _press(cap, "Meta+W")
            assert result.success is False and result.effect is None
            assert result.error == (
                "Meta+W is the browser's Close tab shortcut, and it did not run: tab page-1 is "
                "the only tab open, and the run needs a page, so it was not closed"
            )
            assert FIRST in browser.targets and cap.focused_tab_id == FIRST
            assert not any(
                s.method == "Target.closeTarget" and s.params.get("targetId") == FIRST
                for s in browser.control.sent
            )
            assert not _key_events(browser)
        finally:
            await cap.teardown()

    async def test_a_tab_chrome_keeps_open_fails_and_the_focus_stays(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``beforeunload`` dialog holds the tab open: the close is reported as not done."""
        monkeypatch.setattr(capability_module, "_TAB_CLOSE_TIMEOUT_S", 0.1)
        browser = _browser(("page-2", None))
        cap = await _capability(browser)
        try:
            real_send = cap.cdp.send

            async def send(method: str, *args: Any, **kwargs: Any) -> Any:
                if method == "Target.closeTarget":
                    return {"success": True}  # answered, and the tab stays
                return await real_send(method, *args, **kwargs)

            monkeypatch.setattr(cap.cdp, "send", send)
            result = await _press(cap, "Meta+W")
            assert result.success is False
            assert "still lists tab page-1" in (result.error or "")
            assert cap.focused_tab_id == FIRST
        finally:
            await cap.teardown()

    async def test_the_other_platforms_close_is_pressed_on_the_page(self) -> None:
        """Control+W on macOS closes nothing: Chrome on macOS has no such shortcut."""
        browser = _browser(("page-2", None))
        cap = await _capability(browser)
        try:
            result = await _press(cap, "Control+W")
            assert result.success, result.error
            assert isinstance(result.effect, KeyPressEffect)
            assert not isinstance(result.effect.shortcut, ShortcutRan)
            assert "page-1" in browser.targets and cap.focused_tab_id == FIRST
            assert _key_events(browser)
        finally:
            await cap.teardown()
