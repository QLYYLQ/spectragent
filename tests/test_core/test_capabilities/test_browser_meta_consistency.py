"""One cycle's evidence describes one document — the M18 stale-URL fix (§4.14).

M18 found ``browser.browser_meta``'s URL about 0.25 s older than the DOM and
screenshot of the same cycle: it ran first in the tab's serial observation
scope, and nothing in the capture recorded a loader id that could have
noticed. Now every contribution stamps the document it saw (``loaderId`` +
URL), ``browser_meta`` runs last and reads the frame tree, and a cycle whose
stamps name two documents is re-fired inside the existing attempt budget —
or refused, never served.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from agent_cdp import ConnectionType, ScopeGroup

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import _OBSERVE_MAX_ATTEMPTS
from spectragent.core.capabilities.browser.dom.capture import capture_target
from spectragent.core.capabilities.browser.dom.cleaning.browser_use import REQUIRED_COMPUTED_STYLES
from spectragent.core.capabilities.browser.dom.models import DOMState, TargetCapture
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.capabilities.browser.watchdogs.browser_meta_watchdog import (
    LAST,
    BrowserMetaWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.dom_watchdog import DOMWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
    DocumentStamp,
    describe_stamps,
    documents_disagree,
)
from spectragent.core.capabilities.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
from tests._helpers.fragment_samples import PNG_BYTES

# ── the rule ──────────────────────────────────────────────────────────


class TestDocumentsDisagree:
    def test_two_loaders_disagree(self) -> None:
        stamps = {"dom": DocumentStamp("L1", "http://a/"), "meta": DocumentStamp("L2", "http://b/")}
        assert documents_disagree(stamps) is True
        assert describe_stamps(stamps) == "dom → loader L1 http://a/; meta → loader L2 http://b/"

    def test_one_loader_agrees_even_across_urls(self) -> None:
        """A same-document change (pushState) keeps the loader: not a new document."""
        stamps = {
            "dom": DocumentStamp("L1", "http://a/#x"),
            "meta": DocumentStamp("L1", "http://a/#y"),
        }
        assert documents_disagree(stamps) is False

    def test_fewer_than_two_known_stamps_are_not_compared(self) -> None:
        assert documents_disagree({}) is False
        assert documents_disagree({"dom": DocumentStamp("L1")}) is False
        assert (
            documents_disagree({"dom": DocumentStamp("L1"), "meta": DocumentStamp(None, "u")})
            is False
        )


# ── the capability re-fires a cycle whose stamps disagree ─────────────


class _Scripted:
    """Watchdog doubles on one real scope, each answering per-attempt stamps."""

    def __init__(self, loaders: dict[str, list[str | None]]) -> None:
        self.loaders = loaders
        self.calls: dict[str, int] = dict.fromkeys(loaders, 0)

    def handler(self, name: str, fragment_key: str, fragment: Any):
        async def _on_state(event: BrowserStateRequestEvent) -> BrowserStateSummary:
            attempt = self.calls[name]
            self.calls[name] += 1
            plan = self.loaders[name]
            loader = plan[min(attempt, len(plan) - 1)]
            return BrowserStateSummary(
                fragments={fragment_key: fragment},
                document=DocumentStamp(loader, f"http://site/{loader}") if loader else None,
            )

        _on_state.__qualname__ = f"{name}_on_state"
        return _on_state


async def _cap_with(scripted: _Scripted) -> tuple[BrowserCapability, ScopeGroup]:
    cap = BrowserCapability()
    group = ScopeGroup("t")
    scope = await group.create_scope("T1", metadata={"target_id": "T1"})
    cap._default_session_id = "sid"
    cap._scopes["sid"] = scope
    cap.apply_negotiation({"browser.dom", "browser.screenshot", "browser.browser_meta"})
    fragments = {
        "dom": ("browser.dom", DOMObs(element_tree=DOMState(), selector_map={})),
        "shot": ("browser.screenshot", ScreenshotObs(image_bytes=PNG_BYTES)),
        "meta": ("browser.browser_meta", BrowserMetaObs(url="http://site/")),
    }
    for name in scripted.loaders:
        key, fragment = fragments[name]
        scope.connect(
            BrowserStateRequestEvent,
            scripted.handler(name, key, fragment),
            mode=ConnectionType.QUEUED,
        )
    return cap, group


class TestCycleConsistency:
    async def test_a_disagreeing_attempt_is_refired_and_the_agreeing_one_served(self) -> None:
        scripted = _Scripted({"dom": ["L2", "L2"], "shot": ["L2", "L2"], "meta": ["L1", "L2"]})
        cap, group = await _cap_with(scripted)
        try:
            meta = await cap.resolve_fragment("browser.browser_meta")
            assert meta.url == "http://site/"
            assert scripted.calls == {"dom": 2, "shot": 2, "meta": 2}
        finally:
            await group.close_all()

    async def test_exhausting_the_budget_raises_naming_every_stamp(self) -> None:
        scripted = _Scripted({"dom": ["L2"], "shot": ["L2"], "meta": ["L1"]})
        cap, group = await _cap_with(scripted)
        try:
            with pytest.raises(RuntimeError, match="different documents") as info:
                await cap.resolve_fragment("browser.dom")
            message = str(info.value)
            assert "meta_on_state → loader L1" in message
            assert "dom_on_state → loader L2" in message
            assert scripted.calls["dom"] == _OBSERVE_MAX_ATTEMPTS
            # Recorded on the cycle: the rest of the slice gets the same error.
            with pytest.raises(RuntimeError, match="different documents"):
                await cap.resolve_fragment("browser.screenshot")
            assert scripted.calls["dom"] == _OBSERVE_MAX_ATTEMPTS
        finally:
            await group.close_all()

    async def test_two_handlers_sharing_a_name_keep_both_stamps(self) -> None:
        scripted = _Scripted({"dom": ["L2"], "shot": ["L2"], "meta": ["L1"]})
        original = scripted.handler

        def _same_name(name: str, key: str, fragment: Any):
            handler = original(name, key, fragment)
            handler.__qualname__ = "Watchdog._on_browser_state_request"
            return handler

        scripted.handler = _same_name  # type: ignore[method-assign]
        cap, group = await _cap_with(scripted)
        try:
            with pytest.raises(RuntimeError, match="different documents"):
                await cap.resolve_fragment("browser.dom")
        finally:
            await group.close_all()

    async def test_unstamped_summaries_are_served_without_comparison(self) -> None:
        scripted = _Scripted({"dom": [None], "shot": [None], "meta": ["L9"]})
        cap, group = await _cap_with(scripted)
        try:
            await cap.resolve_fragment("browser.dom")
            assert scripted.calls == {"dom": 1, "shot": 1, "meta": 1}
        finally:
            await group.close_all()


# ── who stamps what ───────────────────────────────────────────────────


class _TreeCDP:
    """Answers the frame tree and navigation history; records the call order."""

    def __init__(self, *, loader: str = "L7", url: str = "http://site/page", **frame: Any) -> None:
        self.sent: list[str] = []
        self.frame = {"id": "F", "loaderId": loader, "url": url, **frame}

    async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
        self.sent.append(method)
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": dict(self.frame)}}
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 0, "entries": [{"url": "http://history/url", "title": "Title"}]}
        if method == "Page.captureScreenshot":
            import base64

            return {"data": base64.b64encode(PNG_BYTES).decode()}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": []}
        return {}

    def on_event(self, *_a: Any) -> None:
        return None

    def off_event(self, *_a: Any) -> None:
        return None


class TestStamps:
    async def test_browser_meta_reads_url_and_loader_from_the_frame_tree(self) -> None:
        cdp = _TreeCDP(loader="L7", url="http://site/now")
        wd = BrowserMetaWatchdog(cdp)  # type: ignore[arg-type]
        summary = await wd._on_browser_state_request(
            BrowserStateRequestEvent(fragments=frozenset({"browser.browser_meta"}))
        )
        assert summary is not None
        meta = summary.fragments["browser.browser_meta"]
        assert (meta.url, meta.loader_id, meta.page_title) == ("http://site/now", "L7", "Title")
        assert summary.document == DocumentStamp("L7", "http://site/now")

    async def test_a_hash_route_keeps_its_fragment(self) -> None:
        """``Frame.url`` has no fragment — CDP keeps it apart in ``urlFragment``."""
        cdp = _TreeCDP(loader="L7", url="http://site/app", urlFragment="#/inbox")
        wd = BrowserMetaWatchdog(cdp)  # type: ignore[arg-type]
        summary = await wd._on_browser_state_request(
            BrowserStateRequestEvent(fragments=frozenset({"browser.browser_meta"}))
        )
        assert summary is not None
        assert summary.fragments["browser.browser_meta"].url == "http://site/app#/inbox"
        assert summary.document == DocumentStamp("L7", "http://site/app#/inbox")

    async def test_an_error_page_keeps_the_url_the_agent_tried(self) -> None:
        cdp = _TreeCDP(url="chrome-error://chromewebdata/", unreachableUrl="http://tried/")
        wd = BrowserMetaWatchdog(cdp)  # type: ignore[arg-type]
        summary = await wd._on_browser_state_request(
            BrowserStateRequestEvent(fragments=frozenset({"browser.browser_meta"}))
        )
        assert summary is not None
        assert summary.fragments["browser.browser_meta"].url == "http://tried/"

    async def test_browser_meta_runs_last_in_the_cycle(self) -> None:
        order: list[str] = []
        group = ScopeGroup("t")
        scope = await group.create_scope("T1")

        class _CDP(_TreeCDP):
            async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
                if method == "Page.getFrameTree":
                    order.append("meta")
                return await super().send(method, params, session_id)

        async def _dom_like(_event: BrowserStateRequestEvent) -> None:
            await asyncio.sleep(0.01)
            order.append("dom")

        async def _shot_like(_event: BrowserStateRequestEvent) -> None:
            order.append("screenshot")

        meta = BrowserMetaWatchdog(_CDP())  # type: ignore[arg-type]
        meta.attach(scope, session_id="sid")  # connected first, yet runs last
        scope.connect(BrowserStateRequestEvent, _dom_like, mode=ConnectionType.QUEUED)
        scope.connect(BrowserStateRequestEvent, _shot_like, mode=ConnectionType.QUEUED)
        event = BrowserStateRequestEvent(fragments=frozenset({"browser.browser_meta"}))
        scope.emit(event)
        await event
        assert order == ["dom", "screenshot", "meta"]
        assert LAST < 0
        await group.close_all()

    async def test_the_screenshot_is_stamped_after_the_capture(self) -> None:
        cdp = _TreeCDP(loader="L3", url="http://site/shot")
        wd = ScreenshotWatchdog(cdp)  # type: ignore[arg-type]
        summary = await wd._on_browser_state_request(
            BrowserStateRequestEvent(fragments=frozenset({"browser.screenshot"}))
        )
        assert summary is not None
        assert cdp.sent == ["Page.captureScreenshot", "Page.getFrameTree"]
        assert summary.document == DocumentStamp("L3", "http://site/shot")

    async def test_the_cdp_capture_records_the_root_frame(self) -> None:
        cdp = _TreeCDP(loader="L5", url="http://site/dom")
        capture = await capture_target(
            cdp,  # type: ignore[arg-type]
            "sid",
            frames=None,
            js_click_listeners=False,
            required_computed_styles=REQUIRED_COMPUTED_STYLES,
        )
        assert (capture.loader_id, capture.url) == ("L5", "http://site/dom")

    async def test_the_dom_watchdog_stamps_and_hands_the_raw_capture_to_the_sink(self) -> None:
        handed: list[tuple[TargetCapture, float]] = []
        raw = TargetCapture(raw_dom={}, snapshot={}, ax_tree={}, loader_id="L8", url="http://d/")

        class _Cleaner:
            async def capture(self, _source: Any) -> tuple[TargetCapture, float]:
                return raw, 2.0

            async def clean(self, *_a: Any, **_k: Any) -> DOMState:
                return DOMState()

        wd = DOMWatchdog(
            _TreeCDP(),  # type: ignore[arg-type]
            cleaner=_Cleaner(),  # type: ignore[arg-type]
            raw_capture_sink=lambda capture, dpr: handed.append((capture, dpr)),
        )
        summary = await wd._on_browser_state_request(
            BrowserStateRequestEvent(fragments=frozenset({"browser.dom"}))
        )
        assert summary is not None
        assert summary.document == DocumentStamp("L8", "http://d/")
        assert handed == [(raw, 2.0)]

    def test_the_default_sink_is_a_no_op(self) -> None:
        cap = BrowserCapability()
        cap._cdp = _TreeCDP()  # type: ignore[assignment]
        ctx = cap._watchdog_context("sid")
        raw = TargetCapture(raw_dom={}, snapshot={}, ax_tree={})
        assert ctx.raw_capture_sink(raw, 1.0) is None
        assert DOMWatchdog.build(ctx)._raw_capture_sink is ctx.raw_capture_sink


# ── against a real Chrome ─────────────────────────────────────────────

_PUSH_TO_HASH_ROUTE = (
    b"<!doctype html><html><body>inbox"
    b"<script>history.pushState({}, '', '/app#/inbox')</script></body></html>"
)


class _OnePage(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server API
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_PUSH_TO_HASH_ROUTE)))
        self.end_headers()
        self.wfile.write(_PUSH_TO_HASH_ROUTE)

    def log_message(self, *_args: Any) -> None:
        return None


@pytest.mark.requires_chrome
async def test_a_pushed_hash_route_reaches_the_agent_whole() -> None:
    """A hash-routed page is reported with its fragment (Chrome keeps it apart in the tree)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OnePage)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{httpd.server_address[1]}"
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(
            cap.setup(negotiation={"browser.browser_meta"}, start_url=f"{origin}/start"), 60
        )
        url = ""
        for cycle in range(1, 21):
            await cap.begin_observation(cycle)
            url = (await cap.resolve_fragment("browser.browser_meta")).url
            if url.endswith("#/inbox"):
                break
            await asyncio.sleep(0.1)
        assert url == f"{origin}/app#/inbox"
    finally:
        await cap.teardown()
        httpd.shutdown()
        httpd.server_close()
