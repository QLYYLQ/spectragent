"""The capability follows a tab the agent's actions opened, and leaves a focused tab that closed (§4.14).

On the fake browser (``tests/_helpers/fake_cdp.py``): target discovery,
``Page.windowOpen`` and tabs that close themselves are scripted the way Chrome
155 sends them (``tabs.py``'s measurements). An "action that opens a tab" is
``browser.wait`` with its ``execute`` replaced: it fires ``Page.windowOpen`` on
the opener's session and opens the target, as a click on a ``target=_blank``
link makes Chrome do. The real-Chrome half is
``test_browser_follow_tab_live.py``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.capabilities.browser import capability as capability_module
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import (
    BrowserCaptureFragment,
    BrowserMetaObs,
)
from spectragent.core.capabilities.browser.settle import SETTLE_WORLD
from spectragent.core.capabilities.browser.tabs import FocusMove
from spectragent.core.composer import ConfigComposer
from spectragent.core.trace.redaction import redact_config
from spectragent.schemas.action import Action, ActionResult
from spectragent.scripts.generate_all import _collect_registrars
from tests._helpers.browser_archive import OwnedBrowser
from tests._helpers.fake_cdp import FakeBrowser

FIRST = "page-1"
FRAGMENTS = ("browser.browser_meta", "browser.dom", "browser.screenshot")
CONFIG = Path(__file__).resolve().parents[3] / "configs" / "ui_tars_local.yaml"


async def _capability(
    browser: FakeBrowser, *, new_tabs: str = "follow", start_url: str | None = None
) -> BrowserCapability:
    cap = BrowserCapability(new_tabs=new_tabs)  # type: ignore[arg-type]
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    await cap.setup(start_url=start_url)
    cap.apply_negotiation(set(FRAGMENTS))
    return cap


class _Observer:
    """One observation cycle at a time, the way ``Environment.observe`` drives them."""

    def __init__(self, cap: BrowserCapability) -> None:
        self.cap = cap
        self.cycle = 0

    async def __call__(self) -> dict[str, Any]:
        self.cycle += 1
        await self.cap.begin_observation(self.cycle)
        return {
            key: await asyncio.wait_for(self.cap.resolve_fragment(key), 5.0) for key in FRAGMENTS
        }


def _opens(
    browser: FakeBrowser,
    cap: BrowserCapability,
    *tabs: tuple[str, bool | None],
    opener: str = FIRST,
    **target: Any,
) -> None:
    """Make ``browser.wait`` open *tabs* — ``(target id, userGesture or None)`` — from *opener*."""

    async def execute(_params: dict[str, Any], *, capability: BrowserCapability) -> ActionResult:
        for target_id, gesture in tabs:
            if gesture is not None:
                session = capability._target_to_session[opener]
                browser.control.fire(
                    "Page.windowOpen",
                    {"url": f"https://shop.test/{target_id}", "userGesture": gesture},
                    session,
                )
            browser.open_target(
                target_id,
                "page",
                url=f"https://shop.test/{target_id}",
                opener_id=opener,
                **target,
            )
        return ActionResult(success=True)

    cap._actions_by_name["browser.wait"].execute = execute  # type: ignore[method-assign]


def _quiet_wait(cap: BrowserCapability) -> None:
    """``browser.wait`` that returns at once and opens nothing."""

    async def execute(_params: dict[str, Any], *, capability: BrowserCapability) -> ActionResult:
        return ActionResult(success=True)

    cap._actions_by_name["browser.wait"].execute = execute  # type: ignore[method-assign]


async def _act(cap: BrowserCapability, name: str = "browser.wait", **params: Any) -> ActionResult:
    result = await cap.execute_action(Action(name=name, params=params))
    assert result.success, result.error
    return result


def _focused(cap: BrowserCapability) -> str:
    return cap._session_to_target[cap.default_session_id]


def _move(observed: dict[str, Any]) -> FocusMove | None:
    """The ``focus_moved`` every fragment of one capture carries — the same on each."""
    moves = [
        fragment.focus_moved
        for fragment in observed.values()
        if isinstance(fragment, BrowserCaptureFragment)
    ]
    assert moves and all(move == moves[0] for move in moves), moves
    return moves[0]


def _activated(browser: FakeBrowser) -> list[str]:
    return [
        s.params["targetId"] for s in browser.control.sent if s.method == "Target.activateTarget"
    ]


def _settled_on(browser: FakeBrowser) -> list[str | None]:
    """The sessions the observation's settle wait ran on, in order."""
    return [
        s.session_id
        for s in browser.control.sent
        if s.method == "Page.createIsolatedWorld" and s.params.get("worldName") == SETTLE_WORLD
    ]


