"""``background_tabs: freeze`` — the tab the focus leaves is frozen, and runs again before it is shown (§4.14).

On the fake browser (``tests/_helpers/fake_cdp.py``), which keeps each tab's
page lifecycle the way Chrome 155 does (measured): ``Page.setWebLifecycleState``
freezes and resumes it, every other command on a frozen tab is answered, and a
screenshot of it or bringing it to the front brings it back to life
(``FakeBrowser.implicit_resumes``). The order of the commands around a focus
move is the contract: the tab the focus returns to is set running before
``Target.activateTarget``, the tab it leaves is frozen after. The real-Chrome
half is ``test_browser_background_tabs_live.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from spectragent.core.capabilities.browser.archive import (
    DomRawArchiver,
    FramesArchiver,
    NetworkArchiver,
    StorageArchiver,
)
from spectragent.core.capabilities.browser.archive.fingerprint import FINGERPRINT_NAME
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.composer import ConfigComposer
from spectragent.core.registries import collect_registrars
from spectragent.core.trace.redaction import redact_config
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.browser_archive import OwnedBrowser, archiving_capability, settle
from tests._helpers.fake_cdp import FakeBrowser

FIRST = "page-1"
FRAGMENTS = ("browser.browser_meta", "browser.dom", "browser.screenshot")
CONFIG = Path(__file__).resolve().parents[3] / "configs" / "ui_tars_local.yaml"
BORROWED_URL = "ws://127.0.0.1:9222/devtools/browser/borrowed"


async def _capability(
    browser: FakeBrowser,
    *,
    background_tabs: str = "freeze",
    new_tabs: str = "follow",
    borrowed: bool = False,
) -> BrowserCapability:
    cap = BrowserCapability(
        background_tabs=background_tabs,  # type: ignore[arg-type]
        new_tabs=new_tabs,  # type: ignore[arg-type]
        cdp_url=BORROWED_URL if borrowed else None,
    )
    if not borrowed:
        cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    await cap.setup()
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


def _opens(browser: FakeBrowser, cap: BrowserCapability, target_id: str, opener: str) -> None:
    """Make ``browser.wait`` open *target_id* from *opener*, as a click on a target=_blank link."""

    async def execute(_params: dict[str, Any], *, capability: BrowserCapability) -> ActionResult:
        session = capability._target_to_session[opener]
        browser.control.fire(
            "Page.windowOpen",
            {"url": f"https://shop.test/{target_id}", "userGesture": True},
            session,
        )
        browser.open_target(
            target_id, "page", url=f"https://shop.test/{target_id}", opener_id=opener
        )
        return ActionResult(success=True)

    cap._actions_by_name["browser.wait"].execute = execute  # type: ignore[method-assign]


async def _act(cap: BrowserCapability, name: str = "browser.wait", **params: Any) -> None:
    result = await cap.execute_action(Action(name=name, params=params))
    assert result.success, result.error


def _focused(cap: BrowserCapability) -> str:
    return cap._session_to_target[cap.default_session_id]


class _Moves:
    """The focus commands the control connection sent, as ``(command, tab)`` in order."""

    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.seen = 0
        #: Every control session's tab, kept after the tab closes.
        self.tab_of: dict[str, str] = {}

    def since_last(self) -> list[tuple[str, str]]:
        for target in self.browser.targets.values():
            for session in target.sessions.values():
                self.tab_of[session] = target.target_id
        sent = self.browser.control.sent[self.seen :]
        self.seen = len(self.browser.control.sent)
        moves: list[tuple[str, str]] = []
        for s in sent:
            if s.method == "Target.activateTarget":
                moves.append(("activate", s.params["targetId"]))
            elif s.method == "Page.setWebLifecycleState":
                moves.append((s.params["state"], self.tab_of.get(s.session_id or "", "?")))
        return moves


def _frozen(browser: FakeBrowser) -> set[str]:
    return {t.target_id for t in browser.targets.values() if t.frozen}


# ── around the focus moves ────────────────────────────────────────────


class TestFocusMoves:
    async def test_following_a_new_tab_freezes_the_tab_it_came_from(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe, moves = _Observer(cap), _Moves(browser)
        try:
            await observe()
            _opens(browser, cap, "page-2", FIRST)
            moves.since_last()
            await _act(cap)
            await observe()
            assert _focused(cap) == "page-2"
            assert moves.since_last() == [("activate", "page-2"), ("frozen", FIRST)]
            assert _frozen(browser) == {FIRST} and cap._frozen == {FIRST}
            assert browser.implicit_resumes == []
        finally:
            await cap.teardown()

    async def test_switching_back_sets_the_tab_running_before_it_is_shown(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe, moves = _Observer(cap), _Moves(browser)
        try:
            await observe()
            _opens(browser, cap, "page-2", FIRST)
            await _act(cap)
            await observe()
            moves.since_last()
            await _act(cap, "browser.switch_tab", target_id=FIRST)
            assert moves.since_last() == [
                ("active", FIRST),
                ("activate", FIRST),
                ("frozen", "page-2"),
            ]
            assert _focused(cap) == FIRST
            assert _frozen(browser) == {"page-2"} and cap._frozen == {"page-2"}
            await observe()
            assert browser.implicit_resumes == [], "the observation never resumed page-2"
        finally:
            await cap.teardown()

    async def test_a_switch_to_the_focused_tab_freezes_nothing(self) -> None:
        browser = FakeBrowser()
        browser.open_target("page-2", "page", url="https://shop.test/2")
        cap = await _capability(browser)
        moves = _Moves(browser)
        try:
            moves.since_last()
            await _act(cap, "browser.switch_tab", target_id=FIRST)
            assert moves.since_last() == [("activate", FIRST)]
            assert _frozen(browser) == set()
        finally:
            await cap.teardown()

    async def test_a_tab_that_closed_is_not_frozen_and_the_one_the_focus_returns_to_runs(
        self,
    ) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe, moves = _Observer(cap), _Moves(browser)
        try:
            await observe()
            _opens(browser, cap, "page-2", FIRST)
            await _act(cap)
            await observe()
            moves.since_last()
            browser.close_target("page-2")  # window.close()
            await observe()
            assert _focused(cap) == FIRST
            assert moves.since_last() == [("active", FIRST), ("activate", FIRST)]
            assert _frozen(browser) == set() and cap._frozen == set()
        finally:
            await cap.teardown()

    async def test_a_frozen_tab_the_page_closed_is_forgotten(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, "page-2", FIRST)
            await _act(cap)
            await observe()
            assert cap._frozen == {FIRST}
            browser.close_target(FIRST)  # page-2 closes the window that opened it
            await observe()
            assert _focused(cap) == "page-2"
            assert cap._frozen == set() and FIRST not in cap._target_to_session
        finally:
            await cap.teardown()

    async def test_a_tab_the_focus_was_never_on_is_not_frozen(self) -> None:
        """``new_tabs: stay``: the new tab runs in the background, as it did before."""
        browser = FakeBrowser()
        cap = await _capability(browser, new_tabs="stay")
        observe, moves = _Observer(cap), _Moves(browser)
        try:
            await observe()
            _opens(browser, cap, "page-2", FIRST)
            moves.since_last()
            await _act(cap)
            await observe()
            assert _focused(cap) == FIRST
            assert moves.since_last() == []
            assert _frozen(browser) == set()
        finally:
            await cap.teardown()

    async def test_run_sends_no_lifecycle_command(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser, background_tabs="run")
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, "page-2", FIRST)
            await _act(cap)
            await observe()
            await _act(cap, "browser.switch_tab", target_id=FIRST)
            assert "Page.setWebLifecycleState" not in browser.control.methods()
            assert _frozen(browser) == set()
        finally:
            await cap.teardown()

    async def test_a_freeze_that_fails_leaves_the_tab_running_and_the_focus_moved(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            browser.fail("Page.setWebLifecycleState", RuntimeError("CDP error: not allowed"))
            _opens(browser, cap, "page-2", FIRST)
            await _act(cap)
            with caplog.at_level(logging.WARNING):
                await observe()
            assert _focused(cap) == "page-2"
            assert cap._frozen == set()
            assert any("could not be frozen" in r.getMessage() for r in caplog.records)
        finally:
            await cap.teardown()

    async def test_the_tab_list_shows_frozen_tabs_without_waking_them(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        try:
            await observe()
            _opens(browser, cap, "page-2", FIRST)
            await _act(cap)
            observed = await observe()
            tabs = observed["browser.browser_meta"].tabs
            assert [(tab["tab_id"], tab["is_active"]) for tab in tabs] == [
                (FIRST, False),
                ("page-2", True),
            ]
            assert _frozen(browser) == {FIRST} and browser.implicit_resumes == []
        finally:
            await cap.teardown()


# ── teardown ──────────────────────────────────────────────────────────


class TestTeardown:
    async def test_a_borrowed_browser_gets_its_frozen_tabs_back_running(self) -> None:
        """An explicit cdp_url: the tabs outlive the run, so none is left frozen."""
        browser = FakeBrowser()
        cap = await _capability(browser, borrowed=True)
        observe = _Observer(cap)
        await observe()
        _opens(browser, cap, "page-2", FIRST)
        await _act(cap)
        await observe()
        assert _frozen(browser) == {FIRST}
        await cap.teardown()
        assert _frozen(browser) == set()
        assert browser.targets.keys() >= {FIRST, "page-2"}, "a borrowed browser's tabs stay"
        assert cap._frozen == set()

    async def test_an_owned_browser_closes_with_them(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        observe = _Observer(cap)
        await observe()
        _opens(browser, cap, "page-2", FIRST)
        await _act(cap)
        await observe()
        sent_before = len(browser.control.sent)
        await cap.teardown()
        assert not [
            s for s in browser.control.sent[sent_before:] if s.method == "Page.setWebLifecycleState"
        ]
        assert cap._frozen == set()


# ── what the archivers do with a frozen tab ───────────────────────────


async def test_collections_read_a_frozen_tab_without_waking_it() -> None:
    """Storage reads every tab, frames shoot only the focused one, the fingerprint says freeze.

    Every read a collection sends to a frozen tab is answered at once in
    Chrome 155 (measured), and none of them resumes it; only a screenshot
    would, and no archiver takes one of a tab the focus is not on.
    """
    browser = FakeBrowser()
    cap, driver = archiving_capability(
        browser=browser,
        archivers=(
            NetworkArchiver(require_replayable=False, drain_timeout_s=0.2),
            StorageArchiver(),
            DomRawArchiver(),
            FramesArchiver(after_observation_delay_s=0.01),
        ),
    )
    cap.background_tabs = "freeze"
    await driver.start()
    try:
        await driver.observe()
        _opens(browser, cap, "page-2", FIRST)
        await driver.act()
        await driver.observe()
        assert _frozen(browser) == {FIRST}
        frozen_at = {client.id: len(client.sent) for client in browser.clients}
        await asyncio.sleep(0.05)  # the after_observation frame of page-2 comes due
        await driver.act()  # a before_action frame of page-2
        driver.change_storage()
        await settle()
        batch = await cap.collect_archive()
        assert browser.implicit_resumes == []
        assert _frozen(browser) == {FIRST}
        frozen_sessions = set(browser.targets[FIRST].sessions.values())
        shots = [
            s.session_id
            for client in browser.clients
            for s in client.sent[frozen_at[client.id] :]
            if s.method == "Page.captureScreenshot"
        ]
        assert shots and not frozen_sessions & set(shots)
        files = {f.name: f for f in batch.files}
        storage = json.loads(files["storage.json"].data)
        assert {tab["tab"] for tab in storage["tabs"]} == {"tab-0", "tab-1"}
        # The frozen tab's frames were read like the focused tab's (the fake
        # answers no storage key for either), and nothing waited out a timeout.
        unread = {(u["target"], u["operation"]) for u in storage["unavailable"]}
        assert (FIRST, "frames") not in unread
        assert {op for target, op in unread if target == FIRST} == {
            op for target, op in unread if target == "page-2"
        }
        assert not [u for u in storage["unavailable"] if "imeout" in u["error"]]
        fingerprint = json.loads(files[FINGERPRINT_NAME].data)
        assert fingerprint["background_tabs"] == "freeze"
    finally:
        await cap.teardown()


# ── the option in YAML ────────────────────────────────────────────────


class TestTheOption:
    @staticmethod
    def _compose(**block: Any) -> tuple[Any, BrowserCapability]:
        raw = yaml.safe_load(CONFIG.read_text("utf-8"))
        raw["environment"][0]["capabilities"][0].update(block)
        run = ConfigComposer(registrars=collect_registrars()).compose(raw)
        (capability,) = run.instances("environment")[0].capabilities.values()
        assert isinstance(capability, BrowserCapability)
        return run, capability

    def test_run_is_the_default(self) -> None:
        _, capability = self._compose()
        assert capability.background_tabs == "run"
        assert BrowserCapability().background_tabs == "run"

    def test_freeze_reaches_the_capability_and_is_recorded_raw(self) -> None:
        run, capability = self._compose(background_tabs="freeze")
        assert capability.background_tabs == "freeze"
        recorded = redact_config(run.top_level_cfg)["environment"][0]["capabilities"][0]
        assert recorded["background_tabs"] == "freeze"

    def test_a_value_outside_the_set_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="background_tabs"):
            self._compose(background_tabs="throttle")

    def test_background_throttling_reaches_local_chrome(self) -> None:
        raw = yaml.safe_load(CONFIG.read_text("utf-8"))
        block = raw["environment"][0]["capabilities"][0]
        block["backend"] = {**block["backend"], "background_throttling": True}
        run = ConfigComposer(registrars=collect_registrars()).compose(raw)
        (capability,) = run.instances("environment")[0].capabilities.values()
        assert capability.backend.background_throttling is True  # type: ignore[union-attr]
        recorded = redact_config(run.top_level_cfg)["environment"][0]["capabilities"][0]
        assert recorded["backend"]["background_throttling"] is True
