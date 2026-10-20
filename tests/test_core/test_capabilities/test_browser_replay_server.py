"""The replay server against a scripted browser (fake CDP).

§4.14 "服务": what it sends to each target before resume, answering on the
pausing session (a worker's request on its page's session; an OOPIF's
document on the parent's, as ``sub``; a popup's first document as ``main``),
the log it keeps, what Fetch never sees, and the invariant the whole design
rests on — **no decision ever continues a request to the network**. The
live counterpart is ``test_browser_replay_live.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.recorder import (
    PausedRequest,
    RecorderConnection,
    RecorderTarget,
)
from spectragent.core.capabilities.browser.replay import ArchiveNetworkSource
from spectragent.core.capabilities.browser.replay.archive_index import RecordedArchive
from spectragent.core.capabilities.browser.replay.matcher import MatcherConfig
from spectragent.core.capabilities.browser.replay.miss_policy import MissDecision, ReplayMiss
from spectragent.core.capabilities.browser.replay.server import (
    REQUEST_STAGE_PATTERN,
    ReplayContext,
    ReplayServer,
)
from spectragent.core.capabilities.browser.replay.shims import SERVICE_WORKER_BLOCK
from tests._helpers.fake_cdp import FakeBrowser, FakeClient
from tests._helpers.replay_archive import RecordingScript

NOW = 1_800_000_000.0
FORM = "application/x-www-form-urlencoded"
#: Every Fetch command that answers or releases a paused request.
_ANSWERS = (
    "Fetch.continueRequest",
    "Fetch.continueResponse",
    "Fetch.failRequest",
    "Fetch.fulfillRequest",
)


@pytest.fixture(scope="module")
def source_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One recorded run (M29's real session, committed by the real WAL)."""

    async def _record() -> Path:
        script = RecordingScript(tmp_path_factory.mktemp("source"), run_id="run-src")
        await script.exchange(
            "http://shop.test/",
            body=b"<html>home</html>",
            headers={"Content-Type": "text/html"},
            resource_type="Document",
        )
        await script.exchange("http://shop.test/api/data.json", body=b'{"n":1}')
        await script.exchange("http://shop.test/api/data.json", body=b'{"n":2}')
        await script.exchange(
            "http://shop.test/login",
            method="POST",
            post=b"user=ann&password=hunter2",
            request_headers={"Content-Type": FORM},
            body=b"welcome",
        )
        await script.exchange("http://shop.test/worker-data.json", body=b"[1,2,3]")
        await script.exchange(
            "http://shop.test/clip.mp4",
            body=bytes(range(200)),
            headers={"Content-Type": "video/mp4"},
            resource_type="Media",
        )
        await script.redirect("http://shop.test/old", "http://shop.test/new")
        await script.too_large("http://shop.test/huge.bin")
        await script.commit(1)
        return script.finish()

    return asyncio.run(_record())


def _recorded(run: Path) -> RecordedArchive:
    return ArchiveNetworkSource(
        matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules")
    ).open_replay(open_archive(run))


async def _serving(
    run: Path, *, source: ArchiveNetworkSource | None = None, **recorder_kwargs: Any
) -> tuple[FakeBrowser, RecorderConnection, FakeClient, ReplayServer, str]:
    browser = FakeBrowser()
    client = browser.client("ws://fake/devtools/browser", max_frame_size=None)
    await client.start()
    box: list[ReplayServer] = []
    recorder = RecorderConnection(
        client, participants=lambda: box, channel=None, on_lost=lambda _e: None, **recorder_kwargs
    )
    source = source or ArchiveNetworkSource(
        matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules")
    )
    server = source.serve(
        source.open_replay(open_archive(run)),
        ReplayContext(recorder=recorder, cycle=lambda: 7, clock=lambda: NOW),
    )
    box.append(server)
    await recorder.open()
    sid = recorder.session_for_target("page-1")
    assert sid is not None
    return browser, recorder, client, server, sid


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


def _pause(
    client: FakeClient,
    sid: str,
    request_id: str,
    url: str,
    *,
    method: str = "GET",
    resource_type: str = "XHR",
    frame_id: str = "frame-x",
    headers: dict[str, str] | None = None,
    post: bytes | None = None,
    network_id: str | None = None,
) -> None:
    request: dict[str, Any] = {
        "url": url,
        "method": method,
        "headers": headers or {"Accept": "*/*"},
    }
    if post is not None:
        request["hasPostData"] = True
        request["postData"] = post.decode()
        request["postDataEntries"] = [{"bytes": base64.b64encode(post).decode()}]
    params: dict[str, Any] = {
        "requestId": request_id,
        "request": request,
        "frameId": frame_id,
        "resourceType": resource_type,
        "networkId": network_id or f"net-{request_id}",
    }
    client.fire("Fetch.requestPaused", params, sid)


def _answers(client: FakeClient) -> list[tuple[str, str, str | None]]:
    return [
        (s.method, s.params["requestId"], s.session_id) for s in client.sent if s.method in _ANSWERS
    ]