# ── following a tab the agent's actions opened ───────────────────────


class TestFollow:
    async def test_the_observation_after_the_action_shows_the_new_tab(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            first = await observe()
            assert _move(first) is None
            _opens(browser, cap, ("page-2", True))
            await _act(cap)
            observed = await observe()
            assert _focused(cap) == "page-2"
            assert _move(observed) == FocusMove(reason="new_tab", from_tab=FIRST, to_tab="page-2")
            meta = observed["browser.browser_meta"]
            assert isinstance(meta, BrowserMetaObs) and meta.url == "https://shop.test/page-2"
            assert [tab["tab_id"] for tab in meta.tabs if tab["is_active"]] == ["page-2"]
            assert _activated(browser) == ["page-2"]
            # The settle wait ran on the tab the capture shows, after it was adopted.
            assert _settled_on(browser)[-1] == cap.default_session_id
        finally:
            await cap.teardown()

    async def test_the_last_of_several_tabs_one_action_opened_is_followed(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True), ("page-3", True))
            await _act(cap)
            await observe()
            assert _focused(cap) == "page-3"
        finally:
            await cap.teardown()

    async def test_a_window_opened_without_the_users_activation_is_not_followed(self) -> None:
        """A popup a page opens as it loads, or one click's second window: a real browser blocks it."""
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True), ("page-3", False))
            await _act(cap)
            observed = await observe()
            assert _focused(cap) == "page-2", (
                "the first window, the one a real browser lets through"
            )
            meta = observed["browser.browser_meta"]
            assert {tab["tab_id"] for tab in meta.tabs} == {FIRST, "page-2", "page-3"}
        finally:
            await cap.teardown()

    async def test_unknown_activation_is_followed(self) -> None:
        """A link inside a cross-site iframe: Chrome sends its windowOpen on the iframe's session."""
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", None))
            await _act(cap)
            await observe()
            assert _focused(cap) == "page-2"
        finally:
            await cap.teardown()

    async def test_a_tab_opened_outside_the_agents_actions_is_not_followed(self) -> None:
        """A timer or an ad opens it while the agent thinks: listed, never followed."""
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            browser.control.fire(
                "Page.windowOpen",
                {"url": "https://ads.test/", "userGesture": True},
                cap.default_session_id,
            )
            browser.open_target("page-2", "page", url="https://ads.test/", opener_id=FIRST)
            _quiet_wait(cap)
            await _act(cap)  # the window opens after the tab did
            observed = await observe()
            assert _focused(cap) == FIRST
            assert _move(observed) is None
            meta = observed["browser.browser_meta"]
            assert [tab["tab_id"] for tab in meta.tabs] == [FIRST, "page-2"]
        finally:
            await cap.teardown()

    async def test_a_tab_another_tab_opened_is_not_followed(self) -> None:
        browser = FakeBrowser()
        browser.open_target("page-9", "page", url="https://other.test/")
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", None), opener="page-9")
            await _act(cap)
            await observe()
            assert _focused(cap) == FIRST
        finally:
            await cap.teardown()

    async def test_the_start_page_opens_no_window(self) -> None:
        """Setup opens the start page; a tab it opens as it loads is the page's, not the agent's."""
        browser = FakeBrowser()
        cap = BrowserCapability()
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
        cap._inject_cdp_factory(browser.client)

        async def navigate(
            _params: dict[str, Any], *, capability: BrowserCapability
        ) -> ActionResult:
            browser.open_target("page-2", "page", url="https://ads.test/", opener_id=FIRST)
            return ActionResult(success=True)

        cap._actions_by_name["browser.navigate"].execute = navigate  # type: ignore[method-assign]
        await cap.setup(start_url="https://shop.test/")
        cap.apply_negotiation(set(FRAGMENTS))
        try:
            await _Observer(cap)()
            assert _focused(cap) == FIRST
        finally:
            await cap.teardown()

    async def test_stay_leaves_the_focus_where_it_is(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser, new_tabs="stay")
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True))
            await _act(cap)
            observed = await observe()
            assert _focused(cap) == FIRST
            assert _move(observed) is None
            assert [tab["tab_id"] for tab in observed["browser.browser_meta"].tabs] == [
                FIRST,
                "page-2",
            ]
            assert _activated(browser) == []
            assert cap._follow_window is None, "stay opens no window"
        finally:
            await cap.teardown()

    async def test_a_tab_opened_while_the_observation_waited_is_followed(self) -> None:
        """A click whose handler opens the tab once a request it sent comes back."""
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            settle = cap._settle
            opened: list[str] = []

            async def settle_then_open(sid: str, feed: Any) -> Any:
                result = await settle(sid, feed)
                if not opened:
                    opened.append(sid)
                    browser.control.fire("Page.windowOpen", {"url": "", "userGesture": True}, sid)
                    browser.open_target(
                        "page-2", "page", url="https://shop.test/2", opener_id=FIRST
                    )
                return result

            cap._settle = settle_then_open  # type: ignore[method-assign]
            _quiet_wait(cap)
            await _act(cap)
            observed = await observe()
            assert _focused(cap) == "page-2"
            assert _move(observed) == FocusMove(reason="new_tab", from_tab=FIRST, to_tab="page-2")
            first_session = cap._target_to_session[FIRST]
            assert _settled_on(browser)[-2:] == [first_session, cap.default_session_id], (
                "waited on the tab it left, then again on the tab it captured"
            )
        finally:
            await cap.teardown()

    async def test_a_switch_tab_restarts_the_window_at_the_tab_it_chose(self) -> None:
        browser = FakeBrowser()
        browser.open_target("page-3", "page", url="https://shop.test/3")
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True))
            await _act(cap)  # page-1 opens page-2 ...
            await _act(
                cap, "browser.switch_tab", target_id="page-3"
            )  # ... the agent looks elsewhere
            observed = await observe()
            assert _focused(cap) == "page-3", (
                "the agent's own choice wins over the tab opened before it"
            )
            assert _move(observed) is None, "the agent's own switch is its action, not a move"
            # From now on a tab the newly focused tab opens is followed.
            _opens(browser, cap, ("page-4", True), opener="page-3")
            await _act(cap)
            await observe()
            assert _focused(cap) == "page-4"
        finally:
            await cap.teardown()

    async def test_a_tab_that_cannot_be_adopted_gives_way_to_the_one_before(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Its first document never commits within the bound: page-side commands are never answered."""
        monkeypatch.setattr(capability_module, "_FOLLOW_ADOPT_TIMEOUT_S", 0.2)
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()

            async def execute(
                _params: dict[str, Any], *, capability: BrowserCapability
            ) -> ActionResult:
                browser.open_target("page-2", "page", url="https://shop.test/2", opener_id=FIRST)
                browser.open_target(
                    "page-3", "page", url="https://slow.test/", opener_id=FIRST, page_process=False
                )
                return ActionResult(success=True)

            cap._actions_by_name["browser.wait"].execute = execute  # type: ignore[method-assign]
            await _act(cap)
            with caplog.at_level(logging.WARNING):
                await observe()
            assert _focused(cap) == "page-2"
            assert "page-3" in cap._unfollowable and "page-3" not in cap._target_to_session
            assert any("cannot move to tab page-3" in r.getMessage() for r in caplog.records)
        finally:
            await cap.teardown()


# ── leaving a focused tab that closed itself ──────────────────────────


class TestClosed:
    async def test_focus_returns_to_the_tab_that_opened_it(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True))
            await _act(cap)
            await observe()
            assert _focused(cap) == "page-2"
            browser.close_target("page-2")  # window.close()
            observed = await observe()
            assert _focused(cap) == FIRST
            assert _move(observed) == FocusMove(
                reason="tab_closed", from_tab="page-2", to_tab=FIRST
            )
            assert "page-2" not in cap._target_to_session, (
                "what it held for the closed tab is released"
            )
            assert _activated(browser)[-1] == FIRST
        finally:
            await cap.teardown()

    async def test_else_to_the_most_recently_opened_tab_still_open(self) -> None:
        browser = FakeBrowser()
        browser.open_target("page-3", "page", url="https://shop.test/3")
        browser.open_target("page-4", "page", url="https://shop.test/4")
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True))
            await _act(cap)
            await observe()
            browser.close_target(FIRST)
            browser.close_target("page-2")
            observed = await observe()
            assert _focused(cap) == "page-4"
            assert _move(observed) == FocusMove(
                reason="tab_closed", from_tab="page-2", to_tab="page-4"
            )
        finally:
            await cap.teardown()

    async def test_stay_leaves_a_closed_tab_too(self) -> None:
        """Nothing to stay on: whichever new_tabs, a focused tab that closed hands the focus back."""
        browser = FakeBrowser()
        cap = await _capability(browser, new_tabs="stay")
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True))
            await _act(cap)
            await _act(cap, "browser.switch_tab", target_id="page-2")
            await observe()
            browser.close_target("page-2")
            await observe()
            assert _focused(cap) == FIRST
        finally:
            await cap.teardown()

    async def test_an_action_on_a_closed_tab_fails_naming_it(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True))
            await _act(cap)
            await observe()
            browser.close_target("page-2")
            result = await cap.execute_action(Action(name="browser.wait", params={}))
            assert result.success is False
            assert "page-2" in (result.error or "") and "closed itself" in (result.error or "")
        finally:
            await cap.teardown()

    async def test_no_tab_left_fails_the_observation_once(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True))
            await _act(cap)
            await observe()
            browser.close_target(FIRST)
            browser.close_target("page-2")
            await cap.begin_observation(99)
            with pytest.raises(RuntimeError, match="no other tab is open") as first:
                await cap.resolve_fragment("browser.screenshot")
            with pytest.raises(RuntimeError) as second:
                await cap.resolve_fragment("browser.dom")
            assert second.value is first.value, "the rest of the slice gets the same failure"
        finally:
            await cap.teardown()


# ── the option in YAML ────────────────────────────────────────────────


class TestTheOption:
    @staticmethod
    def _compose(**block: Any) -> tuple[Any, BrowserCapability]:
        raw = yaml.safe_load(CONFIG.read_text("utf-8"))
        raw["environment"][0]["capabilities"][0].update(block)
        run = ConfigComposer(registrars=_collect_registrars()).compose(raw)
        (capability,) = run.instances("environment")[0].capabilities.values()
        assert isinstance(capability, BrowserCapability)
        return run, capability

    def test_follow_is_the_default(self) -> None:
        _, capability = self._compose()
        assert capability.new_tabs == "follow"
        assert BrowserCapability().new_tabs == "follow"

    def test_stay_reaches_the_capability_and_is_recorded_raw(self) -> None:
        run, capability = self._compose(new_tabs="stay")
        assert capability.new_tabs == "stay"
        recorded = redact_config(run.top_level_cfg)["environment"][0]["capabilities"][0]
        assert recorded["new_tabs"] == "stay"

    def test_a_value_outside_the_set_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="new_tabs"):
            self._compose(new_tabs="follow_all")


# ── the tab list, discovery, teardown ─────────────────────────────────


class TestTabs:
    async def test_tabs_are_listed_in_the_order_they_opened(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, ("page-2", True), ("page-3", True))
            await _act(cap)
            # Chrome's tab list is in no fixed order: here it is newest first.
            browser.targets = dict(reversed(list(browser.targets.items())))
            observed = await observe()
            tabs = [tab["tab_id"] for tab in observed["browser.browser_meta"].tabs]
            assert tabs == [FIRST, "page-2", "page-3"]
            assert _focused(cap) == "page-3"
            # "The most recently opened tab other than the current one".
            await _act(cap, "browser.switch_tab", target_id=None)
            assert _focused(cap) == "page-2"
        finally:
            await cap.teardown()

    async def test_a_browser_without_the_page_filter_still_discovers(self) -> None:
        browser = FakeBrowser()
        handle = browser.handle

        async def no_filter(client: Any, method: str, params: dict[str, Any], sid: Any) -> Any:
            if method == "Target.setDiscoverTargets" and "filter" in params:
                raise RuntimeError("CDP error: Invalid parameters")
            return await handle(client, method, params, sid)

        browser.handle = no_filter  # type: ignore[method-assign]
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            assert cap._tabs is not None
            await observe()
            _opens(browser, cap, ("page-2", True))
            await _act(cap)
            await observe()
            assert _focused(cap) == "page-2"
        finally:
            await cap.teardown()

    async def test_a_browser_that_refuses_discovery_runs_as_before(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        browser = FakeBrowser()
        browser.fail("Target.setDiscoverTargets", RuntimeError("CDP error: not allowed"))
        with caplog.at_level(logging.WARNING):
            cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            assert cap._tabs is None
            assert any("refused target discovery" in r.getMessage() for r in caplog.records)
            await observe()
            _opens(browser, cap, ("page-2", True))
            await _act(cap)
            await observe()
            assert _focused(cap) == FIRST
        finally:
            await cap.teardown()

    async def test_teardown_lets_go_of_the_discovery(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        control = browser.control
        assert control.callbacks.get("Target.targetCreated")
        await cap.teardown()
        for method in (
            "Target.targetCreated",
            "Target.targetInfoChanged",
            "Target.targetDestroyed",
            "Page.windowOpen",
        ):
            assert not control.callbacks.get(method), method
        assert cap._tabs is None and cap._follow_window is None
