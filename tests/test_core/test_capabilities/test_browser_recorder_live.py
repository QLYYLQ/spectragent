"""The recorder connection against a real headless Chrome — the measured facts as regressions.

Everything ``docs/architecture_design.md`` §4.14 says about two CDP clients
on one browser was measured (Chrome 153), and every one of those
facts can change with a Chrome release. These tests pin them as canaries,
against the local fixture site only, with short timeouts:

- the recorder's Response-stage Fetch sees every request the fixture
  served — documents, redirects, POSTs, the out-of-process iframe (site
  isolation **on**), the dedicated worker's fetch, the service worker's
  script and fetches, and a ``window.open`` popup before the control client
  ever adopts it — except the nested worker's script, a documented gap;
- explicit cleanup releases the browser; an unclean drop wedges every new
  target (the reason teardown cleans up first);
- ``PausedTarget`` built-in and callback modes resume only the selected
  flattened session through AF's client; sibling workers remain paused;
- ``replayability: pinned`` makes randomness, the clock, time zone, locale
  and user agent identical across runs, in the page **and** in a worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from agent_cdp import PausedTarget

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.backends.local_chrome import LocalChromeProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.recorder import (
    AUTO_ATTACH_PARAMS,
    PausedRequest,
    RecorderConnection,
    RecorderParticipantBase,
    RecorderTarget,
)
from spectragent.core.capabilities.browser.replay import PinnedReplayability
from spectragent.schemas.action import Action
from tests._helpers.fixture_site import FixtureSite

pytestmark = pytest.mark.requires_chrome

_TIMEOUT_S = 120


class _Collector(RecorderParticipantBase):
    """Asks for every response and remembers what paused where."""

    def __init__(self) -> None:
        self.paused: list[tuple[str, str | None]] = []

    def fetch_patterns(self, target: RecorderTarget) -> tuple[dict[str, Any], ...]:
        return ({"urlPattern": "*", "requestStage": "Response"},)

    async def on_paused(self, paused: PausedRequest) -> None:
        self.paused.append((paused.url, paused.target.type if paused.target else None))

    def paths(self) -> set[str]:
        out: set[str] = set()
        for url, _type in self.paused:
            path = "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
            out.add(path.split("?", 1)[0])
        return out


@asynccontextmanager
async def _chrome(**provider: Any) -> AsyncIterator[str]:
    backend = LocalChromeProvider(headless=True, **provider)
    session = await backend.open()
    try:
        assert session.cdp_url is not None
        yield session.cdp_url
    finally:
        await backend.close(session)


async def _client(url: str, **options: Any) -> CDPClient:
    client = CDPClient(url, proxy=None, **options)
    await client.start()
    return client


async def _recorder(url: str, participants: list[Any]) -> RecorderConnection:
    client = await _client(url, max_frame_size=None, compression=None)
    recorder = RecorderConnection(
        client, participants=lambda: participants, channel=None, on_lost=lambda _e: None
    )
    await recorder.open()
    return recorder


class _Tab:
    """A control-client tab, shaped like ``BrowserCapability._attach_tab``."""

    def __init__(self, client: CDPClient) -> None:
        self.client = client
        self.loads = 0
        client.on_event("Page.loadEventFired", self._on_load)
        self.sid = ""

    def _on_load(self, _params: dict[str, Any], session_id: str | None) -> None:
        if session_id == self.sid:
            self.loads += 1

    async def open(self) -> _Tab:
        created = await self.client.send("Target.createTarget", {"url": "about:blank"}, timeout=5)
        attached = await self.client.send(
            "Target.attachToTarget", {"targetId": created["targetId"], "flatten": True}, timeout=5
        )
        self.sid = attached["sessionId"]
        await self.client.send("Page.enable", {}, self.sid, timeout=5)
        return self

    async def navigate(self, url: str) -> None:
        # A popup takes the foreground; Chrome defers media loading in a
        # background tab, so the tab is brought back before every page.
        await self.client.send("Page.bringToFront", {}, self.sid, timeout=5)
        await self.client.send("Page.navigate", {"url": url}, self.sid, timeout=5)

    async def evaluate(self, expression: str, *, timeout: float = 5) -> Any:
        reply = await self.client.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            self.sid,
            timeout=timeout,
        )
        return (reply.get("result") or {}).get("value")


async def _loads_within(tab: _Tab, url: str, seconds: float) -> bool:
    before = tab.loads
    try:
        await asyncio.wait_for(tab.navigate(url), seconds)
    except TimeoutError:
        return False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if tab.loads > before:
            return True
        await asyncio.sleep(0.05)
    return False


async def _served(site: FixtureSite, paths: list[str], seconds: float) -> set[str]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        seen = {r.bare_path for r in site.requests}
        if set(paths) <= seen:
            return seen
        await asyncio.sleep(0.05)
    return {r.bare_path for r in site.requests}


# ── coverage, cleanup, the wedge, resume modes ────────────────────────


COVERED_SCENARIOS = (
    "redirect",
    "post_form",
    "json_post",
    "graphql",
    "oopif",
    "worker",
    "service_worker",
    "popup",
    "beacon",
    "range_video",
)


async def test_recorder_captures_every_fixture_request() -> None:
    async def _run() -> None:
        collector = _Collector()
        with FixtureSite() as site:
            async with _chrome() as url:
                recorder = await _recorder(url, [collector])
                control = await _client(url)
                try:
                    tab = await _Tab(control).open()
                    for scenario in (*COVERED_SCENARIOS, "nested_worker"):
                        await tab.navigate(site.url(scenario))
                        await _served(site, site.expected(scenario), 8)
                        await asyncio.sleep(0.3)
                    # Every request a covered scenario made was seen by the
                    # recorder: the fixture's access log is the oracle.
                    served = {r.bare_path for r in site.requests if r.scenario in COVERED_SCENARIOS}
                    assert served >= {p for s in COVERED_SCENARIOS for p in site.expected(s)}, (
                        "a scenario did not run to completion"
                    )
                    missing = sorted(served - collector.paths())
                    assert missing == [], f"served but not captured: {missing}"
                    # Site isolation is on: the cross-site frame was its own target.
                    assert any(t == "iframe" for _u, t in collector.paused)
                    assert any(t == "service_worker" for _u, t in collector.paused)
                    # The known gap: a nested dedicated worker's script never
                    # reaches Fetch at either stage (measured, arch §4.14) — its fetch does.
                    nested = {r.bare_path for r in site.requests_for("nested_worker")}
                    assert "/nested_worker/inner.js" in nested
                    assert "/nested_worker/inner.js" not in collector.paths()
                    assert "/nested_worker/data.json" in collector.paths()
                finally:
                    await recorder.close()
                    await control.stop()

    await asyncio.wait_for(_run(), _TIMEOUT_S)


async def test_cleanup_releases_new_targets() -> None:
    async def _run() -> None:
        with FixtureSite() as site:
            async with _chrome() as url:
                recorder = await _recorder(url, [_Collector()])
                control = await _client(url)
                try:
                    tab = await _Tab(control).open()
                    assert await _loads_within(tab, site.url("beacon"), 5)
                    await recorder.close()
                    fresh = await _Tab(control).open()
                    assert await _loads_within(fresh, site.url("worker"), 5)
                finally:
                    await recorder.close()
                    await control.stop()

    await asyncio.wait_for(_run(), _TIMEOUT_S)


async def test_unclean_drop_wedges_new_targets() -> None:
    """Chrome canary: a recorder that stops answering wedges the browser.

    A client holding browser-level ``waitForDebuggerOnStart`` whose socket
    stays open but which no longer services it (a hung recorder) leaves every
    *new* target in the browser paused — for every client. This is the
    failure first recorded as "an unclean disconnect": its client's ``stop()``
    never closed the socket (fixed in M29), so the browser saw a silent
    client, not a gone one. A socket that really closes is released (next
    test). Hence §4.14's rules: liveness is pushed, a local receive error
    closes the socket, and teardown cleans up before it disconnects.
    """

    async def _run() -> None:
        with FixtureSite() as site:
            async with _chrome() as url:
                recorder = await _recorder(url, [_Collector()])
                client = recorder._client  # type: ignore[attr-defined]
                # Stop servicing the socket, leave it open.
                client._recv_task.cancel()  # type: ignore[attr-defined]
                await asyncio.sleep(0.3)
                control = await _client(url)
                try:
                    tab = _Tab(control)
                    try:
                        await asyncio.wait_for(tab.open(), 3)
                        wedged = not await _loads_within(tab, site.url("beacon"), 3)
                    except TimeoutError:
                        wedged = True
                    assert wedged, "a silent recorder no longer wedges new targets"
                finally:
                    await control.stop()
                    await client.stop()

    await asyncio.wait_for(_run(), _TIMEOUT_S)


async def test_a_closed_socket_releases_the_browser_without_cleanup() -> None:
    """Chrome canary: a recorder whose socket closes is detached, not wedging."""

    async def _run() -> None:
        with FixtureSite() as site:
            async with _chrome() as url:
                recorder = await _recorder(url, [_Collector()])
                await recorder._client.stop()  # type: ignore[attr-defined]
                await asyncio.sleep(0.3)
                control = await _client(url)
                try:
                    tab = await asyncio.wait_for(_Tab(control).open(), 5)
                    assert await _loads_within(tab, site.url("beacon"), 5)
                finally:
                    await control.stop()

    await asyncio.wait_for(_run(), _TIMEOUT_S)


@pytest.mark.parametrize("resume_mode", ["builtin", "callback"])
async def test_paused_target_resume_unblocks_only_its_session(resume_mode: str) -> None:
    """The acknowledged envelope route resumes this worker, never its paused sibling."""

    async def run() -> None:
        with FixtureSite() as site:
            async with _chrome() as url:
                client = await _client(url, max_frame_size=None, compression=None)
                attached: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
                worker_sessions: dict[str, str] = {}
                ran: set[str] = set()
                tab = await _Tab(client).open()

                def on_attached(params: dict[str, Any], parent_sid: str | None) -> None:
                    if params.get("targetInfo", {}).get("type") == "worker":
                        assert parent_sid == tab.sid
                        attached.put_nowait(params)

                def on_console(params: dict[str, Any], session_id: str | None) -> None:
                    for argument in params.get("args", []):
                        value = argument.get("value")
                        if isinstance(value, str) and value.startswith("fixture-worker:"):
                            label = value.split(":", 1)[1]
                            assert worker_sessions[label] == session_id
                            ran.add(label)

                client.on_event("Target.attachedToTarget", on_attached)
                client.on_event("Runtime.consoleAPICalled", on_console)
                try:
                    assert await _loads_within(tab, site.url("beacon"), 5)
                    await client.send(
                        "Target.setAutoAttach", dict(AUTO_ATTACH_PARAMS), tab.sid, timeout=5
                    )
                    for label in ("first", "second"):
                        await tab.evaluate(
                            "window.fixtureWorkers ??= [];"
                            "window.fixtureWorkers.push(new Worker(URL.createObjectURL(new Blob(["
                            + json.dumps(
                                f"self.fixtureLabel={label!r};console.log('fixture-worker:{label}')"
                            )
                            + "], {type: 'text/javascript'}))))"
                        )
                        attachment = await asyncio.wait_for(attached.get(), 5)
                        assert attachment["waitingForDebugger"] is True
                        worker_sessions[label] = attachment["sessionId"]
                        await client.send(
                            "Runtime.enable", session_id=attachment["sessionId"], timeout=5
                        )
                    assert worker_sessions["first"] != worker_sessions["second"]

                    # The previous implementation put sessionId in params. This
                    # addresses the browser and leaves both worker targets paused.
                    with contextlib.suppress(RuntimeError):
                        await client.send(
                            "Runtime.runIfWaitingForDebugger",
                            {"sessionId": worker_sessions["first"]},
                            timeout=2,
                        )
                    await asyncio.sleep(0.1)
                    assert ran == set()

                    async def resume_first() -> None:
                        await client.send(
                            "Runtime.runIfWaitingForDebugger",
                            session_id=worker_sessions["first"],
                            timeout=2,
                        )

                    target = (
                        PausedTarget(cdp=client, session_id=worker_sessions["first"])
                        if resume_mode == "builtin"
                        else PausedTarget(resume=resume_first)
                    )
                    async with target:
                        assert not target.resumed and ran == set()
                    assert target.resumed
                    async with asyncio.timeout(5):
                        while "first" not in ran:
                            await asyncio.sleep(0.01)
                    await asyncio.sleep(0.1)
                    assert ran == {"first"}, "the sibling worker must still be paused"
                    async with PausedTarget(
                        cdp=client, session_id=worker_sessions["second"]
                    ) as second:
                        assert not second.resumed
                    assert second.resumed
                    async with asyncio.timeout(5):
                        while ran != {"first", "second"}:
                            await asyncio.sleep(0.01)
                    for label, session_id in worker_sessions.items():
                        reply = await client.send(
                            "Runtime.evaluate",
                            {"expression": "self.fixtureLabel", "returnByValue": True},
                            session_id=session_id,
                            timeout=2,
                        )
                        assert reply["result"]["value"] == label
                finally:
                    for session_id in worker_sessions.values():
                        with contextlib.suppress(Exception):
                            await client.send(
                                "Runtime.runIfWaitingForDebugger", session_id=session_id, timeout=1
                            )
                    with contextlib.suppress(Exception):
                        await client.send(
                            "Target.setAutoAttach",
                            {"autoAttach": False, "waitForDebuggerOnStart": False, "flatten": True},
                            tab.sid,
                            timeout=2,
                        )
                    await client.stop()

    await asyncio.wait_for(run(), _TIMEOUT_S)


async def test_no_target_left_waiting() -> None:
    async def _run() -> None:
        with FixtureSite() as site:
            async with _chrome() as url:
                recorder = await _recorder(url, [_Collector()])
                control = await _client(url)
                try:
                    # Every existing target (Chrome's own included) was handled.
                    assert recorder._waiting == set()
                    tab = await _Tab(control).open()
                    assert await _loads_within(tab, site.url("popup"), 5)
                    await _served(site, site.expected("popup"), 5)
                    await asyncio.sleep(0.3)
                    assert recorder._waiting == set()
                    assert recorder.failures == {} or all(
                        "Target.setAutoAttach" in f for fs in recorder.failures.values() for f in fs
                    ), recorder.failures
                finally:
                    await recorder.close()
                    await control.stop()

    await asyncio.wait_for(_run(), _TIMEOUT_S)


# ── replayability: pinned, page and worker ────────────────────────────

_PROBE = r"""
(function () {
  const out = {};
  out.random = [Math.random(), Math.random(), Math.random()];
  const bytes = new Uint8Array(8); crypto.getRandomValues(bytes); out.bytes = Array.from(bytes);
  out.uuids = [crypto.randomUUID(), crypto.randomUUID()];
  out.dates = [Date.now(), new Date().getTime(), Date.now()];
  const start = Date.now(); let spins = 0;
  while (Date.now() < start + 200) { spins++; }
  out.spins = spins;
  out.tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  out.locale = Intl.DateTimeFormat().resolvedOptions().locale;
  out.language = navigator.language;
  out.ua = navigator.userAgent;
  out.brands = navigator.userAgentData ? navigator.userAgentData.brands.map(b => b.brand) : null;
  return out;
})()
"""

_PAGE = (
    "<!doctype html><html><head><meta charset=utf-8><link rel=icon href='data:,'></head><body>"
    "<script>window.__page = " + _PROBE + ";"
    "window.__worker = new Promise(r => { const w = new Worker('/worker.js');"
    " w.onmessage = e => r(e.data); });"
    "window.__sw = navigator.serviceWorker ? navigator.serviceWorker.register('/sw.js')"
    ".then(() => 'registered', e => 'rejected:' + e.name) : Promise.resolve('none');"
    "</script></body></html>"
).encode()
_WORKER = ("postMessage(" + _PROBE + ");").encode()


class _ProbeServer:
    def __enter__(self) -> str:
        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                return None

            def do_GET(self) -> None:
                body, ctype = {
                    "/page.html": (_PAGE, "text/html; charset=utf-8"),
                    "/worker.js": (_WORKER, "application/javascript"),
                    "/sw.js": (b"", "application/javascript"),
                }.get(self.path, (b"", "text/plain"))
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        # Exact localhost: a secure context, so crypto.randomUUID and service
        # workers exist and the shim's answers to them can be checked.
        return f"http://localhost:{self._server.server_address[1]}/page.html"

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


_UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


async def _pinned_run(page_url: str, **pinned: Any) -> dict[str, Any]:
    cap = BrowserCapability()
    cap.replayability = PinnedReplayability(**pinned)
    cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
    await cap.setup(start_url=page_url)
    try:
        expression = (
            "Promise.all([window.__page, window.__worker, window.__sw])"
            ".then(([page, worker, sw]) => JSON.stringify({page, worker, sw}))"
        )
        reply = await cap.cdp.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=cap.default_session_id,
            timeout=10,
        )
        result = json.loads(reply["result"]["value"])
        assert cap._replay_plan is not None
        result["t0_ms"] = cap._replay_plan.t0_ms
        return result
    finally:
        await cap.teardown()


class TestPinnedShims:
    @pytest.fixture(scope="class")
    def runs(self) -> dict[str, dict[str, Any]]:
        async def _all() -> dict[str, dict[str, Any]]:
            with _ProbeServer() as page_url:
                knobs = {"timezone": "Asia/Tokyo", "locale": "fr-FR"}
                first = await _pinned_run(page_url, random_seed=7, **knobs)
                again = await _pinned_run(page_url, random_seed=7, **knobs)
                other = await _pinned_run(page_url, random_seed=8, **knobs)
                return {"first": first, "again": again, "other": other}

        return asyncio.run(asyncio.wait_for(_all(), _TIMEOUT_S))

    @pytest.mark.parametrize("where", ["page", "worker"])
    def test_same_seed_same_sequences(self, runs: dict[str, Any], where: str) -> None:
        first, again = runs["first"][where], runs["again"][where]
        for key in ("random", "bytes", "uuids"):
            assert first[key] == again[key], (where, key)
        # T0 is each run's own start (base_time: run_start); the clock's
        # steps from it are identical.
        steps = [d - runs["first"]["t0_ms"] for d in first["dates"]]
        assert steps == [d - runs["again"]["t0_ms"] for d in again["dates"]]

    @pytest.mark.parametrize("where", ["page", "worker"])
    def test_another_seed_differs(self, runs: dict[str, Any], where: str) -> None:
        assert runs["first"][where]["random"] != runs["other"][where]["random"]
        assert runs["first"][where]["uuids"] != runs["other"][where]["uuids"]

    @pytest.mark.parametrize("where", ["page", "worker"])
    def test_the_clock_starts_at_t0_and_a_busy_wait_ends(
        self, runs: dict[str, Any], where: str
    ) -> None:
        probe = runs["first"][where]
        assert probe["spins"] > 0  # the 200 ms busy-wait terminated
        # The first constructions read exactly T0 (the clock steps 50 ms per
        # 25 constructions; it never reads the wall clock).
        assert probe["dates"] == [runs["first"]["t0_ms"]] * 3

    @pytest.mark.parametrize("where", ["page", "worker"])
    def test_uuids_are_rfc4122_v4(self, runs: dict[str, Any], where: str) -> None:
        for uuid in runs["first"][where]["uuids"]:
            assert _UUID_V4.match(uuid), uuid

    def test_service_worker_registration_is_rejected(self, runs: dict[str, Any]) -> None:
        assert runs["first"]["sw"] == "rejected:SecurityError"

    @pytest.mark.parametrize("where", ["page", "worker"])
    def test_time_zone_and_locale_are_pinned(self, runs: dict[str, Any], where: str) -> None:
        probe = runs["first"][where]
        assert probe["tz"] == "Asia/Tokyo"
        assert probe["locale"] == "fr-FR"
        assert probe["language"] == "fr-FR"

    def test_user_agent_matches_in_page_and_worker(self, runs: dict[str, Any]) -> None:
        page, worker = runs["first"]["page"], runs["first"]["worker"]
        assert page["ua"] == worker["ua"]
        assert page["brands"] == worker["brands"]
        # The browser's own identifier carries the browser's own client hints (client_hints.py).
        assert page["brands"] and "Chromium" in page["brands"]
        assert runs["first"]["page"]["ua"] == runs["again"]["page"]["ua"]

    def test_a_pinned_user_agent_is_visible_in_page_and_worker(self) -> None:
        async def _run() -> dict[str, Any]:
            with _ProbeServer() as page_url:
                return await _pinned_run(page_url, user_agent="PinnedAgent/1.0 (fixture)")

        result = asyncio.run(asyncio.wait_for(_run(), _TIMEOUT_S))
        assert result["page"]["ua"] == "PinnedAgent/1.0 (fixture)"
        assert result["worker"]["ua"] == "PinnedAgent/1.0 (fixture)"
        # Not in Chrome's own form: the override carries no client hints.
        assert result["page"]["brands"] == result["worker"]["brands"] == []


async def test_pinned_capability_observes_through_the_recorder() -> None:
    """End to end: a pinned BrowserCapability observes and acts as usual."""

    async def _run() -> None:
        with FixtureSite() as site:
            cap = BrowserCapability()
            cap.replayability = PinnedReplayability(random_seed=3)
            cap.backend = LocalChromeProvider(headless=True)  # type: ignore[assignment]
            cap.apply_negotiation({"browser.dom", "browser.screenshot", "browser.browser_meta"})
            await cap.setup(start_url=site.url("oopif"))
            try:
                await cap.begin_observation(1)
                meta = await cap.resolve_fragment("browser.browser_meta")
                assert meta.url.endswith("/oopif/page.html")
                assert meta.loader_id
                dom = await cap.resolve_fragment("browser.dom")
                assert "outer" in dom.element_tree.serialized_dom
                result = await cap.execute_action(
                    Action(name="browser.navigate", params={"url": site.url("worker")})
                )
                assert result.success, result.error
                assert cap._recorder is not None and cap._recorder.lost is None
            finally:
                await cap.teardown()

    await asyncio.wait_for(_run(), _TIMEOUT_S)
