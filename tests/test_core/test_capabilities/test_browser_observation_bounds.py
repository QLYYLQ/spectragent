"""Every command an observation waits on is bounded (§4.14 "命令上限") — no Chrome.

A renderer that stops answering (a dialog, a page script spinning, a document
swap) used to hold the tab's serial observation scope until the whole
``BrowserStateRequestEvent`` timed out (30 s): the screenshot capture was the
only bounded command. Now the DOM capture as a whole, and the small reads —
``Page.getFrameTree`` in ``browser_meta`` and after the screenshot,
``Page.getNavigationHistory`` — are bounded too: a stuck DOM capture costs one
attempt (the cycle re-fires), a stuck read costs a fragment its stamp.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Callable
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.capability import CAPTURE_PROBE_ROOM_S
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, DOMObs
from spectragent.core.capabilities.browser.watchdogs import base as watchdog_base
from spectragent.core.capabilities.browser.watchdogs import dom_watchdog, screenshot_watchdog
from spectragent.core.capabilities.browser.watchdogs.browser_meta_watchdog import (
    BrowserMetaWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.dom_watchdog import DOMWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import BrowserStateRequestEvent
from spectragent.core.capabilities.browser.watchdogs.screenshot_watchdog import (
    ScreenshotWatchdog,
)
from tests._helpers.browser_archive import OwnedBrowser
from tests._helpers.fake_cdp import FakeBrowser, FakeClient
from tests._helpers.fragment_samples import PNG_BYTES

URL = "http://shop.test/page"


class _StuckCDP:
    """Answers like a healthy tab, except that the methods in *stuck* never answer."""

    def __init__(self, *stuck: str) -> None:
        self.stuck = set(stuck)
        self.sent: list[str] = []

    @property
    def is_connected(self) -> bool:
        return True

    async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
        self.sent.append(method)
        if method in self.stuck:
            await asyncio.Event().wait()
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "MAIN", "loaderId": "L1", "url": URL}}}
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"url": URL, "title": "Shop"}]}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(PNG_BYTES).decode()}
        return {}

    def on_event(self, method: str, callback: Callable[..., Any]) -> None:
        return None

    def off_event(self, method: str, callback: Callable[..., Any]) -> None:
        return None


def _event(*fragments: str) -> BrowserStateRequestEvent:
    return BrowserStateRequestEvent(fragments=frozenset(fragments))


def test_the_bounds_leave_the_event_room_for_a_re_fire() -> None:
    """DOM capture + screenshot + the three reads + the knob-bounded probes stay inside 30 s.

    The probes whose limits are YAML knobs (capture_state once, the media
    state twice) get exactly the room the fixed bounds leave, and the
    capability refuses a configuration that would fill it.
    """
    fixed = (
        dom_watchdog._DOM_CAPTURE_TIMEOUT_S
        + screenshot_watchdog._SCREENSHOT_COMMAND_TIMEOUT_S
        + 3 * watchdog_base.OBSERVATION_READ_TIMEOUT_S
    )
    event_timeout = BrowserStateRequestEvent().event_timeout
    assert fixed + CAPTURE_PROBE_ROOM_S == event_timeout
    defaults = BrowserCapability()
    assert fixed + defaults.capture_state_timeout_s + 2 * defaults.media_state_timeout_s < (
        event_timeout
    )


@pytest.mark.parametrize(
    ("capture_state", "media", "valid"),
    [(1.0, 1.0, True), (3.0, 0.4, True), (0.9, 1.5, True), (2.0, 1.0, False), (3.0, 1.5, False)],
)
def test_the_knob_bounded_probes_cannot_overrun_the_capture(
    capture_state: float, media: float, valid: bool
) -> None:
    if valid:
        cap = BrowserCapability(capture_state_timeout_s=capture_state, media_state_timeout_s=media)
        assert (cap.capture_state_timeout_s, cap.media_state_timeout_s) == (capture_state, media)
        return
    with pytest.raises(ValueError, match="capture_state_timeout_s .* media_state_timeout_s"):
        BrowserCapability(capture_state_timeout_s=capture_state, media_state_timeout_s=media)


class TestDomCapture:
    async def test_a_stuck_capture_costs_the_fragment_within_the_bound(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(dom_watchdog, "_DOM_CAPTURE_TIMEOUT_S", 0.1)
        watchdog = DOMWatchdog(_StuckCDP("DOM.getDocument"))  # type: ignore[arg-type]
        started = time.monotonic()
        with caplog.at_level(logging.ERROR), pytest.raises(TimeoutError, match="DOM capture"):
            await asyncio.wait_for(watchdog._on_browser_state_request(_event("browser.dom")), 5.0)
        assert 0.1 <= time.monotonic() - started < 2.0
        assert "the DOM capture got no reply within 0.1s" in caplog.text

    async def test_a_healthy_capture_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dom_watchdog, "_DOM_CAPTURE_TIMEOUT_S", 0.5)
        watchdog = DOMWatchdog(_StuckCDP())  # type: ignore[arg-type]
        summary = await watchdog._on_browser_state_request(_event("browser.dom"))
        assert summary is not None and isinstance(summary.fragments["browser.dom"], DOMObs)


class TestBrowserMetaReads:
    async def test_a_stuck_frame_tree_leaves_the_fragment_unstamped(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(watchdog_base, "OBSERVATION_READ_TIMEOUT_S", 0.05)
        watchdog = BrowserMetaWatchdog(_StuckCDP("Page.getFrameTree"))  # type: ignore[arg-type]
        with caplog.at_level(logging.WARNING):
            summary = await asyncio.wait_for(
                watchdog._on_browser_state_request(_event("browser.browser_meta")), 2.0
            )
        assert summary is not None and summary.document is None
        meta = summary.fragments["browser.browser_meta"]
        assert isinstance(meta, BrowserMetaObs)
        assert (meta.url, meta.page_title, meta.loader_id) == (URL, "Shop", None)
        assert "Page.getFrameTree got no reply within 0.05s" in caplog.text

    async def test_a_stuck_history_leaves_the_url_to_the_frame_tree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(watchdog_base, "OBSERVATION_READ_TIMEOUT_S", 0.05)
        watchdog = BrowserMetaWatchdog(_StuckCDP("Page.getNavigationHistory"))  # type: ignore[arg-type]
        summary = await asyncio.wait_for(
            watchdog._on_browser_state_request(_event("browser.browser_meta")), 2.0
        )
        assert summary is not None and summary.document is not None
        meta = summary.fragments["browser.browser_meta"]
        assert isinstance(meta, BrowserMetaObs)
        assert (meta.url, meta.page_title, meta.loader_id) == (URL, "", "L1")


class TestScreenshotStamp:
    async def test_a_stuck_frame_tree_after_the_capture_leaves_the_shot_unstamped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(watchdog_base, "OBSERVATION_READ_TIMEOUT_S", 0.05)
        watchdog = ScreenshotWatchdog(_StuckCDP("Page.getFrameTree"))  # type: ignore[arg-type]
        summary = await asyncio.wait_for(
            watchdog._on_browser_state_request(_event("browser.screenshot")), 2.0
        )
        assert summary is not None and summary.document is None
        assert summary.fragments["browser.screenshot"].image_bytes == PNG_BYTES  # type: ignore[attr-defined]


class _StuckOnce(FakeBrowser):
    """The fake browser, whose first ``DOM.getDocument`` is never answered."""

    def __init__(self) -> None:
        super().__init__()
        self.documents = 0

    async def handle(
        self, client: FakeClient, method: str, params: dict[str, Any], session_id: str | None
    ) -> Any:
        if method == "DOM.getDocument":
            self.documents += 1
            if self.documents == 1:
                await asyncio.Event().wait()
        return await super().handle(client, method, params, session_id)


async def test_a_stuck_dom_capture_costs_one_attempt_not_the_event(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Through the capability: the cycle re-fires and the second attempt observes."""
    monkeypatch.setattr(dom_watchdog, "_DOM_CAPTURE_TIMEOUT_S", 0.2)
    browser = _StuckOnce()
    cap = BrowserCapability()
    cap.backend = OwnedBrowser()  # type: ignore[assignment]
    cap._inject_cdp_factory(browser.client)
    await cap.setup()
    cap.apply_negotiation({"browser.dom"})
    try:
        await cap.begin_observation(1)
        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            # The event's own timeout is 30 s; the capture bound fires long before.
            dom = await asyncio.wait_for(cap.resolve_fragment("browser.dom"), 10.0)
        assert isinstance(dom, DOMObs)
        assert browser.documents == 2
        assert 0.2 <= time.monotonic() - started < 5.0
        assert "attempt 1/2 did not produce ['browser.dom']" in caplog.text
    finally:
        await cap.teardown()