def _sent(client: FakeClient, request_id: str) -> dict[str, Any]:
    return next(
        s.params
        for s in client.sent
        if s.method in _ANSWERS and s.params.get("requestId") == request_id
    )


def _body(params: dict[str, Any]) -> bytes:
    return base64.b64decode(params["body"])


def _headers(params: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in params["responseHeaders"]}


def _log(server: ReplayServer) -> list[dict[str, Any]]:
    return server.log.drain()


# ── preparation ───────────────────────────────────────────────────────


class TestPreparation:
    async def test_every_target_type_gets_its_commands_before_resume(
        self, source_run: Path
    ) -> None:
        browser, recorder, client, _server, page_sid = await _serving(source_run)
        frame = [
            "Network.enable",
            "Network.setCacheDisabled",
            "Network.setBypassServiceWorker",
            "Page.enable",
            "Page.addScriptToEvaluateOnNewDocument",
            "Fetch.enable",
            "Target.setAutoAttach",
        ]
        assert client.methods(session_id=page_sid) == frame  # attached running: no resume
        cases = {
            "pop-1": ("page", None, [*frame, "Runtime.runIfWaitingForDebugger"]),
            "if-1": ("iframe", page_sid, [*frame, "Runtime.runIfWaitingForDebugger"]),
            "w-1": (
                "worker",
                page_sid,
                [
                    "Network.enable",
                    "Network.setCacheDisabled",
                    "Target.setAutoAttach",
                    "Runtime.runIfWaitingForDebugger",
                ],
            ),
            "sw-1": (
                "service_worker",
                None,
                [
                    "Network.enable",
                    "Network.setCacheDisabled",
                    "Fetch.enable",
                    "Target.setAutoAttach",
                    "Runtime.runIfWaitingForDebugger",
                ],
            ),
        }
        for target_id, (kind, parent, expected) in cases.items():
            scope = (client, parent) if parent else None
            sid = browser.open_target(target_id, kind, parent=scope)[client.id]
            await _settle()
            assert client.methods(session_id=sid) == expected, kind
            assert browser.is_running(target_id)
        fetch = next(s for s in client.sent if s.method == "Fetch.enable")
        assert fetch.params == {"patterns": [dict(REQUEST_STAGE_PATTERN)]}
        script = next(s for s in client.sent if s.method == "Page.addScriptToEvaluateOnNewDocument")
        assert script.params == {"source": SERVICE_WORKER_BLOCK, "runImmediately": True}
        await recorder.close()

    def test_it_owns_the_request_stage_and_serves(self, source_run: Path) -> None:
        server = ArchiveNetworkSource(
            matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules"), fulfill_timeout_s=3.5
        ).serve(
            _recorded(source_run),
            ReplayContext(recorder=None, cycle=lambda: 0),  # type: ignore[arg-type]
        )
        assert server.serves_requests and server.pause_budget_s == 3.5
        page = RecorderTarget(session_id="s", target_id="t", type="page")
        worker = RecorderTarget(session_id="w", target_id="w", type="worker")
        assert server.fetch_patterns(page) == (REQUEST_STAGE_PATTERN,)
        assert server.fetch_patterns(worker) == ()


# ── answering ─────────────────────────────────────────────────────────


