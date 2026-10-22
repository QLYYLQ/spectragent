"""One clock for every tab of a pinned run, in a real Chrome (§4.14 ``replayability``).

The pinned shim used to give every document its own clock — T0 at the
document's start, then 50 ms every 25 ``Date`` constructions — so two tabs of
one run read different times: a tab opened minutes into the run read T0 again,
and a page that called ``Date`` often ran hours ahead of one that did not. On
iqiyi.com that killed recordings: its tabs keep their search history and SDK
cache in step through ``localStorage`` and decide by timestamp whose copy is
newer, the exchange never converged, every iqiyi renderer spent itself on
``storage`` events, and the next observation's ``Page.captureScreenshot`` got no
reply (``shims.py`` module docstring). The shim now reads one clock for the
whole run: T0 at the run's start, then real time.

Pages from a loopback server (no network, no model):

- ``/sync.html?tab=<name>&burn=<n>`` calls ``Date.now()`` *n* times as it
  loads, then joins a two-tab ``localStorage`` sync that keeps the newest copy
  and refuses a copy stamped more than :data:`SKEW_TOLERANCE_MS` in its own
  future (a common defence against clock skew). With one clock it settles
  after each tab's first write; with a clock per tab the tab that is behind
  keeps refusing the other's "future" copy and the two rewrite each other
  without end. ``window.__writes`` counts a tab's writes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.replay.pinned import PinnedReplayability
from spectragent.core.capabilities.browser.replay.shims import render_shim

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 90
#: ``Date.now()`` calls the first tab makes as it loads. Under the per-document
#: counter they put its clock 400 s ahead of a fresh tab's (2 ms per call);
#: under one clock they change nothing. Measured in Chrome 155: a few ms of work.
BURN_CALLS = 200_000
#: How far in its own future the sync page accepts another tab's stamp.
SKEW_TOLERANCE_MS = 1_000
#: How far from the host's clock a tab of an unshifted pinned run may read:
#: the page's high-resolution clock and the host's wall clock are the same
#: clock read through different paths, plus the evaluation round trip.
HOST_CLOCK_SLACK_MS = 1_000
#: How long the sync gets to settle, or to show it never will.
SYNC_WATCH_S = 2.0
#: The writes a settled sync makes: each tab's first.
SETTLED_WRITES = 2

_SYNC_PAGE = """<!doctype html><html><head><meta charset=utf-8><link rel=icon href='data:,'></head>
<body><script>
(function () {
  const params = new URLSearchParams(location.search);
  const TAB = params.get('tab');
  const BURN = Number(params.get('burn') || 0);
  const KEY = 'af-clock-sync';
  const TOLERANCE = __TOLERANCE__;
  for (let i = 0; i < BURN; i++) { Date.now(); }
  window.__writes = 0;
  let mine = null;
  function publish() {
    mine = { owner: TAB, t: Date.now() };
    window.__writes += 1;
    localStorage.setItem(KEY, JSON.stringify(mine));
  }
  window.addEventListener('storage', function (event) {
    if (event.key !== KEY || !event.newValue) { return; }
    const theirs = JSON.parse(event.newValue);
    if (theirs.t > Date.now() + TOLERANCE) { publish(); return; }  // stamped in my future
    if (theirs.t < mine.t) { publish(); return; }                    // older than mine
    mine = theirs;                                                    // newer: adopt it
  });
  publish();
  window.__owner = function () { return mine.owner; };
})();
</script></body></html>""".replace("__TOLERANCE__", str(SKEW_TOLERANCE_MS)).encode()


class _Pages:
    """The loopback server; ``url(tab, burn)`` names one sync page."""

    def __enter__(self) -> _Pages:
        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return None

            def do_GET(self) -> None:
                body = _SYNC_PAGE if self.path.startswith("/sync.html") else b""
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def url(self, tab: str, burn: int) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/sync.html?tab={tab}&burn={burn}"

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(scope="module")
def pages() -> Iterator[_Pages]:
    with _Pages() as served:
        yield served


@asynccontextmanager
async def _browser(start_url: str, *, pinned: bool = True) -> AsyncIterator[BrowserCapability]:
    cap = BrowserCapability(settle_quiet_ms=100)
    if pinned:
        cap.replayability = PinnedReplayability(random_seed=7)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    try:
        await asyncio.wait_for(cap.setup(start_url=start_url), 60)
        yield cap
    finally:
        await cap.teardown()


async def _evaluate(cap: BrowserCapability, session: str, expression: str) -> Any:
    reply = await cap.cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=session,
        timeout=10,
    )
    return reply["result"]["value"]


async def test_every_tab_reads_the_same_clock(pages: _Pages) -> None:
    """A tab that called ``Date`` 200,000 times and a tab opened a second later read one time.

    ``base_time: run_start`` does not shift the clock, so both read the host's
    time. Under the per-document counter the first read T0 + 400 s and the
    second T0, both minutes away from the host's clock and from each other.
    """

    async def run() -> None:
        async with _browser(pages.url("A", BURN_CALLS)) as cap:
            first = cap.default_session_id
            await asyncio.sleep(1.0)
            second = await cap.create_tab(pages.url("B", 0))
            await asyncio.sleep(0.5)
            for session in (first, second):
                before = time.time() * 1000
                read = await _evaluate(cap, session, "Date.now()")
                after = time.time() * 1000
                assert before - HOST_CLOCK_SLACK_MS <= read <= after + HOST_CLOCK_SLACK_MS, (
                    f"tab reads {read - after:+.0f} ms from the host's clock"
                )

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_tabs_keep_their_storage_sync_settled(pages: _Pages) -> None:
    """Two tabs syncing through ``localStorage`` by timestamp settle after their first writes.

    The regression for the iqiyi.com hang: under a clock per tab the first tab
    (400 s ahead) refused the second's copy as older and rewrote its own, the
    second refused that as stamped in its future and rewrote, and so on
    without end (measured against the old shim: 18 writes per tab in the 2 s
    watched, and the first tab read 396 s ahead of the host's clock).
    """

    async def run() -> None:
        async with _browser(pages.url("A", BURN_CALLS)) as cap:
            first = cap.default_session_id
            await asyncio.sleep(1.0)
            second = await cap.create_tab(pages.url("B", 0))
            await asyncio.sleep(SYNC_WATCH_S)
            writes = [await _evaluate(cap, s, "window.__writes") for s in (first, second)]
            assert sum(writes) == SETTLED_WRITES, f"writes per tab: {writes}"
            # The newer copy won in both tabs: the second tab's.
            assert [await _evaluate(cap, s, "window.__owner()") for s in (first, second)] == [
                "B",
                "B",
            ]

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_a_shifted_clock_reads_t0_at_the_runs_start_then_real_time(pages: _Pages) -> None:
    """A replay's clock: the recording's T0 at the replay's start, then real time, in a real V8.

    Evaluated by hand in an unpinned tab (no shim installed there yet), with
    T0 an hour before the start; ``Date()`` as a function and ``new Date()``
    read the same clock, and a date built from arguments is native.
    """
    hour_ms = 3_600_000

    async def run() -> None:
        async with _browser(pages.url("A", 0), pinned=False) as cap:
            started = int(time.time() * 1000)
            shim = render_shim(t0_ms=started - hour_ms, run_started_ms=started, seed=1)
            elapsed_before = time.time() * 1000 - started
            reads = await _evaluate(
                cap,
                cap.default_session_id,
                shim + ";[Date.now(), new Date().getTime(), Date.parse(Date()),"
                " new Date(2020, 0, 1).getFullYear()]",
            )
            elapsed_after = time.time() * 1000 - started
            now, constructed, as_string, built = reads
            low = started - hour_ms + elapsed_before - HOST_CLOCK_SLACK_MS
            high = started - hour_ms + elapsed_after + HOST_CLOCK_SLACK_MS
            for value in (now, constructed):
                assert low <= value <= high, value - (started - hour_ms)
            # Date() as a function: the same clock, at second resolution.
            assert low - 1000 <= as_string <= high
            assert built == 2020

    await asyncio.wait_for(run(), _TIMEOUT_S)
