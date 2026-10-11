"""Observations wait out an in-flight main-frame navigation (§4.14, M33) — no Chrome.

M31 found and M33 measured it: a click on a form's submit button returns
before the navigation it starts, the next observation's
``Page.captureScreenshot`` landed between the request and the commit, and
under pinned's ``--disable-back-forward-cache`` Chrome never answered it, so
the step ended in the 30 s event timeout. Two fixes, pinned here against
fakes:

- the capability follows each tab's main-frame navigation
  (:class:`MainFrameNavigation`, fed by :class:`NavigationFeed` from the
  control connection) and waits for it to end before each capture attempt —
  bounded, per attempt, and not at all when nothing is in flight;
- the observation screenshot's CDP command has its own bound, so a capture
  Chrome never answers costs one attempt, not the whole event.

The real-Chrome regression is ``test_browser_form_submit_live.py``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser import capability as capability_module
from spectragent.core.capabilities.browser.navigation import (
    MainFrameNavigation,
    NavigationFeed,
)
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.capabilities.browser.watchdogs import screenshot_watchdog
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    ScreenshotRequestEvent,
)
from spectragent.core.capabilities.browser.watchdogs.screenshot_watchdog import (
    ScreenshotWatchdog,
)
from tests._helpers.browser_archive import OwnedBrowser, settle
from tests._helpers.fake_cdp import FakeBrowser, FakeClient
from tests._helpers.fragment_samples import PNG_BYTES

MAIN = "MAIN"
#: The control-connection events a feed listens to.
NAVIGATION_EVENTS = (
    "Page.frameRequestedNavigation",
    "Page.frameStartedNavigating",
    "Page.frameNavigated",
    "Page.frameStoppedLoading",
)


def _finished_without_suspending(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run *coro* one step: it must return without awaiting anything."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise AssertionError("the coroutine suspended")


def _requested(frame_id: str, *, disposition: str = "currentTab") -> dict[str, Any]:
    return {
        "frameId": frame_id,
        "reason": "formSubmissionPost",
        "disposition": disposition,
        "url": "http://shop.test/form_submit/result.html",
    }


def _navigated(frame_id: str) -> dict[str, Any]:
    return {
        "frame": {"id": frame_id, "loaderId": "L2", "url": "http://shop.test/result"},
        "type": "Navigation",
    }


# ── the tracker: pure state ──────────────────────────────────────────


class TestMainFrameNavigation:
    def test_idle_is_settled_and_waiting_costs_nothing(self) -> None:
        navigation = MainFrameNavigation(MAIN)
        assert navigation.in_flight is False
        assert _finished_without_suspending(navigation.wait_settled(10.0)) is True

    def test_a_request_is_in_flight_until_its_commit(self) -> None:
        navigation = MainFrameNavigation(MAIN)
        navigation.requested(MAIN)
        assert navigation.in_flight
        navigation.started(MAIN, "differentDocument")
        assert navigation.in_flight
        navigation.committed(MAIN)
        assert not navigation.in_flight

    def test_loading_that_stops_without_a_commit_settles(self) -> None:
        """A 204 or a download: request → start → ``frameStoppedLoading``, no commit."""
        navigation = MainFrameNavigation(MAIN)
        navigation.requested(MAIN)
        navigation.started(MAIN, "differentDocument")
        navigation.stopped(MAIN)
        assert not navigation.in_flight

    @pytest.mark.parametrize("navigation_type", ["sameDocument", "historySameDocument"])
    def test_a_same_document_start_is_not_in_flight(self, navigation_type: str) -> None:
        navigation = MainFrameNavigation(MAIN)
        navigation.started(MAIN, navigation_type)
        assert not navigation.in_flight

    @pytest.mark.parametrize(
        "navigation_type",
        ["differentDocument", "historyDifferentDocument", "reload", "restoreWithPost", None],
    )
    def test_every_other_start_is_in_flight(self, navigation_type: str | None) -> None:
        navigation = MainFrameNavigation(MAIN)
        navigation.started(MAIN, navigation_type)
        assert navigation.in_flight

    def test_sub_frames_are_ignored(self) -> None:
        navigation = MainFrameNavigation(MAIN)
        navigation.requested("CHILD")
        navigation.started("CHILD", "differentDocument")
        assert not navigation.in_flight
        navigation.requested(MAIN)
        navigation.committed("CHILD")
        navigation.stopped("CHILD")
        assert navigation.in_flight, "a sub-frame's commit does not end the main frame's"

    def test_an_unknown_main_frame_counts_nothing(self) -> None:
        navigation = MainFrameNavigation(None)
        navigation.requested(None)
        navigation.requested("ANY")
        navigation.started(None, "differentDocument")
        assert not navigation.in_flight

    async def test_a_wait_returns_at_the_commit(self) -> None:
        navigation = MainFrameNavigation(MAIN)
        navigation.requested(MAIN)
        waiter = asyncio.ensure_future(navigation.wait_settled(10.0))
        await asyncio.sleep(0.02)
        assert not waiter.done()
        navigation.committed(MAIN)
        assert await asyncio.wait_for(waiter, 1.0) is True

    async def test_a_wait_gives_up_at_the_bound(self) -> None:
        navigation = MainFrameNavigation(MAIN)
        navigation.requested(MAIN)
        started = time.monotonic()
        assert await navigation.wait_settled(0.05) is False
        assert 0.04 <= time.monotonic() - started < 1.0
        assert navigation.in_flight


# ── the feed: the control connection's Page events → the tracker ──────


def _feed(session_id: str = "S1") -> tuple[FakeClient, NavigationFeed]:
    client = FakeBrowser().client("ws://fake")
    return client, NavigationFeed(client, session_id, MainFrameNavigation(MAIN))


class TestNavigationFeed:
    def test_its_sessions_page_events_drive_the_tracker(self) -> None:
        client, feed = _feed()
        client.fire("Page.frameRequestedNavigation", _requested(MAIN), "S1")
        assert feed.navigation.in_flight
        client.fire("Page.frameNavigated", _navigated(MAIN), "S1")
        assert not feed.navigation.in_flight
        client.fire(
            "Page.frameStartedNavigating",
            {"frameId": MAIN, "navigationType": "differentDocument", "loaderId": "L3"},
            "S1",
        )
        assert feed.navigation.in_flight
        client.fire("Page.frameStoppedLoading", {"frameId": MAIN}, "S1")
        assert not feed.navigation.in_flight

    def test_another_sessions_events_are_dropped(self) -> None:
        client, feed = _feed()
        client.fire("Page.frameRequestedNavigation", _requested(MAIN), "S2")
        client.fire("Page.frameRequestedNavigation", {**_requested(MAIN), "sessionId": "S2"})
        assert not feed.navigation.in_flight

    @pytest.mark.parametrize("disposition", ["newTab", "newWindow", "download"])
    def test_a_request_that_leaves_this_document_is_not_in_flight(self, disposition: str) -> None:
        client, feed = _feed()
        client.fire(
            "Page.frameRequestedNavigation", _requested(MAIN, disposition=disposition), "S1"
        )
        assert not feed.navigation.in_flight

    def test_close_unregisters_every_callback(self) -> None:
        client, feed = _feed()
        assert {method for method, callbacks in client.callbacks.items() if callbacks} == set(
            NAVIGATION_EVENTS
        )
        feed.close()
        feed.close()
        assert not any(client.callbacks.values())
        client.fire("Page.frameRequestedNavigation", _requested(MAIN), "S1")
        assert not feed.navigation.in_flight


# ── the capability waits before each capture attempt ─────────────────


class _Chrome(FakeBrowser):
    """The fake browser, with Chrome's two ways of losing a capture sent mid-navigation."""

    def __init__(self, *, first_capture: str = "answered") -> None:
        super().__init__()
        self.first_capture = first_capture
        self.captures = 0

    async def handle(
        self, client: FakeClient, method: str, params: dict[str, Any], session_id: str | None
    ) -> Any:
        if method == "Page.captureScreenshot":
            self.captures += 1
            if self.captures == 1 and self.first_capture == "unanswered":
                await asyncio.Event().wait()
            if self.captures == 1 and self.first_capture == "refused":
                raise RuntimeError("CDP error: Not attached to an active page")
        return await super().handle(client, method, params, session_id)