class TestAnswering:
    async def test_a_hit_is_fulfilled_on_the_pausing_session(self, source_run: Path) -> None:
        _browser, recorder, client, server, sid = await _serving(source_run)
        _pause(client, sid, "q1", "http://shop.test/api/data.json")
        await _settle()
        assert _answers(client) == [("Fetch.fulfillRequest", "q1", sid)]
        params = _sent(client, "q1")
        assert params["responseCode"] == 200 and _body(params) == b'{"n":1}'
        (entry,) = _log(server)
        assert entry["kind"] == "hit" and entry["level"] == "exact"
        assert entry["run_id"] == "run-src" and entry["record_id"]
        assert entry["answer"] == "fulfilled" and entry["status"] == 200
        assert entry["session_id"] == sid and entry["target_id"] == "page-1"
        assert entry["cycle"] == 7 and entry["captured_at"] == NOW
        await recorder.close()

    async def test_records_are_served_in_recording_order_then_repeated(
        self, source_run: Path
    ) -> None:
        _browser, recorder, client, server, sid = await _serving(source_run)
        for n in range(3):
            _pause(client, sid, f"q{n}", "http://shop.test/api/data.json")
            await _settle()
        bodies = [_body(_sent(client, f"q{n}")) for n in range(3)]
        assert bodies == [b'{"n":1}', b'{"n":2}', b'{"n":2}']
        assert [entry["kind"] for entry in _log(server)] == ["hit", "hit", "exhausted"]
        await recorder.close()

    async def test_a_worker_request_is_answered_on_the_page_session(self, source_run: Path) -> None:
        browser, recorder, client, server, page_sid = await _serving(source_run)
        browser.open_target("w-1", "worker", parent=(client, page_sid))
        await _settle()
        _pause(client, page_sid, "qw", "http://shop.test/worker-data.json")
        await _settle()
        assert _answers(client) == [("Fetch.fulfillRequest", "qw", page_sid)]
        await recorder.close()

    async def test_the_live_body_is_redacted_before_any_key(self, source_run: Path) -> None:
        """The archive holds ``password=<redacted>``; a live password still hits exactly."""
        _browser, recorder, client, server, sid = await _serving(source_run)
        _pause(
            client,
            sid,
            "qp",
            "http://shop.test/login",
            method="POST",
            headers={"Content-Type": FORM},
            post=b"user=ann&password=a-different-secret",
        )
        await _settle()
        assert _body(_sent(client, "qp")) == b"welcome"
        (entry,) = _log(server)
        assert entry["kind"] == "hit" and entry["level"] == "exact"
        assert "secret" not in json.dumps(entry)
        assert entry["body_key"] == "password=%3Credacted%3E&user=ann"
        await recorder.close()

    async def test_a_recorded_redirect_goes_out_as_recorded(self, source_run: Path) -> None:
        _browser, recorder, client, _server, sid = await _serving(source_run)
        _pause(
            client, sid, "qr", "http://shop.test/old", resource_type="Document", frame_id="page-1"
        )
        await _settle()
        params = _sent(client, "qr")
        assert params["responseCode"] == 302
        assert _headers(params)["location"] == "http://shop.test/new"
        await recorder.close()

    async def test_a_range_is_206_from_the_range_map(self, source_run: Path) -> None:
        _browser, recorder, client, server, sid = await _serving(source_run)
        _pause(
            client,
            sid,
            "qv",
            "http://shop.test/clip.mp4",
            resource_type="Media",
            headers={"Range": "bytes=100-"},
        )
        await _settle()
        params = _sent(client, "qv")
        assert params["responseCode"] == 206 and _body(params) == bytes(range(100, 200))
        assert _headers(params)["content-range"] == "bytes 100-199/200"
        (entry,) = _log(server)
        assert entry["kind"] == "hit" and entry["range"] == "bytes 100-199/200"
        await recorder.close()

    async def test_an_unarchived_preflight_is_synthesised(self, source_run: Path) -> None:
        _browser, recorder, client, server, sid = await _serving(source_run)
        _pause(
            client,
            sid,
            "qo",
            "http://api.other.test/v1",
            method="OPTIONS",
            headers={"Origin": "http://shop.test", "Access-Control-Request-Method": "PUT"},
        )
        await _settle()
        params = _sent(client, "qo")
        assert params["responseCode"] == 204
        assert _headers(params)["access-control-allow-origin"] == "http://shop.test"
        (entry,) = _log(server)
        assert entry["kind"] == "synthetic" and entry["answer"] == "fulfilled"
        await recorder.close()


# ── misses ────────────────────────────────────────────────────────────


