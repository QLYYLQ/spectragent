"""Frames against a real headless Chrome: observe, then act by index.

Everything the unit tests assume about Chrome is only an assumption until a
browser agrees, so this module drives the whole stack — ``BrowserCapability``
with a ``LocalChromeProvider`` backend — against pages served from two local
origins. ``127.0.0.1`` and ``localhost`` are different sites, so under site
isolation the ``localhost`` iframe runs out of process: its document is not in
the tab's ``DOM.getDocument``, and every backend node id in it may equal one
in the page.

The main page::

    127.0.0.1  main.html, scrolled
    ├── button#main-btn
    ├── iframe#same     border 5 / padding 7, same origin, scrolled
    │   └── button#same-btn, input#same-input
    ├── div#host ── open shadow root ── button#shadow-btn
    └── iframe#cross    border 4 / padding 6, localhost (out of process), scrolled
        ├── div#cb role=checkbox (toggles aria-checked)
        ├── input#cross-input, select#cross-select
        └── iframe#nested  border 3 / padding 2, 127.0.0.1 — out of process again
            └── button#nested-btn

Each click is checked through the *next observation* — the element's own
attribute flips — so the loop the agent runs is the loop under test. The
remaining tests serve pages of their own for one situation each.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import spectragent.core.capabilities.browser.dom.serializers.text_serializer  # noqa: F401
from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.dom.models import (
    FRAME_HANDLE_BASE,
    DOMElementNode,
    DOMState,
)
from spectragent.core.capabilities.browser.input import WindMouseTrajectory
from spectragent.core.capabilities.browser.watchdogs.dom_watchdog import DOMWatchdog
from spectragent.schemas.action import Action, ActionResult

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 90

# ``data-clicked`` is written by the element's own click handler, so a click
# that lands anywhere else leaves it unset.
_MARK_CLICKED = "this.setAttribute('data-clicked', String(Number(this.dataset.clicked || 0) + 1))"


def _main_html(cross_origin: str) -> str:
    return f"""<!doctype html><html><body style="margin:0">
<div style="height:260px">top</div>
<button id="main-btn" onclick="{_MARK_CLICKED}">Main</button>
<iframe id="same" src="/same.html"
  style="border:5px solid red;padding:7px;width:300px;height:150px"></iframe>
<div id="host"></div>
<iframe id="cross" src="{cross_origin}/cross.html"
  style="border:4px solid blue;padding:6px;width:320px;height:220px;margin-left:40px"></iframe>
<div style="height:1600px">tail</div>
<script>
  const root = document.getElementById('host').attachShadow({{mode: 'open'}});
  const button = document.createElement('button');
  button.id = 'shadow-btn';
  button.textContent = 'Shadow';
  button.onclick = function () {{ {_MARK_CLICKED}; }};
  root.appendChild(button);
</script>
</body></html>"""


_SAME_HTML = f"""<!doctype html><html><body style="margin:0">
<div style="height:90px">same top</div>
<button id="same-btn" onclick="{_MARK_CLICKED}">Same</button>
<input id="same-input">
<div style="height:600px">same tail</div>
</body></html>"""


def _cross_html(main_origin: str) -> str:
    return f"""<!doctype html><html><body style="margin:0">
<div style="height:40px">cross top</div>
<div id="cb" role="checkbox" aria-checked="false" tabindex="0"
  style="width:28px;height:28px;margin-left:6px;background:#ddd"
  onclick="this.setAttribute('aria-checked', String(this.getAttribute('aria-checked') !== 'true'))"></div>
<input id="cross-input">
<select id="cross-select"><option value="a">A</option><option value="b">B</option></select>
<br>
<iframe id="nested" src="{main_origin}/nested.html"
  style="border:3px solid green;padding:2px;width:220px;height:70px"></iframe>
