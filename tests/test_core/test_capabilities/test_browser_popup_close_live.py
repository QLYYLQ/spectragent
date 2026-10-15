"""A popup that closes itself lets go of its navigation feed, in a real Chrome (§4.14).

The capability follows each attached tab's main frame with a ``NavigationFeed``
on the control connection (the form-submit fix). A popup the page closes on
its own (``window.close()``) never passes through ``close_tab``: what tells the
feed to let go is Chrome's ``Target.detachedFromTarget`` for its session, sent
on the browser session. ``test_browser_navigation_settle.py`` pins the rule
against fakes; this shows Chrome sends it.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider

pytestmark = pytest.mark.requires_chrome

_PAGES = {
    "/opener.html": b"<!doctype html><title>opener</title><p>opener</p>"
    b"<script>window.open('/popup.html');</script>",
    "/popup.html": b"<!doctype html><title>popup</title><p>popup</p>",
}


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = _PAGES.get(self.path.split("?", 1)[0])
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return None


async def _until(condition: Any, what: str, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


async def test_a_popup_that_closes_itself_releases_its_feed() -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{httpd.server_address[1]}"
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(
            cap.setup(negotiation={"browser.browser_meta"}, start_url=f"{origin}/opener.html"), 60
        )
        opener = cap._default_session_id
        assert opener is not None
        opener_target = cap._session_to_target[opener]
        popup = None
        for _ in range(100):
            try:
                popup = (await cap.switch_tab()).session_id  # adopt the popup the page opened
                break
            except ValueError:  # not open yet
                await asyncio.sleep(0.1)
        assert popup is not None and popup != opener
        await cap.switch_tab(opener_target)  # focus back on the opener
        feed = cap._navigation_feeds[popup]
        assert feed._callbacks, "the adopted popup is followed"
        assert cap._cdp is not None
        await cap._cdp.send("Runtime.evaluate", {"expression": "window.close()"}, session_id=popup)
        await _until(lambda: not feed._callbacks, "the popup's feed to let go")
        assert not feed.navigation.in_flight
        assert cap._navigation_feeds[opener]._callbacks, "the opener is still followed"
    finally:
        await cap.teardown()
        httpd.shutdown()
        httpd.server_close()