class TestMisses:
    async def test_a_main_frame_document_miss_fails_and_diverges(self, source_run: Path) -> None:
        _browser, recorder, client, server, sid = await _serving(source_run)
        _pause(
            client, sid, "qd", "http://shop.test/never", resource_type="Document", frame_id="page-1"
        )
        await _settle()
        assert _sent(client, "qd") == {"requestId": "qd", "errorReason": "Failed"}
        (entry,) = _log(server)
        assert entry["kind"] == "miss" and entry["frame"] == "main" and entry["diverged"]
        assert entry["request_class"] == "document" and entry["answer"] == "failed"
        assert entry["reason"] == "no_match"
        assert [c["url"] for c in entry["candidates"]][:1] == ["http://shop.test/"]
        await recorder.close()

    async def test_an_oopif_document_on_the_parent_session_is_a_sub_frame(
        self, source_run: Path
    ) -> None:
        browser, recorder, client, server, sid = await _serving(source_run)
        browser.open_target("if-1", "iframe", parent=(client, sid))
        await _settle()
        # The OOPIF's document pauses on its parent page's session, with its own frame id.
        _pause(
            client,
            sid,
            "qf",
            "http://ads.other.test/frame",
            resource_type="Document",
            frame_id="if-1",
        )
        await _settle()
        assert _sent(client, "qf")["errorReason"] == "Failed"
        (entry,) = _log(server)
        assert entry["frame"] == "sub" and not entry.get("diverged", False)
        assert entry["session_id"] == sid
        await recorder.close()

    async def test_a_popups_first_document_is_its_main_frame(self, source_run: Path) -> None:
        browser, recorder, client, server, _sid = await _serving(source_run)
        pop_sid = browser.open_target("pop-1", "page")[client.id]
        await _settle()
        _pause(
            client,
            pop_sid,
            "qp",
            "http://shop.test/popup",
            resource_type="Document",
            frame_id="pop-1",
        )
        await _settle()
        assert _answers(client) == [("Fetch.failRequest", "qp", pop_sid)]
        (entry,) = _log(server)
        assert entry["frame"] == "main" and entry["diverged"] and entry["target_id"] == "pop-1"
        await recorder.close()

    async def test_a_subresource_miss_is_a_marked_404(self, source_run: Path) -> None:
        _browser, recorder, client, server, sid = await _serving(source_run)
        _pause(client, sid, "qs", "http://shop.test/api/unknown.json")
        await _settle()
        params = _sent(client, "qs")
        assert params["responseCode"] == 404 and _headers(params)["x-replay-miss"] == "1"
        (entry,) = _log(server)
        assert entry["request_class"] == "subresource" and entry["status"] == 404
        await recorder.close()

    async def test_a_body_over_the_cap_is_a_too_large_miss(self, source_run: Path) -> None:
        source = ArchiveNetworkSource(
            matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules"),
            max_fulfill_body_bytes=100,
        )
        _browser, recorder, client, server, sid = await _serving(source_run, source=source)
        _pause(client, sid, "qt", "http://shop.test/clip.mp4", resource_type="Media")
        _pause(client, sid, "qh", "http://shop.test/huge.bin")
        await _settle()
        assert _sent(client, "qt")["responseCode"] == 404
        assert _sent(client, "qh")["responseCode"] == 404
        reasons = {entry["url"]: entry["reason"] for entry in _log(server)}
        assert reasons == {
            "http://shop.test/clip.mp4": "too_large",
            "http://shop.test/huge.bin": "too_large",
        }
        await recorder.close()

    def test_websocket_and_unservable_requests_are_logged(self, source_run: Path) -> None:
        recorder = _SilentRecorder()
        server = ArchiveNetworkSource(
            matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules")
        ).serve(
            _recorded(source_run),
            ReplayContext(recorder=recorder, cycle=lambda: 3, clock=lambda: NOW),
        )
        _deliver(
            server, "Network.webSocketCreated", {"requestId": "ws1", "url": "ws://shop.test/live"}
        )
        # A nested worker's script: announced, never paused, failed.
        _deliver(
            server,
            "Network.requestWillBeSent",
            {
                "requestId": "n1",
                "type": "Script",
                "request": {"url": "http://shop.test/nested.js", "method": "GET", "postData": "x"},
            },
            session="worker-s",
        )
        _deliver(
            server, "Network.loadingFailed", {"requestId": "n1", "errorText": "net::ERR_FAILED"}
        )
        # A data: URL is never tracked.
        assert (
            server.admit_event(
                "Network.requestWillBeSent", {"requestId": "d", "request": {"url": "data:,x"}}, "s"
            )
            is None
        )
        entries = _log(server)
        assert [(e["reason"], e["answer"]) for e in entries] == [
            ("websocket", "none"),
            ("unservable", "none"),
        ]
        assert entries[1]["url"] == "http://shop.test/nested.js"
        assert entries[1]["resource_type"] == "Script" and entries[1]["session_id"] == "worker-s"

    async def test_a_paused_request_is_never_unservable_whatever_the_order(
        self, source_run: Path
    ) -> None:
        recorder = _SilentRecorder()
        server = ArchiveNetworkSource(
            matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules")
        ).serve(
            _recorded(source_run),
            ReplayContext(recorder=recorder, cycle=lambda: 3, clock=lambda: NOW),
        )
        target = RecorderTarget(session_id="s", target_id="page-1", type="page")
        announce = {
            "requestId": "a",
            "type": "XHR",
            "request": {"url": "http://shop.test/api/data.json"},
        }
        # Announced first, then paused.
        _deliver(server, "Network.requestWillBeSent", announce)
        await server.on_paused(_paused(target, "fa", "http://shop.test/api/data.json", "a"))
        _deliver(server, "Network.loadingFinished", {"requestId": "a"})
        # Paused first (the scope is behind), then announced.
        await server.on_paused(_paused(target, "fb", "http://shop.test/api/data.json", "b"))
        _deliver(server, "Network.requestWillBeSent", {**announce, "requestId": "b"})
        _deliver(server, "Network.loadingFailed", {"requestId": "b"})
        # A redirect chain under one id, both hops paused before either announcement.
        old = "http://shop.test/old"
        await server.on_paused(_paused(target, "fc1", old, "c"))
        await server.on_paused(_paused(target, "fc2", "http://shop.test/new", "c"))
        _deliver(server, "Network.requestWillBeSent", {**announce, "requestId": "c"})
        _deliver(server, "Network.requestWillBeSent", {**announce, "requestId": "c"})
        _deliver(server, "Network.loadingFinished", {"requestId": "c"})
        assert all(
            entry["kind"] != "miss" or entry["reason"] != "unservable" for entry in _log(server)
        )
        assert server.pending_sessions() == set()


# ── the invariant, and state ──────────────────────────────────────────


class _BrokenPolicy:
    """A miss policy with a bug (a plain object: subclassing would register it)."""

    def decide(self, miss: ReplayMiss) -> MissDecision:
        raise RuntimeError("policy bug")