<div style="height:600px">cross tail</div>
<script>window.addEventListener('load', () => window.scrollTo(0, 15));</script>
</body></html>"""


_NESTED_HTML = f"""<!doctype html><html><body style="margin:0">
<div style="height:12px"></div>
<button id="nested-btn" style="margin-left:9px" onclick="{_MARK_CLICKED}">Nested</button>
</body></html>"""


class _Server:
    """Serves the fixture pages; one instance per origin port."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes

        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — http.server API
                body = server.routes.get(self.path.split("?")[0])
                if body is None:
                    self.send_error(404)
                    return
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: Any) -> None:
                return None

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@dataclass
class Site:
    main_origin: str
    cross_origin: str
    #: The servers themselves, so a test can add pages of its own.
    main: _Server
    cross: _Server


@pytest.fixture
def site() -> Any:
    main = _Server({})
    cross = _Server({})
    main_origin = f"http://127.0.0.1:{main.port}"
    cross_origin = f"http://localhost:{cross.port}"
    main.routes.update(
        {
            "/main.html": _main_html(cross_origin),
            "/same.html": _SAME_HTML,
            "/nested.html": _NESTED_HTML,
        }
    )
    cross.routes.update({"/cross.html": _cross_html(main_origin)})
    try:
        yield Site(main_origin, cross_origin, main, cross)
    finally:
        main.close()
        cross.close()


@asynccontextmanager
async def browser(
    url: str, *, trajectory: Any = None, scroll_main_page: bool = False
) -> AsyncIterator[BrowserCapability]:
    cap = BrowserCapability()
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    if trajectory is not None:
        cap.mouse_trajectory = trajectory
    try:
        await cap.setup(start_url=url)
        if scroll_main_page:
            # Scroll the page and both scrollable frames, so every offset term is non-zero.
            await evaluate(
                cap,
                None,
                "window.scrollTo(0, 150); document.getElementById('same').contentWindow.scrollTo(0, 30); 1",
            )
        yield cap
    finally:
        await cap.teardown()


def main_page(site: Site, **kwargs: Any) -> Any:
    return browser(f"{site.main_origin}/main.html", scroll_main_page=True, **kwargs)


async def evaluate(cap: BrowserCapability, session_id: str | None, expression: str) -> Any:
    result = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=session_id or cap.default_session_id,
    )
    return (result or {}).get("result", {}).get("value")


async def observe(cap: BrowserCapability) -> DOMState:
    fragment = await cap.resolve_fragment("browser.dom")
    # Reading without acting: start a fresh cycle for the next observe.
    cap._cycle_cache.pop(cap.default_session_id, None)
    return fragment.element_tree


def element(state: DOMState, element_id: str) -> tuple[int, DOMElementNode]:
    for key, node in state.selector_map.items():
        if node.attributes.get("id") == element_id:
            return key, node
    raise AssertionError(f"#{element_id} has no index:\n{state.serialized_dom}")


def indexed(state: DOMState, element_id: str) -> bool:
    return any(n.attributes.get("id") == element_id for n in state.selector_map.values())


async def run(cap: BrowserCapability, name: str, **params: Any) -> ActionResult:
    return await cap.execute_action(Action(name=name, params=params))


async def act(cap: BrowserCapability, name: str, **params: Any) -> None:
    result = await run(cap, name, **params)
    assert result.success, result.error


def dom_watchdog(cap: BrowserCapability) -> DOMWatchdog:
    target = cap._session_to_target[cap.default_session_id]
    return next(wd for wd in cap._watchdogs[target] if isinstance(wd, DOMWatchdog))


async def chrome_box(
    cap: BrowserCapability, node: DOMElementNode
) -> tuple[float, float, float, float]:
    """``node``'s border box ``(x, y, w, h)`` in the page viewport, per Chrome's own box model.

    ``DOM.getBoxModel`` in the node's session (local-root coordinates), plus
    ``DOM.getBoxModel(...).content`` of the owner for each out-of-process
    boundary.
    """
    box = await cap.cdp.send(
        "DOM.getBoxModel", {"backendNodeId": node.backend_node_id}, session_id=node.session_id
    )
    x, y = box["model"]["border"][0], box["model"]["border"][1]
    w, h = box["model"]["width"], box["model"]["height"]
    for owner in node.frame_owners():
        crosses_process = (
            owner.content_document is not None
            and owner.content_document.session_id != owner.session_id
        )
        if not crosses_process:
            continue
        owner_box = await cap.cdp.send(
            "DOM.getBoxModel", {"backendNodeId": owner.backend_node_id}, session_id=owner.session_id
        )
        x += owner_box["model"]["content"][0]
        y += owner_box["model"]["content"][1]
    return x, y, w, h


