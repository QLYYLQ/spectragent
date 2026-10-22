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
page that never goes quiet is captured at the cap.

A page can also be quiet and empty: the 2026-10-22 lookahead data had two
captures recorded as settled that were a white app shell with a 3 px loading
bar, quiet for seconds while its script downloaded. Here a page shows only
such a bar until its one request answers, after much longer than the quiet
window: the observation waits past the window, until the content is drawn. A
page that stays empty is captured at the cap. Which pages count as showing
content is pinned on a set of small pages, through the probe itself
(``watch_page``). The fake-CDP half is ``test_browser_settle.py``.
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
from spectragent.core.capabilities.browser.settle import watch_page
from spectragent.schemas.action import Action

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

#: How long the server takes to answer the progress page's one request: three
#: quiet windows, so a wait that stopped at the first quiet window would capture
#: the bare loading bar.
DATA_DELAY_S = 1.5
#: A loading bar along the top edge, 3 px tall and painted by CSS, the way the
#: blank 10jqka captures showed one; its CSS animation changes no DOM.
_BAR = (
    "<style>#bar{position:fixed;left:0;top:0;height:3px;width:0;background:#5b8def;"
    "animation:grow 3s linear forwards}@keyframes grow{to{width:100%}}</style>"
    "<div id=bar></div>"
)
_PROGRESS = (
    "<!doctype html><title>progress</title><link rel=icon href='data:,'>" + _BAR + "<script>"
    "fetch('/data').then((answer) => answer.text()).then((text) => {"
    "  document.getElementById('bar').remove();"
    "  const box = document.createElement('div');"
    f"  box.style.cssText = 'position:fixed;left:{BOX[0]}px;top:{BOX[1]}px;"
    f"width:{BOX[2]}px;height:{BOX[3]}px;background:rgb{GREEN}';"
    "  box.textContent = text;"
    "  document.body.appendChild(box);"
    "});"
    "</script>"
)
#: The same bar with nothing ever coming: a page that stays empty.
_EMPTY = "<!doctype html><title>empty</title><link rel=icon href='data:,'>" + _BAR

#: Small pages, and whether their viewport shows content by the rule (``settle.py``).
_HEAD = "<!doctype html><meta charset=utf-8><link rel=icon href='data:,'>"
_VERDICT_PAGES: dict[str, tuple[str, bool]] = {
    "text": (_HEAD + "<p>A line of text</p>", True),
    "loading_bar": (_HEAD + _BAR, False),
    "spinner": (
        # Bootstrap's spinner: a CSS-drawn ring and "Loading..." for screen readers only.
        _HEAD + "<style>.ring{width:2rem;height:2rem;border:.25em solid #333;"
        "border-right-color:transparent;border-radius:50%}"
        ".sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;"
        "clip:rect(0,0,0,0);white-space:nowrap;border:0}</style>"
        "<div class=ring role=status><span class=sr>Loading...</span></div>",
        False,
    ),
    "faded_root": (_HEAD + "<div style='opacity:0'><h1>Not revealed yet</h1></div>", False),
    "visible_child": (
        _HEAD + "<div style='visibility:hidden'><h1 style='visibility:visible'>Shown</h1></div>",
        True,
    ),
    "display_contents": (_HEAD + "<div style='display:contents'>No box of its own</div>", True),
    "shadow_text": (
        _HEAD + "<x-card></x-card><script>customElements.define('x-card', class extends "
        "HTMLElement { constructor() { super(); this.attachShadow({mode: 'open'})"
        ".innerHTML = '<p>In a shadow root</p>'; } });</script>",
        True,
    ),
    "empty_svg": (_HEAD + "<svg width=600 height=400></svg>", False),
    "thin_svg": (
        _HEAD + "<svg width=1280 height=3><rect width=600 height=3 fill=blue /></svg>",
        False,
    ),
    "icon": (_HEAD + "<svg width=24 height=24><circle cx=12 cy=12 r=10 /></svg>", True),
    "button_at_the_edge": (
        # Its label is centred beyond the right edge; its box shows 80 px of it.
        _HEAD + "<style>body{width:2400px}</style><button style='position:absolute;"
        "left:1200px;top:40px;width:200px;height:40px'>Edge</button>",
        True,
    ),
    "loading_image": (
        # Added after the load, so the navigation does not wait for it; never finishes here.
        _HEAD + "<script>addEventListener('load', () => {"
        "const image = new Image(200, 200); image.src = '/held.png';"
        "document.body.appendChild(image); });</script>",
        False,
    ),
    "canvas": (_HEAD + "<canvas width=300 height=150></canvas>", True),
    "colour_block": (_HEAD + "<div style='width:400px;height:300px;background:red'></div>", False),
    "text_offscreen": (_HEAD + "<p style='position:absolute;left:-9999px'>Away</p>", False),
}
#: How long the probe looks at a page judged empty before it reports: a page
#: showing content answers at once, so this is only what an empty page costs.
VERDICT_BUDGET_S = 0.3


