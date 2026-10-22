"""Tabs the focus left, in a real Chrome: frozen (``background_tabs: freeze``) or throttled (§4.14).

iqiyi.com keeps its tabs in step through ``localStorage``: a ``storage`` event
makes a tab write its own copy back, and with five or more of its tabs open
the exchange never stopped (every renderer pegged, the observation's
screenshot got no reply). Freezing the tab the focus leaves stops it: a frozen
tab runs no event handler, so the focused tab's writes get no answer.

Pages from a loopback server (no network, no model):

- ``/sync.html?tab=<name>`` joins a two-tab ``localStorage`` exchange with a
  tie-break that never settles — whichever tab hears the other's copy claims
  a newer one — so two running tabs rewrite each other without end
  (measured, Chrome 155 headless: about 330 writes a second each). It counts
  its writes, its timer wake-ups and its ``freeze`` / ``resume`` events, and
  sends a beacon (``/beacon``) every 100 ms while it runs, so whether it runs
  is known without asking the page.

The fake-browser half (the order of the commands, the archivers, teardown) is
``test_browser_background_tabs.py``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from spectragent.core.capabilities.browser.archive import FramesArchiver, StorageArchiver
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.schemas.action import Action

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 90
#: How long each count is watched.
WATCH_S = 2.0
#: Writes a second above which the exchange is running away. Two running tabs
#: make about 330 each (measured, Chrome 155 headless, also with Chrome's
#: background throttling on); a frozen partner leaves the focused tab at 0.
RUNAWAY_WRITES_PER_S = 50
#: The slowest round trip a responsive focused tab may take for a trivial
#: ``Runtime.evaluate``; measured below 10 ms, whatever the other tab does.
RESPONSIVE_MS = 500
#: Beacons a frozen tab may still send in a watch: one already queued when the
#: freeze took effect.
FROZEN_BEACONS = 1
#: Beacons a running background tab sends at least in a watch: one every
#: 100 ms, aligned to Chrome's 100 ms wake-ups of a hidden tab.
RUNNING_BEACONS = 5

_SYNC_PAGE = b"""<!doctype html><html><head><meta charset=utf-8><link rel=icon href='data:,'></head>
<body><script>
(function () {
  const TAB = new URLSearchParams(location.search).get('tab');
  const KEY = 'af-endless-sync';
  window.__writes = 0;
  window.__ticks = 0;
  window.__life = [];
  setInterval(function () { window.__ticks += 1; }, 10);
  setInterval(function () { navigator.sendBeacon('/beacon?tab=' + TAB); }, 100);
  document.addEventListener('freeze', function () { window.__life.push('freeze'); });
  document.addEventListener('resume', function () { window.__life.push('resume'); });
  function publish(t) {
    window.__writes += 1;
    localStorage.setItem(KEY, JSON.stringify({ owner: TAB, t: t }));
  }
  window.addEventListener('storage', function (event) {
    if (event.key !== KEY || !event.newValue) { return; }
    publish(JSON.parse(event.newValue).t + 1);  // never settles: always claim a newer copy
  });
  publish(Date.now());
})();
</script></body></html>"""


class _Pages:
    """The loopback server; ``url(tab)`` names one sync page, ``beacons(tab, since)`` counts."""

    def __enter__(self) -> _Pages:
        beacons: list[tuple[float, str]] = []
        self._beacons = beacons

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return None

            def _answer(self) -> None:
                url = urlsplit(self.path)
                if url.path == "/beacon":
                    beacons.append((time.monotonic(), parse_qs(url.query)["tab"][0]))
                    body, status = b"", 204
                elif url.path == "/sync.html":
                    body, status = _SYNC_PAGE, 200
                else:
                    body, status = b"", 404
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            do_GET = _answer
            do_POST = _answer

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def url(self, tab: str) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/sync.html?tab={tab}"

    def beacons(self, tab: str, since: float) -> int:
        return sum(1 for at, name in list(self._beacons) if at >= since and name == tab)

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(scope="module")
def pages() -> Iterator[_Pages]:
    with _Pages() as served:
        yield served


@asynccontextmanager
async def _two_tabs(
    pages: _Pages,
    *,
    background_tabs: str = "freeze",
    background_throttling: bool = False,
    archived: bool = False,
) -> AsyncIterator[tuple[BrowserCapability, str, str]]:
    """Tab A at the start, tab B opened and switched to by the agent: ``(cap, A, B)`` target ids."""
    cap = BrowserCapability(settle_quiet_ms=100, background_tabs=background_tabs)  # type: ignore[arg-type]
    cap.backend = LocalChromeProvider(  # type: ignore[assignment]
        headless=True, background_throttling=background_throttling
    )
    if archived:
        cap.bind_archivers([StorageArchiver(), FramesArchiver(after_observation_delay_s=None)])
    try:
        await asyncio.wait_for(cap.setup(start_url=pages.url("A")), 60)
        first = cap._session_to_target[cap.default_session_id]
        second = cap._session_to_target[await cap.create_tab(pages.url("B"))]
        await asyncio.sleep(0.5)
        result = await cap.execute_action(
            Action(name="browser.switch_tab", params={"target_id": second})
        )
        assert result.success, result.error
        yield cap, first, second
    finally:
        await cap.teardown()


async def _evaluate(cap: BrowserCapability, target: str, expression: str) -> Any:
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=cap._target_to_session[target],
        timeout=10,
    )
    return reply["result"]["value"]


async def _writes_per_s(cap: BrowserCapability, target: str) -> float:
    before = await _evaluate(cap, target, "window.__writes")
    await asyncio.sleep(WATCH_S)
    return (await _evaluate(cap, target, "window.__writes") - before) / WATCH_S


async def _round_trip_ms(cap: BrowserCapability, target: str) -> float:
    started = time.monotonic()
    await _evaluate(cap, target, "1 + 1")
    return (time.monotonic() - started) * 1000


async def test_two_running_tabs_rewrite_each_other_without_end(pages: _Pages) -> None:
    """The control: ``run`` leaves both tabs running, and the exchange never stops."""

    async def run() -> None:
        async with _two_tabs(pages, background_tabs="run") as (cap, _first, second):
            rate = await _writes_per_s(cap, second)
            assert rate > RUNAWAY_WRITES_PER_S, f"{rate:.0f} writes/s"

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_throttling_alone_does_not_stop_it(pages: _Pages) -> None:
    """Chrome's background throttling slows timers, not ``storage`` events."""

    async def run() -> None:
        async with _two_tabs(pages, background_tabs="run", background_throttling=True) as (
            cap,
            first,
            second,
        ):
            rate = await _writes_per_s(cap, second)
            assert rate > RUNAWAY_WRITES_PER_S, f"{rate:.0f} writes/s"
            # Throttled all the same: the left tab's 10 ms timer wakes about once a second.
            ticks = await _evaluate(cap, first, "window.__ticks")
            await asyncio.sleep(WATCH_S)
            assert await _evaluate(cap, first, "window.__ticks") - ticks <= 3 * WATCH_S

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_freezing_the_left_tab_stops_it_and_the_focused_tab_answers(
    pages: _Pages,
) -> None:
    async def run() -> None:
        async with _two_tabs(pages) as (cap, first, second):
            assert cap._frozen == {first}
            since = time.monotonic()
            assert await _writes_per_s(cap, second) == 0
            assert pages.beacons("A", since) <= FROZEN_BEACONS, "the frozen tab ran"
            assert await _round_trip_ms(cap, second) < RESPONSIVE_MS
            assert await _evaluate(cap, first, "window.__life") == ["freeze"]

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_switching_back_runs_the_tab_again_and_freezes_the_other(pages: _Pages) -> None:
    async def run() -> None:
        async with _two_tabs(pages) as (cap, first, second):
            result = await cap.execute_action(
                Action(name="browser.switch_tab", params={"target_id": first})
            )
            assert result.success, result.error
            assert cap._frozen == {second}
            assert await _evaluate(cap, first, "document.visibilityState") == "visible"
            since = time.monotonic()
            assert await _writes_per_s(cap, first) == 0, "its partner is frozen now"
            assert pages.beacons("A", since) >= RUNNING_BEACONS
            assert pages.beacons("B", since) <= FROZEN_BEACONS
            assert await _evaluate(cap, first, "window.__life") == ["freeze", "resume"]

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_a_collection_reads_the_frozen_tab_without_waking_it(pages: _Pages) -> None:
    """``browser.storage`` reads every tab and frame, ``browser.frames`` shoots the focused tab."""

    async def run() -> None:
        async with _two_tabs(pages, archived=True) as (cap, first, second):
            await cap.execute_action(Action(name="browser.wait", params={"seconds": 0}))
            since = time.monotonic()
            batch = await cap.collect_archive()
            await asyncio.sleep(0.5)
            assert pages.beacons("A", since) <= FROZEN_BEACONS, "the collection woke it"
            assert cap._frozen == {first}
            # Every read of the frozen tab answered within its 1 s bound: none is unavailable.
            assert not batch.gaps
            storage = next(f for f in batch.files if f.name == "storage.json")
            assert b"af-endless-sync" in storage.data
            assert b'"unavailable":[]' in storage.data

    await asyncio.wait_for(run(), _TIMEOUT_S)