class TestNeverContinue:
    async def test_no_decision_ever_continues_a_request(self, source_run: Path) -> None:
        """Every path — hit, exhausted, range, preflight, every miss class, a bug, a
        timeout, a failed send, a request held at close, no target — and not one continue."""
        source = ArchiveNetworkSource(
            matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules"), fulfill_timeout_s=0.05
        )
        browser, recorder, client, server, sid = await _serving(source_run, source=source)
        paths = [
            ("hit", "http://shop.test/", {"resource_type": "Document", "frame_id": "page-1"}),
            ("exhausted-1", "http://shop.test/worker-data.json", {}),
            ("exhausted-2", "http://shop.test/worker-data.json", {}),
            ("range", "http://shop.test/clip.mp4", {"headers": {"Range": "bytes=0-"}}),
            (
                "preflight",
                "http://x.test/",
                {
                    "method": "OPTIONS",
                    "headers": {
                        "Origin": "http://shop.test",
                        "Access-Control-Request-Method": "PUT",
                    },
                },
            ),
            (
                "main-doc",
                "http://shop.test/gone",
                {"resource_type": "Document", "frame_id": "page-1"},
            ),
            ("sub-doc", "http://ads.test/", {"resource_type": "Document", "frame_id": "f-9"}),
            (
                "benign",
                "https://www.google-analytics.com/collect",
                {"resource_type": "Ping", "method": "POST"},
            ),
            ("404", "http://shop.test/missing.css", {"resource_type": "Stylesheet"}),
            ("too-large", "http://shop.test/huge.bin", {}),
        ]
        for request_id, url, extra in paths:
            _pause(client, sid, request_id, url, **extra)
        _pause(client, "no-such-session", "no-target", "http://shop.test/")
        await _settle()
        # A failed send: the recorder fails it.
        browser.fail("Fetch.fulfillRequest", RuntimeError("CDP error: Invalid header"))
        _pause(client, sid, "send-fails", "http://shop.test/api/data.json")
        await _settle()
        browser.failures.clear()
        # A send that never answers: the owner times out, the recorder fails it.
        gate = browser.hold("Fetch.fulfillRequest")
        _pause(client, sid, "timeout", "http://shop.test/api/data.json")
        await asyncio.sleep(1.3)  # fulfill_timeout_s + the recorder's grace
        gate.set()
        browser.holds.clear()
        # A bug in the policy: failed by the recorder, and the next drain raises.
        server.policy = _BrokenPolicy()
        _pause(client, sid, "bug", "http://shop.test/nothing-recorded")
        await _settle()
        # Held at close.
        browser.hold("Fetch.fulfillRequest")
        _pause(client, sid, "held", "http://shop.test/api/data.json")
        await _settle()
        await recorder.close()

        methods = {s.method for s in client.sent}
        assert "Fetch.continueRequest" not in methods and "Fetch.continueResponse" not in methods
        answered = {request_id for _, request_id, _ in _answers(client)}
        expected = {request_id for request_id, _, _ in paths} | {
            "no-target",
            "send-fails",
            "timeout",
            "bug",
            "held",
        }
        assert expected <= answered
        fails = {
            request_id
            for method, request_id, _ in _answers(client)
            if method == "Fetch.failRequest"
        }
        assert {"main-doc", "sub-doc", "no-target", "send-fails", "timeout", "bug", "held"} <= fails
        with pytest.raises(RuntimeError, match="policy bug"):
            server.log.drain()


#: A recorded jQuery JSONP call: the built-in normalisation drops the callback
#: value from the key, so another name matches it (§4.14 "JSONP 回调名").
JSONP_URL = "http://shop.test/api/hot?size=2&callback={name}&_={ts}"
JSONP_RECORDED = "jQuery371_1791958613693"
JSONP_DATA = b'({"hot":["a","b"]});'


@pytest.fixture(scope="module")
def jsonp_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    async def _record() -> Path:
        script = RecordingScript(tmp_path_factory.mktemp("jsonp"), run_id="run-jsonp")
        await script.exchange(
            JSONP_URL.format(name=JSONP_RECORDED, ts=1791958613694),
            body=JSONP_RECORDED.encode() + JSONP_DATA,
            headers={"Content-Type": "application/json; charset=utf-8"},
            resource_type="Script",
        )
        await script.commit(1)
        return script.finish()

    return asyncio.run(_record())