async def page_rect(cap: BrowserCapability, js_element: str) -> tuple[float, float, float, float]:
    """``getBoundingClientRect()`` of ``js_element``, evaluated from the top page.

    Relative to the viewport of the element's own document — so it only
    reaches same-origin frames, and only as far as the caller adds up.
    """
    rect = await evaluate(
        cap,
        None,
        f"(() => {{ const r = {js_element}.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }})()",
    )
    return rect[0], rect[1], rect[2], rect[3]


async def test_frame_content_is_indexed_where_chrome_draws_it(site: Site) -> None:
    async def body() -> None:
        async with main_page(site) as cap:
            state = await observe(cap)
            tab = cap.default_session_id

            for element_id in ("main-btn", "same-btn", "same-input", "shadow-btn"):
                key, node = element(state, element_id)
                assert node.session_id == tab and key == node.backend_node_id
            frame_nodes = [
                element(state, i) for i in ("cb", "cross-input", "cross-select", "nested-btn")
            ]
            for key, node in frame_nodes:
                assert node.session_id != tab
                assert key >= FRAME_HANDLE_BASE
            assert element(state, "cb")[1].session_id != element(state, "nested-btn")[1].session_id

            # Page-space bounds agree with Chrome's own box model, through
            # borders, paddings, three scroll offsets and two process hops —
            # both the snapshot's and the live point a click is aimed at.
            actions = cap._action_watchdog_for(tab)
            assert actions is not None
            scroll_x, scroll_y = state.scroll_info.scroll_x, state.scroll_info.scroll_y
            for element_id in ("same-btn", "shadow-btn", "cb", "cross-input", "nested-btn"):
                _, node = element(state, element_id)
                assert node.bounds is not None and node.backend_node_id is not None
                live = await actions._get_live_viewport_center(
                    node.backend_node_id, node.session_id, node.frame_owners()
                )
                x, y, w, h = await chrome_box(cap, node)
                assert abs((node.bounds.x - scroll_x) - x) <= 1, element_id
                assert abs((node.bounds.y - scroll_y) - y) <= 1, element_id
                assert live is not None, element_id
                assert abs(live[0] - (x + w / 2)) <= 1, (element_id, live, (x, y, w, h))
                assert abs(live[1] - (y + h / 2)) <= 1, (element_id, live, (x, y, w, h))

            # The same-origin frame is reachable by script from the page, so
            # its button's box has a second, fully independent witness.
            same_x, same_y, same_w, same_h = await page_rect(
                cap, "document.getElementById('same').contentDocument.getElementById('same-btn')"
            )
            frame_x, frame_y, _, _ = await page_rect(cap, "document.getElementById('same')")
            _, same_btn = element(state, "same-btn")
            live = await actions._get_live_viewport_center(
                same_btn.backend_node_id or 0, same_btn.session_id, same_btn.frame_owners()
            )
            assert live is not None
            expected = (
                frame_x + 5 + 7 + same_x + same_w / 2,
                frame_y + 5 + 7 + same_y + same_h / 2,
            )
            assert abs(live[0] - expected[0]) <= 1 and abs(live[1] - expected[1]) <= 1

            lines = state.serialized_dom.splitlines()
            cross_key, _ = element(state, "cross")  # 320x220: a target of its own
            cross = next(i for i, line in enumerate(lines) if "<iframe id=cross" in line)
            assert lines[cross].strip().startswith(f"[{cross_key}]<iframe")
            assert any(f"[{element(state, 'cb')[0]}]<div" in line for line in lines[cross:])

    await asyncio.wait_for(body(), _TIMEOUT_S)


