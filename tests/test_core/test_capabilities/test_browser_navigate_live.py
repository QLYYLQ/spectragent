"""``browser.navigate`` in a real Chrome: a late ``load`` warns, a missing commit fails (§4.14).

The 2026-10-14 LexBench screening found two sites (yicai.com, eeo.com.cn)
whose page was drawn but whose ``load`` event never came, and setup failed
after 30 s with "could not open start_url": the event's own 30 s clock
started before ``Page.navigate`` was even sent, so it always ran out before
the 30 s lifecycle wait, and the handler's promised warn-and-carry-on was
never reached. Here a local page holds an image whose response never
finishes, so its ``load`` never fires, the way those sites behaved:

- setup opens it after the lifecycle bound, logging a warning, and the
  observations that follow are captured; the first pays the settle wait's
  ``load`` budget once, the second does not wait for that ``load`` again;
- a server that never answers is a navigation that never commits, and it
  still fails — setup with it as the start page fails, naming it;
- a 204 commits nothing: Chrome answers with ``errorText`` and the navigation
  fails at once, without waiting out a lifecycle that cannot come.

The fake-CDP half is ``test_browser_actions/test_navigate_lifecycle.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.actions.navigate import NavigateAction
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.observations import BrowserMetaObs
from spectragent.schemas.action import Action

pytestmark = pytest.mark.requires_chrome

_WATCHDOG_LOG = "spectragent.core.capabilities.browser.watchdogs.action_watchdog"
#: The navigate action's bounds here (ms): short, so the test is quick.
LIFECYCLE_MS = 1500
COMMIT_MS = 1500
#: The observation's settle cap here (s): the first observation pays it once for the late load.
SETTLE_CAP_S = 1.0

_DRAWN = (
    "<!doctype html><title>drawn</title><link rel=icon href='data:,'>"
    "<p>The page is drawn; one image never finishes.</p><img src='/never.png'>"
)
_PLAIN = "<!doctype html><title>plain</title><link rel=icon href='data:,'><p>plain</p>"


class _Pages(BaseHTTPRequestHandler):
    #: Set when the module is done: every response held open ends.
    release: threading.Event

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/drawn.html", "/plain.html"):
            body = (_DRAWN if path == "/drawn.html" else _PLAIN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/never.png":
            # Headers and a first chunk, then nothing: the image never finishes.
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"\x89PNG")
            self.wfile.flush()
            self.release.wait(120)
        elif path == "/no-answer":
            self.release.wait(120)
        elif path == "/no-content":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *_args: Any) -> None:
        return None


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    release = threading.Event()
    handler = type("_ModulePages", (_Pages,), {"release": release})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()


def _capability(**navigate: Any) -> BrowserCapability:
    cap = BrowserCapability(actions=[NavigateAction(**navigate)], settle_max_wait_s=SETTLE_CAP_S)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    return cap


async def _meta(cap: BrowserCapability, cycle: int) -> BrowserMetaObs:
    await cap.begin_observation(cycle)
    meta = await cap.resolve_fragment("browser.browser_meta")
    assert isinstance(meta, BrowserMetaObs)
    return meta


async def test_a_page_whose_load_never_fires_opens_with_a_warning(
    origin: str, caplog: pytest.LogCaptureFixture
) -> None:
    url = f"{origin}/drawn.html"
    cap = _capability(lifecycle_timeout_ms=LIFECYCLE_MS)
    try:
        with caplog.at_level(logging.WARNING, logger=_WATCHDOG_LOG):
            started = time.monotonic()
            await asyncio.wait_for(
                cap.setup(negotiation={"browser.browser_meta"}, start_url=url), 60
            )
            took = time.monotonic() - started
        assert took >= LIFECYCLE_MS / 1000, "setup waited for the load, up to the bound"
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert (
            f"navigate: {url} committed, but its 'load' lifecycle event did not arrive within "
            f"{LIFECYCLE_MS / 1000:g}s; carrying on with the page as it is"
        ) in warnings

        first = await _meta(cap, 1)
        assert first.url == url and first.page_title == "drawn"
        assert first.settle is not None
        assert (first.settle.outcome, first.settle.waiting_for) == (
            "capped",
            ["load", "dom", "content"],
        )

        second = await _meta(cap, 2)
        assert second.url == url
        assert second.settle is not None and second.settle.outcome == "settled", (
            "the document's load is waited for at most one settle cap, across observations"
        )
        assert second.settle.waited_ms < SETTLE_CAP_S * 1000
    finally:
        await cap.teardown()


async def test_a_navigation_that_never_commits_fails_setup(origin: str) -> None:
    url = f"{origin}/no-answer"
    cap = _capability(timeout_ms=COMMIT_MS)
    try:
        with pytest.raises(RuntimeError) as info:
            await asyncio.wait_for(
                cap.setup(negotiation={"browser.browser_meta"}, start_url=url), 60
            )
        assert str(info.value) == (
            f"BrowserCapability.setup(): could not open start_url {url!r} — "
            f"navigation to {url!r} did not commit within {COMMIT_MS / 1000:g} s"
        )
    finally:
        await cap.teardown()


async def test_a_navigation_chrome_reports_failed_fails_at_once(origin: str) -> None:
    """A 204 commits nothing; the tab stays on the page it was on."""
    start = f"{origin}/plain.html"
    cap = _capability()  # the default bounds: 30 s each, none of it waited here
    try:
        await asyncio.wait_for(cap.setup(negotiation={"browser.browser_meta"}, start_url=start), 60)
        started = time.monotonic()
        result = await cap.execute_action(
            Action(name="browser.navigate", params={"url": f"{origin}/no-content"})
        )
        took = time.monotonic() - started
        assert result.success is False
        assert result.error == f"navigation to '{origin}/no-content' failed: net::ERR_ABORTED"
        assert took < 5.0
        assert (await _meta(cap, 1)).url == start
    finally:
        await cap.teardown()