class TestJsonpCallbackNames:
    async def test_a_hit_calls_the_name_this_request_asks_for(self, jsonp_run: Path) -> None:
        _browser, recorder, client, server, sid = await _serving(jsonp_run)
        asked = "jQuery371_1791958613701"
        _pause(client, sid, "q1", JSONP_URL.format(name=asked, ts=1), resource_type="Script")
        await _settle()
        params = _sent(client, "q1")
        assert _body(params) == asked.encode() + JSONP_DATA
        assert _headers(params)["content-length"] == str(len(asked) + len(JSONP_DATA))
        (entry,) = _log(server)
        assert (entry["kind"], entry["level"], entry["answer"]) == (
            "hit",
            "normalized",
            "fulfilled",
        )
        assert entry["callback_rewrite"] == {
            "parameter": "callback",
            "recorded": JSONP_RECORDED,
            "requested": asked,
        }
        await recorder.close()

    async def test_the_recorded_name_is_served_as_recorded(self, jsonp_run: Path) -> None:
        _browser, recorder, client, server, sid = await _serving(jsonp_run)
        url = JSONP_URL.format(name=JSONP_RECORDED, ts=1791958613694)
        _pause(client, sid, "q1", url, resource_type="Script")
        await _settle()
        assert _body(_sent(client, "q1")) == JSONP_RECORDED.encode() + JSONP_DATA
        (entry,) = _log(server)
        assert entry["level"] == "exact" and "callback_rewrite" not in entry
        await recorder.close()

    async def test_repeats_fold_and_count_every_rewrite(self, jsonp_run: Path) -> None:
        _browser, recorder, client, server, sid = await _serving(
            jsonp_run,
            source=ArchiveNetworkSource(
                matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules"), log_mode="summary"
            ),
        )
        names = [f"jQuery371_17919586137{i:02d}" for i in range(6)]
        for index, name in enumerate(names):
            _pause(
                client,
                sid,
                f"q{index}",
                JSONP_URL.format(name=name, ts=index),
                resource_type="Script",
            )
            await _settle()
        for index, name in enumerate(names):
            assert _body(_sent(client, f"q{index}")) == name.encode() + JSONP_DATA
        entries = _log(server)
        # One hit, then the record repeated: three exhausted entries in full (each with
        # its own name), the other two folded into one summary that keeps the last.
        assert [e["kind"] for e in entries] == [
            "hit",
            "exhausted",
            "exhausted",
            "exhausted",
            "summary",
        ]
        assert [e["callback_rewrite"]["requested"] for e in entries] == [*names[:4], names[5]]
        summary = entries[-1]
        assert (summary["folded_kind"], summary["count"]) == ("exhausted", 2)
        await recorder.close()


class TestState:
    async def test_closing_a_tab_mid_pause_leaves_no_state(self, source_run: Path) -> None:
        browser, recorder, client, server, _sid = await _serving(
            source_run, pause_owner_timeout_s=5
        )
        pop_sid = browser.open_target("pop-1", "page")[client.id]
        await _settle()
        # The popup announces a request that will never end (its tab closes).
        _deliver(
            server,
            "Network.requestWillBeSent",
            {"requestId": "n-pop", "type": "XHR", "request": {"url": "http://shop.test/slow"}},
            session=pop_sid,
        )
        gate = browser.hold("Fetch.fulfillRequest")
        _pause(client, pop_sid, "mid", "http://shop.test/api/data.json")
        await _settle()
        assert pop_sid in server.pending_sessions()
        assert recorder._held  # the pause is still being answered
        # The tab closes: the recorder's detach bookkeeping reaches the server too.
        client.fire("Target.detachedFromTarget", {"sessionId": pop_sid}, None)
        from spectragent.core.capabilities.browser.recorder import RecorderEvent

        server.on_event(
            RecorderEvent(
                session_id=pop_sid,
                method="Target.detachedFromTarget",
                params={"sessionId": pop_sid},
            )
        )
        gate.set()
        await _settle()
        assert server.pending_sessions() == set()
        assert not recorder._held and not recorder._pause_tasks
        assert recorder.target(pop_sid) is None
        await recorder.close()
        server.close()
        assert server.pending_sessions() == set()


def test_the_fingerprint_names_the_source_run_and_the_warnings(source_run: Path) -> None:
    recorded = _recorded(source_run)
    server = ArchiveNetworkSource(
        matcher=MatcherConfig(site_rules="tests/fixtures/replay_rules"), shift_dates="all"
    ).serve(
        recorded,
        ReplayContext(recorder=None, cycle=lambda: 0),  # type: ignore[arg-type]
    )
    server.warn("Chrome version differs: recorded X, now Y")
    server.warn("Chrome version differs: recorded X, now Y")
    block = server.fingerprint()
    assert block == {
        "source_id": "archive",
        "matcher": {
            "levels": ["exact", "normalized"],
            "fuzzy_threshold": 0.5,
            "site_rules": "tests/fixtures/replay_rules",
        },
        "miss_policy_id": "strict",
        "shift_dates": "all",
        "isolation": "required",
        "source_run_id": "run-src",
        "source_fingerprint_sha256": recorded.fingerprint_sha256,
        "site_rules_sha256": recorded.rules.sha256,
        "site_rules_snapshot": recorded.rules.snapshot().model_dump(mode="json"),
        "log_mode": "full",
        "log_repeat_limit": 3,
        "warnings": ["Chrome version differs: recorded X, now Y"],
    }
    assert recorded.fingerprint_sha256


# ── helpers for the direct (no recorder) cases ────────────────────────