async def test_click_by_index_lands_in_every_kind_of_frame(site: Site) -> None:
    async def body() -> None:
        async with main_page(site) as cap:
            state = await observe(cap)
            for element_id in ("main-btn", "same-btn", "shadow-btn", "nested-btn"):
                await act(cap, "browser.click", index=element(state, element_id)[0])
                state = await observe(cap)
                assert element(state, element_id)[1].attributes.get("data-clicked") == "1", (
                    element_id
                )

            key, node = element(state, "cb")
            assert node.attributes.get("aria-checked") == "false"
            await act(cap, "browser.click", index=key)
            state = await observe(cap)
            assert element(state, "cb")[1].attributes.get("aria-checked") == "true"

    await asyncio.wait_for(body(), _TIMEOUT_S)


async def test_type_and_select_reach_the_out_of_process_frame(site: Site) -> None:
    async def body() -> None:
        async with main_page(site) as cap:
            state = await observe(cap)
            _, cross_input = element(state, "cross-input")
            await act(
                cap, "browser.type", index=element(state, "cross-input")[0], text="typed in a frame"
            )
            await act(
                cap, "browser.select_dropdown", index=element(state, "cross-select")[0], value="b"
            )
            await act(
                cap, "browser.type", index=element(state, "same-input")[0], text="same origin"
            )

            frame = cross_input.session_id
            assert (
                await evaluate(cap, frame, "document.getElementById('cross-input').value")
                == "typed in a frame"
            )
            assert (
                await evaluate(cap, frame, "document.getElementById('cross-select').value") == "b"
            )
            missing = await run(
                cap,
                "browser.select_dropdown",
                index=element(state, "cross-select")[0],
                text="missing",
            )
            assert not missing.success and "no enabled option" in (missing.error or "")
            assert (
                await evaluate(cap, frame, "document.getElementById('cross-select').value") == "b"
            )
            assert (
                await evaluate(
                    cap,
                    None,
                    "document.getElementById('same').contentDocument.getElementById('same-input').value",
                )
                == "same origin"
            )

    await asyncio.wait_for(body(), _TIMEOUT_S)


async def test_history_actions_navigate_real_chrome_and_refuse_an_empty_direction(
    site: Site,
) -> None:
    site.main.routes["/first.html"] = "<html><body>first</body></html>"
    site.main.routes["/second.html"] = "<html><body>second</body></html>"
    async with browser(f"{site.main_origin}/first.html") as cap:
        empty_forward = await run(cap, "browser.go_forward")
        assert not empty_forward.success
        await act(cap, "browser.navigate", url=f"{site.main_origin}/second.html")
        await act(cap, "browser.go_back")
        assert await evaluate(cap, None, "location.pathname") == "/first.html"
        await act(cap, "browser.go_forward")
        assert await evaluate(cap, None, "location.pathname") == "/second.html"


async def test_humanized_click_in_a_nested_frame(site: Site) -> None:
    async def body() -> None:
        async with main_page(site, trajectory=WindMouseTrajectory(seed=3)) as cap:
            state = await observe(cap)
            await act(cap, "browser.click", index=element(state, "nested-btn")[0])
            state = await observe(cap)
            assert element(state, "nested-btn")[1].attributes.get("data-clicked") == "1"

    await asyncio.wait_for(body(), _TIMEOUT_S)