class _Pages(BaseHTTPRequestHandler):
    #: Set when the module is done: a response held open ends.
    release: threading.Event

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        pages = {"/late.html": _LATE, "/busy.html": _BUSY}
        pages.update({"/progress.html": _PROGRESS, "/empty.html": _EMPTY})
        pages.update({f"/{name}.html": page for name, (page, _) in _VERDICT_PAGES.items()})
        if path == "/part":
            time.sleep(PART_DELAY_S)
            body = f"part {self.path.rsplit('=', 1)[-1]} of 3".encode()
            kind = "text/plain"
        elif path == "/data":
            time.sleep(DATA_DELAY_S)
            body = LATE_TEXT.encode()
            kind = "text/plain"
        elif path == "/held.png":
            self.release.wait(120)
            body, kind = b"", "image/png"
        elif path in pages:
            body = pages[path].encode()
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


async def test_a_page_showing_only_a_loading_bar_is_waited_for_until_its_content(
    origin: str,
) -> None:
    """Quiet from its load until its request answers: the window alone would capture the bar."""
    observed, took = await _observe(origin, "progress.html", {"browser.screenshot"})
    shot = observed["browser.screenshot"]
    assert isinstance(shot, ScreenshotObs)
    assert _box_colour(shot) == GREEN, "captured before the page drew its content"
    assert shot.settle is not None
    assert (shot.settle.outcome, shot.settle.waiting_for) == ("settled", [])
    # The request was in flight before the wait began; the wait still outlasted the
    # quiet window it used to stop at by most of the request's delay.
    assert shot.settle.waited_ms >= DATA_DELAY_S * 1000 - 500
    assert took < 5.0


async def test_a_page_that_stays_empty_is_captured_at_the_cap(origin: str) -> None:
    observed, took = await _observe(
        origin, "empty.html", {"browser.screenshot"}, settle_max_wait_s=1.5
    )
    shot = observed["browser.screenshot"]
    assert isinstance(shot, ScreenshotObs) and shot.image_bytes
    assert shot.settle is not None
    assert (shot.settle.outcome, shot.settle.waiting_for) == ("capped", ["content"])
    assert 1400 <= shot.settle.waited_ms < 2500
    assert took < 4.0


async def test_what_counts_as_content(origin: str) -> None:
    """The in-page check on small pages: text and drawn elements count, CSS colour does not."""
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(negotiation={"browser.screenshot"}), 60)
        sid = cap._default_session_id
        assert cap._cdp is not None and sid is not None
        verdicts: dict[str, bool] = {}
        for name in ["about:blank", *_VERDICT_PAGES]:
            url = name if name == "about:blank" else f"{origin}/{name}.html"
            result = await cap.execute_action(Action(name="browser.navigate", params={"url": url}))
            assert result.success, result.error
            watch = await watch_page(
                cap._cdp,
                session_id=sid,
                frame_id=cap._main_frame_ids[sid],
                quiet_ms=0,
                budget_s=VERDICT_BUDGET_S,
            )
            assert watch.error is None and not watch.document_gone, (name, watch)
            verdicts[name] = watch.content
        expected = {"about:blank": True} | {
            name: shows for name, (_page, shows) in _VERDICT_PAGES.items()
        }
        assert verdicts == expected
    finally:
        await cap.teardown()