class _SilentRecorder:
    """A recorder handle that answers every send and knows no target."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        self.sent.append((method, dict(params or {}), session_id))
        return {}

    def target(self, session_id: str) -> RecorderTarget | None:
        return None

    def targets(self) -> dict[str, RecorderTarget]:
        return {}

    def session_for_target(self, target_id: str) -> str | None:
        return None

    async def browser_version(self) -> dict[str, Any]:
        return {}


def _deliver(
    server: ReplayServer, method: str, params: dict[str, Any], *, session: str = "s"
) -> None:
    admitted = server.admit_event(method, params, session)
    if admitted is not None:
        assert "postData" not in json.dumps(admitted.params)
        server.on_event(admitted)


def _paused(target: RecorderTarget, request_id: str, url: str, network_id: str) -> PausedRequest:
    return PausedRequest(
        session_id=target.session_id,
        request_id=request_id,
        stage="Request",
        params={
            "requestId": request_id,
            "networkId": network_id,
            "request": {"url": url, "method": "GET", "headers": {}},
            "resourceType": "XHR",
        },
        target=target,
    )


async def test_unreadable_source_body_is_a_distinct_offline_miss(tmp_path: Path) -> None:
    script = RecordingScript(tmp_path / "source", run_id="run-src")
    await script.streamed_later("http://shop.test/api/data.json", b"response" * 1024)
    await script.commit(0)
    source_run = script.finish()
    _browser, recorder, client, server, sid = await _serving(source_run)
    reader = open_archive(source_run)
    (blob,) = [
        entry for _step, entry in reader.iter_archive("browser.network") if entry.content_addressed
    ]
    (source_run / blob.path).unlink()
    _pause(client, sid, "bad-body", "http://shop.test/api/data.json")
    await _settle()
    (entry,) = _log(server)
    assert entry["kind"] == "miss"
    assert entry["reason"] == "archive_unreadable"
    assert entry["record_id"] and entry["level"] == "exact"
    assert entry["status"] == 404
    assert all(sent.method != "Fetch.continueRequest" for sent in client.sent)
    await recorder.close()


# ── recorded requests without a body (§4.14 "匹配", "回放日志") ─────────

PICTURE = "http://cdn.test/pic.webp"
PICTURE_BYTES = b"RIFF....WEBPfour eggs"
CROSS_SITE = {"Accept": "image/webp", "Origin": "http://shop.test"}
SAME_SITE = {"Accept": "image/webp"}


def _rules(root: Path, policy: str) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "rules.yaml").write_text(
        "rules:\n"
        "  - id: everything\n"
        "    scope: {}\n"
        "    exhaustion: repeat_last\n"
        f"    unavailable: {policy}\n"
    )
    return str(root)


@pytest.fixture(scope="module")
def unavailable_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A picture loaded twice and then cancelled; an ad blocked; a thumbnail always cancelled."""

    async def _record() -> Path:
        script = RecordingScript(tmp_path_factory.mktemp("unavailable"), run_id="run-pic")
        image = {"Content-Type": "image/webp", "Access-Control-Allow-Origin": "*"}
        await script.exchange(
            PICTURE,
            body=PICTURE_BYTES,
            headers={**image, "X-Recorded-As": "cross-site"},
            request_headers=CROSS_SITE,
            resource_type="Image",
        )
        await script.exchange(
            PICTURE,
            body=PICTURE_BYTES,
            headers={**image, "X-Recorded-As": "same-site"},
            request_headers=SAME_SITE,
            resource_type="Image",
        )
        script.failed(
            PICTURE,
            error_text="net::ERR_ABORTED",
            request_headers=CROSS_SITE,
            resource_type="Image",
        )
        await script.exchange("http://ads.test/ad.png", body=b"PNG", resource_type="Image")
        script.failed(
            "http://ads.test/ad.png", error_text="net::ERR_BLOCKED_BY_ORB", resource_type="Image"
        )
        script.failed(
            "http://cdn.test/thumb.webp", error_text="net::ERR_ABORTED", resource_type="Image"
        )
        await script.commit(0)
        return script.finish()

    return asyncio.run(_record())