async def test_an_editor_frame_is_typed_into_through_its_owner(site: Site) -> None:
    """Editors whose document *is* the editable surface have no inner element to index."""
    site.main.routes["/editor.html"] = (
        "<!doctype html><body contenteditable=true style='margin:0'>same-origin editor</body>"
    )
    site.cross.routes["/design.html"] = (
        "<!doctype html><body style='margin:0'>cross-site editor"
        "<script>document.designMode = 'on'</script></body>"
    )
    site.main.routes["/editors.html"] = f"""<!doctype html><body style="margin:0">
<iframe id="ed" src="/editor.html" style="width:400px;height:200px"></iframe>
<iframe id="xed" src="{site.cross_origin}/design.html" style="width:400px;height:200px"></iframe>
</body>"""

    async def body() -> None:
        async with browser(f"{site.main_origin}/editors.html") as cap:
            await asyncio.sleep(0.5)
            state = await observe(cap)
            await act(cap, "browser.type", index=element(state, "ed")[0], text="typed here")
            await act(cap, "browser.type", index=element(state, "xed")[0], text="and there")

            assert (
                await evaluate(
                    cap, None, "document.getElementById('ed').contentDocument.body.innerText"
                )
                == "typed here"
            )
            xed = element(state, "xed")[1].content_document
            assert xed is not None and xed.session_id != cap.default_session_id
            assert await evaluate(cap, xed.session_id, "document.body.innerText") == "and there"

    await asyncio.wait_for(body(), _TIMEOUT_S)


async def test_a_hidden_cross_site_frame_offers_nothing_to_click(site: Site) -> None:
    """A ``visibility:hidden`` frame hit-tests to the page beneath it."""
    site.cross.routes["/ghost.html"] = (
        "<!doctype html><body style='margin:0'>"
        f"<button id='ghost' style='width:200px;height:60px' onclick=\"{_MARK_CLICKED}\">ghost</button></body>"
    )
    site.main.routes["/hidden.html"] = f"""<!doctype html><body style="margin:0">
<button id="under" onclick="{_MARK_CLICKED}"
  style="position:absolute;left:60px;top:15px;width:80px;height:30px">under</button>
<iframe id="ghost-frame" src="{site.cross_origin}/ghost.html"
  style="position:absolute;left:0;top:0;width:200px;height:60px;border:0;visibility:hidden"></iframe>
</body>"""

    async def body() -> None:
        async with browser(f"{site.main_origin}/hidden.html") as cap:
            await asyncio.sleep(0.5)
            state = await observe(cap)
            assert not indexed(state, "ghost"), state.serialized_dom
            assert "ghost" not in state.serialized_dom.replace("ghost-frame", "")
            await act(cap, "browser.click", index=element(state, "under")[0])
            state = await observe(cap)
            assert element(state, "under")[1].attributes.get("data-clicked") == "1"

    await asyncio.wait_for(body(), _TIMEOUT_S)


#: A transform with all three parts, so a mapping that gets any one of them
#: wrong misses: the scale moves the far corner, the rotation tilts it, the
#: translate shifts the whole frame.
_FRAME_TRANSFORM = "scale(0.8) rotate(3deg) translate(20px, 10px)"


def _transform_target(button_id: str) -> str:
    """A 320x180 frame whose only control sits near its far corner.

    Distance from the frame's origin is the point: a scale error grows with
    it, so a 44x22 target at (240, 140) is missed by a mapping that adds the
    owner's content origin and stops there, while one at (10, 10) would be
    hit by accident.
    """
    return f"""<!doctype html><html><body style="margin:0;overflow:hidden">
<button id="{button_id}" onclick="{_MARK_CLICKED}"
  style="position:absolute;left:240px;top:140px;width:44px;height:22px;margin:0;padding:0;font-size:9px"
>T</button>
</body></html>"""


