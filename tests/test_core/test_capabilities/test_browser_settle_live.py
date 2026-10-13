"""The observation waits for the page to settle, in a real Chrome (``settle.py``, §4.14).

The live smoke run found observations taken before the page had rendered:
the agent decided on a blank page, and a replay (served fast from the archive)
captured the finished one, so the restoration's screenshot check refused it.
Here a local page renders its main content only after a chain of three
requests that each take 300 ms, the way a page fetches its data after its
load: right after the load there is only "Loading…".

With the wait (the default), the observation that follows the start URL holds
the late content — in the screenshot, and in a DOM-only observation alike,
because the wait belongs to the observation, not to one fragment. With the
wait turned off (``settle_max_wait_s: 0``) the same observation is taken at
once and misses it, which is what this test would see without the change. A
page that never goes quiet is captured at the cap. The fake-CDP half is
``test_browser_settle.py``.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from PIL import Image

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.observations import DOMObs, ScreenshotObs

pytestmark = pytest.mark.requires_chrome

#: How long the server takes to answer each of the late page's three requests.
PART_DELAY_S = 0.3
#: The late content: a green box (CSS px) and its text.
BOX = (100, 200, 400, 200)
GREEN = (0, 160, 0)
LATE_TEXT = "Late content"

_LATE = (
    "<!doctype html><title>late</title><link rel=icon href='data:,'>"
    "<p id=status>Loading…</p>"
    "<script>"
    "(async () => {"
    "  for (const n of [1, 2, 3]) {"
    "    const part = await (await fetch('/part?n=' + n)).text();"
    "    document.getElementById('status').textContent = part;"
    "  }"
    "  const box = document.createElement('div');"
    "  box.id = 'late';"
    f"  box.style.cssText = 'position:fixed;left:{BOX[0]}px;top:{BOX[1]}px;"
    f"width:{BOX[2]}px;height:{BOX[3]}px;background:rgb{GREEN}';"
    f"  box.textContent = '{LATE_TEXT}';"
    "  document.body.appendChild(box);"
    "})();"
    "</script>"
)
_BUSY = (
    "<!doctype html><title>busy</title><link rel=icon href='data:,'>"
    "<p id=clock></p>"
    "<script>setInterval(() => {"
    "document.getElementById('clock').textContent = String(performance.now());"
    "}, 100);</script>"
)


class _Pages(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/part":
            time.sleep(PART_DELAY_S)
            body = f"part {self.path.rsplit('=', 1)[-1]} of 3".encode()
            kind = "text/plain"
        elif path in ("/late.html", "/busy.html"):
            body = (_LATE if path == "/late.html" else _BUSY).encode()
            kind = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return None


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


async def _observe(
    origin: str, page: str, fragments: set[str], **knobs: Any
) -> tuple[dict[str, Any], float]:
    """The first observation after the start URL: its fragments, and how long it took."""
    cap = BrowserCapability(**knobs)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(negotiation=fragments, start_url=f"{origin}/{page}"), 60)
        await cap.begin_observation(1)
        started = time.monotonic()
        observed = {key: await cap.resolve_fragment(key) for key in sorted(fragments)}
        return observed, time.monotonic() - started
    finally:
        await cap.teardown()


def _box_colour(shot: ScreenshotObs) -> tuple[int, int, int]:
    """The screenshot's colour at the late box's centre, in image pixels."""
    assert shot.image_bytes
    scale = shot.image_width / shot.viewport_width if shot.viewport_width else 1.0
    x, y = BOX[0] + BOX[2] // 2, BOX[1] + BOX[3] // 2
    with Image.open(io.BytesIO(shot.image_bytes)) as image:
        pixel = image.convert("RGB").getpixel((round(x * scale), round(y * scale)))
    assert isinstance(pixel, tuple)
    return pixel[0], pixel[1], pixel[2]


def _text(dom: DOMObs) -> str:
    return " ".join(dom.element_tree.serialized_dom.split())


async def test_the_screenshot_holds_what_the_page_renders_after_its_load(origin: str) -> None:
    observed, took = await _observe(origin, "late.html", {"browser.screenshot"})
    shot = observed["browser.screenshot"]
    assert isinstance(shot, ScreenshotObs)
    assert _box_colour(shot) == GREEN, "the late content is missing from the screenshot"
    assert shot.settle is not None and shot.settle.outcome == "settled"
    # Three requests of 300 ms each, then the quiet window: the wait saw them through.
    assert shot.settle.waited_ms >= 3 * PART_DELAY_S * 1000
    assert took < 5.0


async def test_a_dom_only_observation_waits_the_same(origin: str) -> None:
    observed, _took = await _observe(origin, "late.html", {"browser.dom"})
    dom = observed["browser.dom"]
    assert isinstance(dom, DOMObs)
    assert LATE_TEXT in _text(dom) and "part 3 of 3" in _text(dom)
    assert dom.settle is not None and dom.settle.outcome == "settled"
    assert dom.settle.waited_ms >= 3 * PART_DELAY_S * 1000


async def test_without_the_wait_the_observation_misses_it(origin: str) -> None:
    """The control: what every observation saw before the wait existed."""
    observed, _took = await _observe(
        origin, "late.html", {"browser.dom", "browser.screenshot"}, settle_max_wait_s=0
    )
    shot, dom = observed["browser.screenshot"], observed["browser.dom"]
    assert isinstance(shot, ScreenshotObs) and isinstance(dom, DOMObs)
    assert _box_colour(shot) != GREEN
    assert LATE_TEXT not in _text(dom) and "Loading" in _text(dom)
    assert shot.settle is None and dom.settle is None


async def test_a_page_that_never_goes_quiet_is_captured_at_the_cap(origin: str) -> None:
    observed, took = await _observe(
        origin, "busy.html", {"browser.screenshot"}, settle_max_wait_s=1.5
    )
    shot = observed["browser.screenshot"]
    assert isinstance(shot, ScreenshotObs) and shot.image_bytes
    assert shot.settle is not None
    assert (shot.settle.outcome, shot.settle.waiting_for) == ("capped", ["dom"])
    assert 1400 <= shot.settle.waited_ms < 2500
    assert took < 4.0