class TestRecordsWithoutABody:
    async def _serve(
        self, run: Path, tmp_path: Path, policy: str
    ) -> tuple[RecorderConnection, FakeClient, ReplayServer, str]:
        source = ArchiveNetworkSource(
            matcher=MatcherConfig(site_rules=_rules(tmp_path / policy, policy))
        )
        _browser, recorder, client, server, sid = await _serving(run, source=source)
        return recorder, client, server, sid

    async def _three_pictures(self, client: FakeClient, sid: str) -> None:
        for request_id, headers in (("p1", CROSS_SITE), ("p2", SAME_SITE), ("p3", CROSS_SITE)):
            _pause(client, sid, request_id, PICTURE, resource_type="Image", headers=headers)
            await _settle()

    async def test_the_cancelled_request_is_answered_by_the_identical_cross_site_record(
        self, unavailable_run: Path, tmp_path: Path
    ) -> None:
        recorder, client, server, sid = await self._serve(
            unavailable_run, tmp_path, "serve_identical"
        )
        await self._three_pictures(client, sid)
        third = _sent(client, "p3")
        assert (third["responseCode"], _body(third)) == (200, PICTURE_BYTES)
        assert _headers(third)["x-recorded-as"] == "cross-site"
        assert "x-replay-miss" not in _headers(third)
        first, _second, substituted = _log(server)
        assert substituted["kind"] == "substituted"
        assert substituted["served_record_id"] == first["record_id"]
        assert substituted["record_id"] not in (first["record_id"], _second["record_id"])
        assert substituted["source_outcome"] == "page_cancelled_before_response"
        assert (substituted["unavailable_policy"], substituted["exhaustion_policy"]) == (
            "serve_identical",
            "repeat_last",
        )
        assert (substituted["answer"], substituted["status"], substituted["level"]) == (
            "fulfilled",
            200,
            "exact",
        )
        assert "reason" not in substituted
        await recorder.close()

    async def test_under_miss_it_stays_a_404_that_says_how_the_source_ended(
        self, unavailable_run: Path, tmp_path: Path
    ) -> None:
        recorder, client, server, sid = await self._serve(unavailable_run, tmp_path, "miss")
        await self._three_pictures(client, sid)
        third = _sent(client, "p3")
        assert (third["responseCode"], _headers(third)["x-replay-miss"]) == (404, "1")
        entry = _log(server)[-1]
        assert (entry["kind"], entry["reason"], entry["source_outcome"]) == (
            "miss",
            "body_unavailable",
            "page_cancelled_before_response",
        )
        assert entry["unavailable_policy"] == "miss" and "served_record_id" not in entry
        await recorder.close()

    async def test_a_source_network_failure_stays_a_failure(
        self, unavailable_run: Path, tmp_path: Path
    ) -> None:
        recorder, client, server, sid = await self._serve(
            unavailable_run, tmp_path, "serve_identical"
        )
        for request_id in ("a1", "a2"):
            _pause(client, sid, request_id, "http://ads.test/ad.png", resource_type="Image")
            await _settle()
        assert _sent(client, "a1")["responseCode"] == 200
        assert _sent(client, "a2")["responseCode"] == 404
        entry = _log(server)[-1]
        assert (entry["kind"], entry["reason"], entry["source_outcome"]) == (
            "miss",
            "body_unavailable",
            "network_failed",
        )
        await recorder.close()

    async def test_nothing_to_substitute_keeps_the_404_and_logs_the_outcome(
        self, unavailable_run: Path, tmp_path: Path
    ) -> None:
        recorder, client, server, sid = await self._serve(
            unavailable_run, tmp_path, "serve_identical"
        )
        _pause(client, sid, "t1", "http://cdn.test/thumb.webp", resource_type="Image")
        await _settle()
        assert _sent(client, "t1")["responseCode"] == 404
        (entry,) = _log(server)
        assert (entry["reason"], entry["source_outcome"], entry["request_class"]) == (
            "body_unavailable",
            "page_cancelled_before_response",
            "subresource",
        )
        await recorder.close()

    async def test_a_repeat_of_a_record_without_a_body_is_marked(
        self, unavailable_run: Path, tmp_path: Path
    ) -> None:
        """The first miss on the body-less record is not a repeat; repeat_last's repeats are."""
        recorder, client, server, sid = await self._serve(
            unavailable_run, tmp_path, "serve_identical"
        )
        for request_id in ("t1", "t2", "t3"):
            _pause(client, sid, request_id, "http://cdn.test/thumb.webp", resource_type="Image")
            await _settle()
        entries = _log(server)
        assert [(e["reason"], e.get("repeated")) for e in entries] == [
            ("body_unavailable", None),
            ("body_unavailable", True),
            ("body_unavailable", True),
        ]
        assert {e["record_id"] for e in entries} == {entries[0]["record_id"]}
        assert {e["source_outcome"] for e in entries} == {"page_cancelled_before_response"}
        await recorder.close()

    async def test_a_repeat_that_is_served_is_exhausted_not_marked(
        self, unavailable_run: Path, tmp_path: Path
    ) -> None:
        """Four pictures: three recorded, then the last available one served again."""
        recorder, client, server, sid = await self._serve(
            unavailable_run, tmp_path, "serve_identical"
        )
        await self._three_pictures(client, sid)
        _pause(client, sid, "p4", PICTURE, resource_type="Image", headers=SAME_SITE)
        await _settle()
        assert _body(_sent(client, "p4")) == PICTURE_BYTES
        last = _log(server)[-1]
        assert last["kind"] == "exhausted" and "repeated" not in last
        await recorder.close()

    async def test_a_hit_carries_no_outcome(self, unavailable_run: Path, tmp_path: Path) -> None:
        recorder, client, server, sid = await self._serve(
            unavailable_run, tmp_path, "serve_identical"
        )
        _pause(client, sid, "p1", PICTURE, resource_type="Image", headers=CROSS_SITE)
        await _settle()
        (entry,) = _log(server)
        assert entry["kind"] == "hit"
        assert "source_outcome" not in entry and "served_record_id" not in entry
        await recorder.close()