async def test_a_frame_under_a_css_transform_is_clicked_where_it_is_drawn(site: Site) -> None:
    """Same-origin and cross-origin frames under one transform, plus a control.

    The cross-origin frame is the case the composition has to get right by
    itself: its contents come back in its own renderer's coordinates, and
    only its owner's box model says where — and how — they are drawn.
    """
    site.main.routes["/t-same.html"] = _transform_target("same-t-btn")
    site.cross.routes["/t-cross.html"] = _transform_target("cross-t-btn")
    site.cross.routes["/t-plain.html"] = _transform_target("plain-btn")
    site.main.routes["/transform.html"] = f"""<!doctype html><body style="margin:0">
<div style="height:40px">top</div>
<div style="transform:{_FRAME_TRANSFORM};transform-origin:0 0;width:900px">
  <iframe id="same-t" src="/t-same.html"
    style="border:5px solid red;padding:7px;width:320px;height:180px"></iframe>
  <iframe id="cross-t" src="{site.cross_origin}/t-cross.html"
    style="border:4px solid blue;padding:6px;width:320px;height:180px"></iframe>
</div>
<div style="height:40px">mid</div>
<iframe id="cross-p" src="{site.cross_origin}/t-plain.html"
  style="border:4px solid blue;padding:6px;width:320px;height:180px"></iframe>
<div style="height:600px">tail</div>
</body>"""

    async def body() -> None:
        async with browser(f"{site.main_origin}/transform.html") as cap:
            await asyncio.sleep(0.5)
            state = await observe(cap)
            targets = ("cross-t-btn", "same-t-btn", "plain-btn")
            for element_id in targets:
                assert indexed(state, element_id), (element_id, state.serialized_dom)

            for element_id in targets:
                await act(cap, "browser.click", index=element(state, element_id)[0])
                state = await observe(cap)
                # The button's own handler wrote this, so a press that landed
                # anywhere else — on the page behind the frame, or on the
                # frame's blank area — leaves it unset.
                assert element(state, element_id)[1].attributes.get("data-clicked") == "1", (
                    element_id,
                    state.serialized_dom,
                )

    await asyncio.wait_for(body(), _TIMEOUT_S)


async def test_a_busy_frame_costs_the_deadline_not_the_observation(site: Site) -> None:
    site.cross.routes["/busy.html"] = (
        "<!doctype html><body><button id='busy'>busy</button>"
        "<script>window.spin = (ms) => { const end = Date.now() + ms; while (Date.now() < end) {} }"
        "</script></body>"
    )
    site.main.routes["/busy-host.html"] = f"""<!doctype html><body style="margin:0">
<button id="page-btn">page</button>
<iframe id="busy-frame" src="{site.cross_origin}/busy.html" style="width:300px;height:120px"></iframe>
</body>"""

    async def body() -> None:
        async with browser(f"{site.main_origin}/busy-host.html") as cap:
            await asyncio.sleep(0.5)
            dom_watchdog(cap)._frame_capture_timeout_s = 1.0
            state = await observe(cap)
            frame_session = element(state, "busy")[1].session_id

            await evaluate(cap, frame_session, "setTimeout(() => spin(2500), 50); 1")
            await asyncio.sleep(0.5)
            started = time.monotonic()
            state = await observe(cap)
            elapsed = time.monotonic() - started

            assert elapsed < 2.2, elapsed
            assert indexed(state, "page-btn") and indexed(state, "busy-frame")
            assert not indexed(state, "busy")

            await asyncio.sleep(1.6)
            assert indexed(await observe(cap), "busy")

    await asyncio.wait_for(body(), _TIMEOUT_S)


async def test_a_click_into_a_frame_that_navigated_is_refused(site: Site) -> None:
    site.cross.routes["/one.html"] = (
        f"<!doctype html><body style='margin:0'><button id='one' onclick=\"{_MARK_CLICKED}\">one</button></body>"
    )
    site.cross.routes["/two.html"] = (
        "<!doctype html><body style='margin:0'><div style='height:5px'></div>"
        f"<button id='two' onclick=\"{_MARK_CLICKED}\">two</button></body>"
    )
    site.main.routes["/nav-host.html"] = f"""<!doctype html><body style="margin:0">
<iframe id="nav" src="{site.cross_origin}/one.html" style="width:300px;height:60px"></iframe>
</body>"""

    async def body() -> None:
        async with browser(f"{site.main_origin}/nav-host.html") as cap:
            await asyncio.sleep(0.5)
            state = await observe(cap)
            stale_key, _ = element(state, "one")

            await evaluate(
                cap, None, f"document.getElementById('nav').src = '{site.cross_origin}/two.html'; 1"
            )
            await asyncio.sleep(1.0)
            result = await run(cap, "browser.click", index=stale_key)

            assert not result.success
            assert "observe the page again" in (result.error or "")
            state = await observe(cap)
            assert element(state, "two")[1].attributes.get("data-clicked") is None

    await asyncio.wait_for(body(), _TIMEOUT_S)