async def _capability(browser: FakeBrowser) -> BrowserCapability:
    """A set-up capability on the fake browser, observing the screenshot only."""
    cap = BrowserCapability()
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    await cap.setup()
    cap.apply_negotiation({"browser.screenshot"})
    return cap


def _focused(cap: BrowserCapability) -> tuple[str, str]:
    """The focused tab's CDP session and its main frame id."""
    sid = cap._default_session_id
    assert sid is not None
    return sid, cap._main_frame_ids[sid]


def _captures(browser: FakeBrowser, sid: str) -> int:
    return browser.control.methods(session_id=sid).count("Page.captureScreenshot")


class TestCaptureWaitsForTheNavigation:
    async def test_the_capture_is_sent_after_the_commit(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        try:
            sid, main = _focused(cap)
            browser.control.fire("Page.frameRequestedNavigation", _requested(main), sid)
            await cap.begin_observation(1)
            capture = asyncio.ensure_future(cap.resolve_fragment("browser.screenshot"))
            await settle()
            assert not capture.done()
            assert _captures(browser, sid) == 0, "captured between the request and the commit"
            browser.control.fire("Page.frameNavigated", _navigated(main), sid)
            shot = await asyncio.wait_for(capture, 5.0)
            assert isinstance(shot, ScreenshotObs) and shot.image_bytes == browser.screenshot
            assert _captures(browser, sid) == 1
        finally:
            await cap.teardown()

    async def test_nothing_in_flight_means_no_wait(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A capture that waited the bound would take a minute here.
        monkeypatch.setattr(capability_module, "_NAVIGATION_SETTLE_TIMEOUT_S", 60.0)
        browser = FakeBrowser()
        cap = await _capability(browser)
        try:
            sid, main = _focused(cap)
            # A sub-frame navigating, and a same-document one: neither is waited for.
            browser.control.fire("Page.frameRequestedNavigation", _requested("CHILD"), sid)
            browser.control.fire(
                "Page.frameStartedNavigating",
                {"frameId": main, "navigationType": "sameDocument"},
                sid,
            )
            await cap.begin_observation(1)
            with caplog.at_level(logging.WARNING):
                await asyncio.wait_for(cap.resolve_fragment("browser.screenshot"), 2.0)
            assert _captures(browser, sid) == 1
            assert "did not end" not in caplog.text
        finally:
            await cap.teardown()

    async def test_at_the_bound_it_captures_anyway(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(capability_module, "_NAVIGATION_SETTLE_TIMEOUT_S", 0.05)
        browser = FakeBrowser()
        cap = await _capability(browser)
        try:
            sid, main = _focused(cap)
            browser.control.fire("Page.frameRequestedNavigation", _requested(main), sid)
            await cap.begin_observation(1)
            with caplog.at_level(logging.WARNING):
                shot = await asyncio.wait_for(cap.resolve_fragment("browser.screenshot"), 5.0)
            assert shot.image_bytes == browser.screenshot
            assert _captures(browser, sid) == 1
            warnings = [r.getMessage() for r in caplog.records if "did not end" in r.getMessage()]
            assert len(warnings) == 1 and "attempt 1/2" in warnings[0]
        finally:
            await cap.teardown()

    async def test_the_bound_is_per_attempt(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Chrome refusing the first capture (the raw-CDP failure mode) re-fires; it waits again."""
        monkeypatch.setattr(capability_module, "_NAVIGATION_SETTLE_TIMEOUT_S", 0.05)
        browser = _Chrome(first_capture="refused")
        cap = await _capability(browser)
        try:
            sid, main = _focused(cap)
            browser.control.fire("Page.frameRequestedNavigation", _requested(main), sid)
            await cap.begin_observation(1)
            started = time.monotonic()
            with caplog.at_level(logging.WARNING):
                shot = await asyncio.wait_for(cap.resolve_fragment("browser.screenshot"), 5.0)
            assert shot.image_bytes == browser.screenshot
            assert time.monotonic() - started >= 0.1
            assert browser.captures == 2
            warnings = [r.getMessage() for r in caplog.records if "did not end" in r.getMessage()]
            assert [("attempt 1/2" in w, "attempt 2/2" in w) for w in warnings] == [
                (True, False),
                (False, True),
            ]
        finally:
            await cap.teardown()

    async def test_an_unanswered_screenshot_costs_one_attempt_not_the_event(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(screenshot_watchdog, "_SCREENSHOT_COMMAND_TIMEOUT_S", 0.2)
        browser = _Chrome(first_capture="unanswered")
        cap = await _capability(browser)
        try:
            sid, _main = _focused(cap)
            await cap.begin_observation(1)
            started = time.monotonic()
            with caplog.at_level(logging.WARNING):
                # The event's own timeout is 30 s; the command bound must fire long before.
                shot = await asyncio.wait_for(cap.resolve_fragment("browser.screenshot"), 10.0)
            elapsed = time.monotonic() - started
            assert shot.image_bytes == browser.screenshot
            assert browser.captures == 2
            assert 0.2 <= elapsed < 5.0
            assert "got no reply within 0.2s" in caplog.text
            assert "attempt 1/2 did not produce ['browser.screenshot']" in caplog.text
        finally:
            await cap.teardown()

    async def test_an_unknown_main_frame_captures_as_before(self) -> None:
        browser = FakeBrowser()
        browser.fail("Page.getFrameTree", RuntimeError("CDP error: no frame tree"))
        cap = await _capability(browser)
        try:
            sid = cap._default_session_id
            assert sid is not None
            assert sid not in cap._main_frame_ids and sid not in cap._navigation_feeds
            await cap.begin_observation(1)
            shot = await asyncio.wait_for(cap.resolve_fragment("browser.screenshot"), 2.0)
            assert shot.image_bytes == browser.screenshot
        finally:
            await cap.teardown()


# ── the observation screenshot's command bound ───────────────────────


class _SlowCDP:
    """Answers a screenshot after *delay* seconds."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    @property
    def is_connected(self) -> bool:
        return True

    async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
        if method == "Page.captureScreenshot":
            await asyncio.sleep(self.delay)
            return {"data": base64.b64encode(PNG_BYTES).decode()}
        return {}

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        return None

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        return None


class TestScreenshotCommandBound:
    def test_the_bound_sits_well_inside_the_event_timeout(self) -> None:
        bound = screenshot_watchdog._SCREENSHOT_COMMAND_TIMEOUT_S
        assert bound * 2 < BrowserStateRequestEvent().event_timeout

    async def test_the_observation_path_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(screenshot_watchdog, "_SCREENSHOT_COMMAND_TIMEOUT_S", 0.05)
        watchdog = ScreenshotWatchdog(_SlowCDP(0.3))
        event = BrowserStateRequestEvent(fragments=frozenset({"browser.screenshot"}))
        assert await watchdog._on_browser_state_request(event) is None

    async def test_the_action_path_is_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``browser.screenshot`` (the action) keeps waiting as long as its event does."""
        monkeypatch.setattr(screenshot_watchdog, "_SCREENSHOT_COMMAND_TIMEOUT_S", 0.05)
        watchdog = ScreenshotWatchdog(_SlowCDP(0.3))
        assert await watchdog._on_screenshot_request(ScreenshotRequestEvent()) == PNG_BYTES


# ── the tracker lives and dies with its tab ──────────────────────────


def _live_callbacks(client: FakeClient) -> dict[str, int]:
    return {method: len(client.callbacks.get(method, [])) for method in NAVIGATION_EVENTS}


class TestTabLifecycle:
    async def test_attach_follows_the_main_frame_on_the_control_connection(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        try:
            sid, main = _focused(cap)
            feed = cap._navigation_feeds[sid]
            assert feed.navigation.main_frame_id == main
            assert _live_callbacks(browser.control) == dict.fromkeys(NAVIGATION_EVENTS, 1)
        finally:
            await cap.teardown()

    async def test_closing_a_tab_drops_its_feed_only(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        try:
            first, _main = _focused(cap)
            popup = await cap.create_tab()
            assert set(cap._navigation_feeds) == {first, popup}
            assert _live_callbacks(browser.control) == dict.fromkeys(NAVIGATION_EVENTS, 2)
            popup_feed = cap._navigation_feeds[popup]
            await cap.close_tab(popup)
            assert set(cap._navigation_feeds) == {first}
            assert _live_callbacks(browser.control) == dict.fromkeys(NAVIGATION_EVENTS, 1)
            # The closed tab's tracker hears nothing any more.
            browser.control.fire(
                "Page.frameRequestedNavigation",
                _requested(str(popup_feed.navigation.main_frame_id)),
                popup,
            )
            assert not popup_feed.navigation.in_flight
        finally:
            await cap.teardown()

    async def test_a_failed_attach_drops_its_feed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        try:
            first, _main = _focused(cap)

            def _boom(*_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("watchdog construction failed")

            monkeypatch.setattr(cap, "_create_watchdogs_for_scope", _boom)
            with pytest.raises(RuntimeError, match="watchdog construction failed"):
                await cap.create_tab()
            assert set(cap._navigation_feeds) == {first}
            assert _live_callbacks(browser.control) == dict.fromkeys(NAVIGATION_EVENTS, 1)
        finally:
            await cap.teardown()

    async def test_teardown_drops_every_feed(self) -> None:
        browser = FakeBrowser()
        cap = await _capability(browser)
        await cap.create_tab()
        assert len(cap._navigation_feeds) == 2
        await cap.teardown()
        assert cap._navigation_feeds == {}
        assert _live_callbacks(browser.control) == dict.fromkeys(NAVIGATION_EVENTS, 0)
