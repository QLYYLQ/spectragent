"""``browser.network`` against a scripted recorder (§4.14).

Event by event and pause by pause, with no Chrome: pairing in either order,
redirect hops, ExtraInfo and ``Set-Cookie``, worker and service-worker
sessions, POST sources and redaction, WebSockets, 304 / 206, the size tiers —
inline, streamed while the page reads it, over the 64 MiB limit — timeouts,
revisit records, and what the WARC and the index say. The live
completeness check against the fixture site is ``test_browser_archive_live.py``.
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest
from warcio.archiveiterator import ArchiveIterator

from spectragent.core.archive import ArchiveFile
from spectragent.core.capabilities.browser.archive import NetworkArchiver
from spectragent.core.capabilities.browser.archive import network as network_module
from spectragent.core.capabilities.browser.archive.network import (
    INDEX_NAME,
    MAX_BODY_BYTES,
    WARC_NAME,
    WEBSOCKET_NAME,
    NetworkSession,
    parse_content_range,
)
from spectragent.core.capabilities.browser.archive.network_model import TARGET_DETACHED_ERROR
from spectragent.core.capabilities.browser.recorder import RecorderTarget
from spectragent.core.capabilities.browser.replay.archive_source import (
    DEFAULT_FULFILL_BODY_BYTES,
)
from tests._helpers.archive_recorder import (
    ScriptedRecorder,
    deliver,
    event,
    open_session,
    paused,
    stream_chunks,
)

URL = "http://shop.test/api"
SECRET = "planted-secret-Zx81"


def _session(**knobs: Any) -> tuple[NetworkSession, ScriptedRecorder]:
    archiver = NetworkArchiver(**{"require_replayable": False, "drain_timeout_s": 0.05, **knobs})
    session, recorder = open_session(archiver)
    recorder.add_target("page-s", "PAGE")
    assert isinstance(session, NetworkSession)
    return session, recorder


def _request_event(
    request_id: str,
    url: str = URL,
    *,
    session_id: str = "page-s",
    method: str = "GET",
    headers: dict[str, str] | None = None,
    redirect: dict[str, Any] | None = None,
    **request: Any,
) -> Any:
    params: dict[str, Any] = {
        "requestId": request_id,
        "loaderId": "L1",
        "documentURL": "http://shop.test/",
        "request": {"url": url, "method": method, "headers": headers or {}, **request},
        "timestamp": 10.0,
        "wallTime": 1_700_000_000.0,
        "type": "XHR",
        "frameId": "frame-1",
        "initiator": {"type": "script", "stack": {"callFrames": [{"url": "x"}]}},
    }
    if redirect is not None:
        params["redirectResponse"] = redirect
        params["redirectHasExtraInfo"] = False
    return event("Network.requestWillBeSent", params, session_id)


def _response_event(
    request_id: str, status: int = 200, *, session_id: str = "page-s", extra: bool = False
) -> Any:
    return event(
        "Network.responseReceived",
        {
            "requestId": request_id,
            "type": "XHR",
            "hasExtraInfo": extra,
            "response": {
                "url": URL,
                "status": status,
                "statusText": "OK",
                "headers": {"Content-Type": "text/plain"},
                "mimeType": "text/plain",
                "protocol": "http/1.1",
                "remoteIPAddress": "127.0.0.1",
                "remotePort": 8080,
            },
        },
        session_id,
    )


def _finished(request_id: str, *, session_id: str = "page-s") -> Any:
    return event(
        "Network.loadingFinished",
        {"requestId": request_id, "timestamp": 11.0, "encodedDataLength": 42},
        session_id,
    )


def _files(files: list[ArchiveFile]) -> dict[str, ArchiveFile]:
    return {item.name: item for item in files if not item.content_addressed}


def _index(files: list[ArchiveFile]) -> list[dict[str, Any]]:
    data = _files(files).get(INDEX_NAME)
    return [json.loads(line) for line in data.data.decode().splitlines()] if data else []


def _warc(files: list[ArchiveFile]) -> list[Any]:
    data = _files(files)[WARC_NAME].data
    records = []
    for record in ArchiveIterator(io.BytesIO(data)):
        records.append(
            (
                record.rec_type,
                dict(record.rec_headers.headers),
                record.http_headers,
                record.content_stream().read(),
            )
        )
    return records


async def _one_exchange(session: NetworkSession, recorder: ScriptedRecorder, **pause: Any) -> None:
    recorder.bodies["i1"] = pause.pop("body", b"hello")
    deliver(session, _request_event("n1"))
    await session.on_paused(paused(request_id="i1", network_id="n1", url=URL, **pause))
    deliver(session, _response_event("n1", pause.get("status") or 200))
    deliver(session, _finished("n1"))


class TestPairing:
    async def test_pause_first_or_events_first_is_one_exchange(self) -> None:
        for order in ("events_first", "pause_first"):
            session, recorder = _session()
            recorder.bodies["i1"] = b"body"
            pause = paused(request_id="i1", network_id="n1", url=URL)
            if order == "events_first":
                deliver(session, _request_event("n1"))
                await session.on_paused(pause)
            else:
                await session.on_paused(pause)
                deliver(session, _request_event("n1"))
            deliver(session, _response_event("n1"))
            deliver(session, _finished("n1"))
            (line,) = _index(await session.collect())
            assert (line["kind"], line["body_status"], line["payload_length"]) == (
                "exchange",
                "ok",
                4,
            ), order
            assert line["loader_id"] == "L1" and line["initiator"] == {"type": "script"}

    async def test_a_redirect_chain_is_one_record_per_hop(self) -> None:
        session, recorder = _session()
        recorder.bodies["i2"] = b"landed"
        deliver(session, _request_event("n1", "http://shop.test/hop1"))
        await session.on_paused(
            paused(
                request_id="i1",
                network_id="n1",
                url="http://shop.test/hop1",
                status=302,
                headers={"Location": "/landing"},
            )
        )
        deliver(
            session,
            _request_event(
                "n1",
                "http://shop.test/landing",
                redirect={
                    "status": 302,
                    "statusText": "Found",
                    "headers": {"Location": "/landing"},
                },
            ),
        )
        await session.on_paused(
            paused(request_id="i2", network_id="n1", url="http://shop.test/landing")
        )
        deliver(session, _response_event("n1"))
        deliver(session, _finished("n1"))
        files = await session.collect()
        hop1, landing = _index(files)
        assert (hop1["status"], hop1["body_status"], hop1["hop"]) == (302, "redirect", 0)
        assert (landing["status"], landing["body_status"], landing["hop"]) == (200, "ok", 1)
        assert landing["redirected_from"] == hop1["record_id"]
        assert "Fetch.getResponseBody" in recorder.methods()
        assert [p["requestId"] for m, p, _ in recorder.sent if m == "Fetch.getResponseBody"] == [
            "i2"
        ], "a redirect has no body to read"
        types = [
            (rec_type, http.get_statuscode() if rec_type == "response" else None)
            for rec_type, _, http, _ in _warc(files)
        ]
        assert types == [
            ("request", None),
            ("response", "302"),
            ("request", None),
            ("response", "200"),
        ]

    async def test_a_worker_request_pauses_on_the_page_session(self) -> None:
        session, recorder = _session()
        recorder.add_target("worker-s", "WORKER", "worker")
        recorder.bodies["i1"] = b"{}"
        deliver(session, _request_event("w1", session_id="worker-s"))
        await session.on_paused(
            paused(request_id="i1", network_id="w1", url=URL, session_id="page-s")
        )
        deliver(session, _response_event("w1", session_id="worker-s"))
        deliver(session, _finished("w1", session_id="worker-s"))
        (line,) = _index(await session.collect())
        assert (line["target_type"], line["paused_session_id"], line["body_status"]) == (
            "worker",
            "page-s",
            "ok",
        )

    async def test_two_sessions_of_one_service_worker_record_once(self) -> None:
        session, recorder = _session()
        sw_a = recorder.add_target("sw-a", "SW", "service_worker")
        sw_b = recorder.add_target("sw-b", "SW", "service_worker")
        recorder.bodies["i1"] = recorder.bodies["i2"] = b"sw body"
        for sid in ("sw-a", "sw-b"):
            deliver(session, _request_event("s1", session_id=sid))
        await session.on_paused(
            paused(request_id="i1", network_id="s1", url=URL, session_id="sw-a", target=sw_a)
        )
        await session.on_paused(
            paused(request_id="i2", network_id="s1", url=URL, session_id="sw-b", target=sw_b)
        )
        for sid in ("sw-a", "sw-b"):
            deliver(session, _response_event("s1", session_id=sid))
            deliver(session, _finished("s1", session_id=sid))
        lines = _index(await session.collect())
        assert len(lines) == 1 and lines[0]["body_status"] == "ok"
        assert recorder.methods().count("Fetch.getResponseBody") == 1

    async def test_a_data_url_is_not_traffic(self) -> None:
        session, _ = _session()
        deliver(session, _request_event("d1", "data:text/plain,hi"))
        deliver(session, _finished("d1"))
        assert await session.collect() == []


class TestHeadersAndPrivacy:
    async def test_set_cookie_comes_from_extra_info_and_request_credentials_never_land(
        self,
    ) -> None:
        session, recorder = _session(strip_request_headers=["X-Api-Key"])
        recorder.bodies["i1"] = b"ok"
        headers = {
            "Proxy-Authorization": f"Basic {SECRET}",
            "Cookie": f"sid={SECRET}",
            "Authorization": f"Bearer {SECRET}",
            "X-Api-Key": SECRET,
            "Accept": "*/*",
        }
        deliver(session, _request_event("n1", headers=headers))
        deliver(
            session,
            event(
                "Network.requestWillBeSentExtraInfo",
                {
                    "requestId": "n1",
                    "headers": {**headers, "Host": "shop.test"},
                    "associatedCookies": [{"cookie": {"name": "sid", "value": SECRET}}],
                },
            ),
        )
        await session.on_paused(
            paused(request_id="i1", network_id="n1", url=URL, request_headers=headers)
        )
        deliver(session, _response_event("n1", extra=True))
        deliver(
            session,
            event(
                "Network.responseReceivedExtraInfo",
                {
                    "requestId": "n1",
                    "statusCode": 200,
                    "headers": {"Content-Type": "text/plain", "Set-Cookie": "a=1\nb=2"},
                    "blockedCookies": [{"cookieLine": f"c={SECRET}", "blockedReasons": []}],
                },
            ),
        )
        deliver(session, _finished("n1"))
        files = await session.collect()
        everything = b"".join(item.data for item in files)
        warc = _warc(files)
        assert SECRET.encode() not in everything
        assert SECRET.encode() not in b"".join(body for *_, body in warc)
        response_http = warc[1][2]
        assert [v for k, v in response_http.headers if k.lower() == "set-cookie"] == ["a=1", "b=2"]
        (line,) = _index(files)
        assert line["request"]["headers"] == {"Accept": "*/*"}
        assert line["request"]["raw_headers"] == {"Accept": "*/*", "Host": "shop.test"}
        assert line["raw_response_headers"] is True
        stripped = {(e["location"], e["field"]) for e in line["redaction"]["entries"]}
        assert ("/request/headers", "Cookie") in stripped
        assert ("/request/raw_headers", "X-Api-Key") in stripped
        assert line["redaction"]["policy"] == "af-privacy-1+browser-2"

    async def test_a_post_body_is_redacted_before_it_is_stored(self) -> None:
        session, recorder = _session()
        body = f"user=alice&password={SECRET}".encode()
        recorder.bodies["i1"] = b"ok"
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        deliver(session, _request_event("n1", method="POST", headers=form, hasPostData=True))
        await session.on_paused(
            paused(
                request_id="i1",
                network_id="n1",
                url=URL,
                method="POST",
                request_headers=form,
                post_data_entries=[body[:9], body[9:]],
            )
        )
        deliver(session, _response_event("n1"))
        deliver(session, _finished("n1"))
        files = await session.collect()
        request_body = _warc(files)[0][3]
        assert request_body == b"user=alice&password=<redacted>"
        (line,) = _index(files)
        assert line["request"]["post"]["length"] == len(request_body)
        assert line["request"]["post"]["source"] == "requestPaused.postDataEntries"
        assert any(e["field"] == "password" for e in line["redaction"]["entries"])

    async def test_get_request_post_data_is_the_last_source(self) -> None:
        session, recorder = _session()
        recorder.bodies["i1"] = b"ok"
        recorder.post_data["n1"] = b'{"q": 1}'
        deliver(session, _request_event("n1", method="POST", hasPostData=True))
        await session.on_paused(
            paused(request_id="i1", network_id="n1", url=URL, method="POST", has_post_data=True)
        )
        deliver(session, _response_event("n1"))
        deliver(session, _finished("n1"))
        (line,) = _index(await session.collect())
        assert line["request"]["post"]["source"] == "getRequestPostData"


class TestBodies:
    def test_the_pause_budget_is_the_archivers_own_reads(self) -> None:
        """The recorder waits for the owner as long as its knobs say (plus a grace).

        A POST body read and a stream that does not start, then the body read inline.
        """
        session, _ = _session()
        assert session.pause_budget_s == 2 * 1.0 + 10.0
        slow, _ = _session(body_read_timeout_s=30.0, command_timeout_s=2.0)
        assert slow.pause_budget_s == 34.0

    async def test_encoding_headers_describe_the_wire_not_the_stored_body(self) -> None:
        session, recorder = _session()
        await _one_exchange(
            session,
            recorder,
            body=b"decoded body",
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": "5",
                "Content-Type": "text/plain",
            },
        )
        files = await session.collect()
        http = _warc(files)[1][2]
        assert http.get_header("Content-Encoding") is None
        assert http.get_header("Content-Length") == str(len(b"decoded body"))
        (line,) = _index(files)
        assert line["original_encoding"] == {"content-encoding": "gzip", "content-length": "5"}

    @pytest.mark.parametrize(
        "collect_between", [False, True], ids=["same-batch", "separate-batches"]
    )
    async def test_repeated_payloads_revisit_only_within_their_collection(
        self,
        collect_between: bool,
    ) -> None:
        session, recorder = _session()
        first = None
        for n in (1, 2):
            recorder.bodies[f"i{n}"] = b"same bytes"
            deliver(session, _request_event(f"n{n}", f"http://shop.test/{n}"))
            await session.on_paused(
                paused(request_id=f"i{n}", network_id=f"n{n}", url=f"http://shop.test/{n}")
            )
            deliver(session, _response_event(f"n{n}"))
            deliver(session, _finished(f"n{n}"))
            if collect_between and n == 1:
                first = _index(await session.collect())[0]
        files = await session.collect()
        if collect_between:
            (second,) = _index(files)
        else:
            first, second = _index(files)
        assert first is not None
        rec_type, headers, _http, payload = _warc(files)[-1]
        if collect_between:
            assert second["revisit_of"] is None
            assert second["response_record_id"] != first["response_record_id"]
            assert rec_type == "response" and payload == b"same bytes"
            assert "WARC-Refers-To" not in headers
        else:
            assert second["revisit_of"] == first["response_record_id"]
            assert rec_type == "revisit" and payload == b""
            assert headers["WARC-Refers-To"] == f"<{first['response_record_id']}>"
            assert headers["WARC-Profile"].endswith("identical-payload-digest")

    async def test_304_is_recorded_as_it_is(self) -> None:
        session, recorder = _session()
        recorder.failures["Fetch.getResponseBody"] = RuntimeError("CDP error: No data found")
        await _one_exchange(session, recorder, status=304)
        (line,) = _index(await session.collect())
        assert (line["status"], line["body_status"], line["payload_length"]) == (304, "ok", 0)

    async def test_206_keeps_its_content_range(self) -> None:
        session, recorder = _session(max_inline_body_bytes=10)
        await _one_exchange(
            session,
            recorder,
            status=206,
            body=b"0123",
            resource_type="Media",
            headers={"Content-Range": "bytes 0-3/300", "Content-Length": "4"},
        )
        (line,) = _index(await session.collect())
        assert line["content_range"] == {"unit": "bytes", "start": 0, "end": 3, "total": 300}
        assert line["body_status"] == "ok"
        assert "Network.streamResourceContent" not in recorder.methods()

    async def test_a_body_read_that_times_out_is_incomplete(self) -> None:
        session, recorder = _session(body_read_timeout_s=0.05)
        recorder.holds["Fetch.getResponseBody"] = asyncio.Event()
        await _one_exchange(session, recorder)
        (line,) = _index(await session.collect())
        assert line["body_status"] == "incomplete"
        assert "did not answer" in line["body_note"]

    async def test_without_a_pause_the_body_is_incomplete_unless_empty_by_protocol(self) -> None:
        session, _ = _session()
        deliver(session, _request_event("n1", "http://shop.test/nested.js"))
        deliver(session, _response_event("n1"))
        deliver(session, _finished("n1"))
        deliver(session, _request_event("n2", "http://shop.test/beacon", method="POST"))
        deliver(session, _response_event("n2", 204))
        deliver(session, _finished("n2"))
        lines = {line["url"]: line for line in _index(await session.collect())}
        assert lines["http://shop.test/nested.js"]["body_status"] == "incomplete"
        assert lines["http://shop.test/beacon"]["body_status"] == "ok"

    async def test_a_detached_target_fails_what_it_was_loading(self) -> None:
        session, recorder = _session()
        recorder.add_target("worker-s", "W", "worker")
        deliver(session, _request_event("n1", session_id="worker-s"))
        deliver(session, event("Target.detachedFromTarget", {"sessionId": "worker-s"}, "worker-s"))
        (line,) = _index(await session.collect())
        assert line["body_status"] == "incomplete"
        assert "detached" in line["error_text"]


def _kinds(lines: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return [(x["kind"], x.get("body_status") or x.get("waiting_for")) for x in lines]


async def _start_streamed(
    session: NetworkSession,
    recorder: ScriptedRecorder,
    body_length: int,
    *,
    resource_type: str = "XHR",
    method: str = "GET",
    status: int = 200,
    headers: dict[str, str] | None = None,
    network_session: str = "page-s",
) -> None:
    """A request over the inline tier, paused and released: its body has not arrived yet."""
    declared = {"Content-Length": str(body_length), "Content-Type": "application/json"}
    deliver(session, _request_event("n1", method=method, session_id=network_session))
    await session.on_paused(
        paused(
            request_id="i1",
            network_id="n1",
            url=URL,
            method=method,
            status=status,
            resource_type=resource_type,
            headers={**declared, **(headers or {})},
        )
    )
    deliver(session, _response_event("n1", status, session_id=network_session))


class TestStreamedBodies:
    """Over the inline tier and up to 64 MiB, a body streams while the page reads it (§4.14)."""

    async def test_it_streams_whatever_its_type_and_lands_as_a_blob(self) -> None:
        session, recorder = _session(max_inline_body_bytes=8)
        big = b'{"rows": "' + b"x" * 3000 + b'"}'
        await _start_streamed(session, recorder, len(big), method="POST")
        stream_chunks(session, "n1", big)
        deliver(session, _finished("n1"))
        files = await session.collect()
        assert "Fetch.getResponseBody" not in recorder.methods(), "never held for an inline read"
        assert [(m, p, sid) for m, p, sid in recorder.sent if m.startswith("Network.stream")] == [
            ("Network.streamResourceContent", {"requestId": "n1"}, "page-s")
        ]
        lines = _index(files)
        assert _kinds(lines) == [("exchange", "streamed_later"), ("body", "ok")]
        exchange, body = lines
        assert exchange["response_record_id"] is None, "no response record with a partial body"
        assert exchange["method"] == "POST" and "streamed" in exchange["body_note"]
        assert body["record_id"] == exchange["record_id"] and body["payload_length"] == len(big)
        (blob,) = [item for item in files if item.content_addressed]
        assert blob.data == big

    async def test_what_arrived_before_streaming_comes_first(self) -> None:
        session, recorder = _session(max_inline_body_bytes=8)
        recorder.buffered["n1"] = b"head-"
        await _start_streamed(session, recorder, 15)
        stream_chunks(session, "n1", b"0123456789")
        deliver(session, _finished("n1"))
        (blob,) = [item for item in await session.collect() if item.content_addressed]
        assert blob.data == b"head-0123456789"

    async def test_a_body_still_streaming_is_pending_until_its_body_line(self) -> None:
        """A record never ends on a line that promises a body nobody wrote; the drain never waits."""
        session, recorder = _session(max_inline_body_bytes=8, drain_timeout_s=5.0)
        big = b"x" * 3000
        await _start_streamed(session, recorder, len(big))
        started = asyncio.get_running_loop().time()
        first = _index(await session.collect())
        assert asyncio.get_running_loop().time() - started < 1.0, "a stream is never drained"
        assert _kinds(first) == [("exchange", "streamed_later"), ("pending", "streamed_body")]
        assert first[1]["record_id"] == first[0]["record_id"]
        stream_chunks(session, "n1", big[:1000])
        assert _kinds(_index(await session.collect())) == [("pending", "streamed_body")]
        stream_chunks(session, "n1", big[1000:])
        deliver(session, _finished("n1"))
        last = _index(await session.collect())
        assert _kinds(last) == [("body", "ok")]
        assert last[0]["record_id"] == first[0]["record_id"]
        assert _index(await session.collect()) == []

    async def test_a_reader_cancelled_after_the_last_byte_is_complete(self) -> None:
        """Every declared byte arrived: ``loadingFailed`` (a cancelled reader, measured) changes nothing."""
        session, recorder = _session(max_inline_body_bytes=8)
        await _start_streamed(session, recorder, 2000)
        stream_chunks(session, "n1", b"r" * 2000)
        deliver(
            session,
            event(
                "Network.loadingFailed",
                {"requestId": "n1", "errorText": "net::ERR_ABORTED", "canceled": True},
            ),
        )
        lines = _index(await session.collect())
        assert _kinds(lines)[-1] == ("body", "ok")

    async def test_a_stream_cut_short_is_incomplete(self) -> None:
        session, recorder = _session(max_inline_body_bytes=8)
        await _start_streamed(session, recorder, 2000)
        stream_chunks(session, "n1", b"r" * 500)
        deliver(
            session,
            event("Network.loadingFailed", {"requestId": "n1", "errorText": "net::ERR_FAILED"}),
        )
        files = await session.collect()
        body = _index(files)[-1]
        assert (body["kind"], body["body_status"]) == ("body", "incomplete")
        assert "500 of 2000" in body["note"] and "net::ERR_FAILED" in body["note"]
        assert not [item for item in files if item.content_addressed]

    async def test_a_compressed_body_completes_when_the_load_finishes(self) -> None:
        """Declared is the wire length; the decoded chunks are longer, and the finish says done."""
        session, recorder = _session(max_inline_body_bytes=8)
        decoded = b"text " * 1000
        await _start_streamed(session, recorder, 100, headers={"Content-Encoding": "gzip"})
        stream_chunks(session, "n1", decoded, encoded=60)
        deliver(session, _finished("n1"))
        files = await session.collect()
        assert _index(files)[-1]["body_status"] == "ok"
        (blob,) = [item for item in files if item.content_addressed]
        assert blob.data == decoded

    async def test_a_compressed_body_whose_load_failed_short_of_its_wire_bytes_is_incomplete(
        self,
    ) -> None:
        session, recorder = _session(max_inline_body_bytes=8)
        await _start_streamed(session, recorder, 100, headers={"Content-Encoding": "br"})
        stream_chunks(session, "n1", b"text " * 100, encoded=60)
        deliver(
            session,
            event("Network.loadingFailed", {"requestId": "n1", "errorText": "net::ERR_ABORTED"}),
        )
        assert _index(await session.collect())[-1]["body_status"] == "incomplete"

    async def test_a_detached_target_ends_its_stream_incomplete(self) -> None:
        session, recorder = _session(max_inline_body_bytes=8)
        await _start_streamed(session, recorder, 2000)
        stream_chunks(session, "n1", b"r" * 100)
        deliver(session, event("Target.detachedFromTarget", {"sessionId": "page-s"}, "page-s"))
        body = _index(await session.collect())[-1]
        assert (body["kind"], body["body_status"]) == ("body", "incomplete")
        assert "detached" in body["note"]

    async def test_a_worker_request_streams_on_the_worker_session(self) -> None:
        """Paused on the page's session, reported on the worker's: the chunks come there."""
        session, recorder = _session(max_inline_body_bytes=8)
        recorder.add_target("worker-s", "W", "worker")
        await _start_streamed(session, recorder, 1000, network_session="worker-s")
        assert ("Network.streamResourceContent", {"requestId": "n1"}, "worker-s") in recorder.sent
        stream_chunks(session, "n1", b"p" * 1000, session_id="page-s")  # not the streaming session
        stream_chunks(session, "n1", b"w" * 1000, session_id="worker-s")
        deliver(session, _finished("n1", session_id="worker-s"))
        files = await session.collect()
        (blob,) = [item for item in files if item.content_addressed]
        assert blob.data == b"w" * 1000

    async def test_chunks_never_enter_the_archive_scope(self) -> None:
        """Admission keeps a chunk (or drops it, for a request nothing streams) and admits nothing."""
        session, recorder = _session(max_inline_body_bytes=2)
        assert "Network.dataReceived" in session.event_methods()
        unrelated = {"requestId": "other", "dataLength": 3, "encodedDataLength": 3, "data": "YWJj"}
        assert session.admit_event("Network.dataReceived", unrelated, "page-s") is None
        await _start_streamed(session, recorder, 3)
        streamed = {**unrelated, "requestId": "n1"}
        assert session.admit_event("Network.dataReceived", streamed, "page-s") is None
        deliver(session, _finished("n1"))
        (blob,) = [item for item in await session.collect() if item.content_addressed]
        assert blob.data == b"abc"

    async def test_streaming_that_cannot_start_reads_the_body_inline(self) -> None:
        session, recorder = _session(max_inline_body_bytes=8)
        recorder.failures["Network.streamResourceContent"] = RuntimeError(
            "CDP error: Request with the provided ID does not exists"
        )
        await _one_exchange(session, recorder, body=b"y" * 2000, headers={"Content-Length": "2000"})
        (line,) = _index(await session.collect())
        assert (line["body_status"], line["payload_length"]) == ("ok", 2000)
        assert "streaming could not start" in line["body_note"]
        assert "Fetch.getResponseBody" in recorder.methods()

    async def test_a_large_document_is_read_inline(self) -> None:
        """A navigation's body streams only after it commits, which the pause holds back."""
        session, recorder = _session(max_inline_body_bytes=8)
        await _one_exchange(
            session,
            recorder,
            body=b"<html>" + b"d" * 2000,
            resource_type="Document",
            headers={"Content-Length": "2006", "Content-Type": "text/html"},
        )
        (line,) = _index(await session.collect())
        assert (line["body_status"], line["payload_length"]) == ("ok", 2006)
        assert "Network.streamResourceContent" not in recorder.methods()

    async def test_a_stream_over_its_declared_tier_but_empty_by_protocol_is_not_streamed(
        self,
    ) -> None:
        """A HEAD declares the length of a body it does not carry."""
        session, recorder = _session(max_inline_body_bytes=8)
        recorder.failures["Fetch.getResponseBody"] = RuntimeError("CDP error: No data found")
        await _one_exchange(session, recorder, method="HEAD", headers={"Content-Length": "9000"})
        (line,) = _index(await session.collect())
        assert (line["body_status"], line["payload_length"]) == ("ok", 0)
        assert "Network.streamResourceContent" not in recorder.methods()


class TestTheSizeLimit:
    """Nothing over 64 MiB is stored, however the size is learned (the replay's limit)."""

    async def test_a_body_declared_over_64_mib_is_never_read(self) -> None:
        session, recorder = _session()
        await _one_exchange(session, recorder, headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
        (line,) = _index(await session.collect())
        assert line["body_status"] == "too_large"
        assert "64 MiB" in line["body_note"]
        assert not {"Fetch.getResponseBody", "Network.streamResourceContent"} & set(
            recorder.methods()
        )

    async def test_a_stream_that_grows_past_the_limit_is_too_large(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(network_module, "MAX_BODY_BYTES", 1000)
        session, recorder = _session(max_inline_body_bytes=8)
        await _start_streamed(session, recorder, 100, headers={"Content-Encoding": "gzip"})
        stream_chunks(session, "n1", b"z" * 1500, encoded=100)
        deliver(session, _finished("n1"))
        files = await session.collect()
        body = _index(files)[-1]
        assert (body["kind"], body["body_status"]) == ("body", "too_large")
        assert not [item for item in files if item.content_addressed]

    async def test_a_body_read_inline_that_decodes_past_the_limit_is_too_large(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No declared length: read inline, and kept only if it fits."""
        monkeypatch.setattr(network_module, "MAX_BODY_BYTES", 1000)
        session, recorder = _session()
        await _one_exchange(
            session, recorder, body=b"q" * 1500, headers={"Content-Type": "text/plain"}
        )
        (line,) = _index(await session.collect())
        assert (line["body_status"], line["payload_length"]) == ("too_large", None)
        assert line["response_record_id"] is None

    def test_the_limit_is_the_replays_default_fulfil_limit(self) -> None:
        """Everything a default replay can serve is recorded — and nothing it cannot."""
        assert MAX_BODY_BYTES == DEFAULT_FULFILL_BODY_BYTES == 64 * 1024 * 1024


class TestWebSocket:
    async def test_the_handshake_is_an_exchange_and_frames_go_to_their_own_file(self) -> None:
        session, _ = _session()
        ws = "ws://shop.test/echo"
        deliver(session, event("Network.webSocketCreated", {"requestId": "w1", "url": ws}))
        deliver(
            session,
            event(
                "Network.webSocketWillSendHandshakeRequest",
                {
                    "requestId": "w1",
                    "timestamp": 5.0,
                    "wallTime": 1_700_000_005.0,
                    "request": {"headers": {"Cookie": SECRET, "Upgrade": "websocket"}},
                },
            ),
        )
        deliver(
            session,
            event(
                "Network.webSocketHandshakeResponseReceived",
                {
                    "requestId": "w1",
                    "response": {
                        "status": 101,
                        "statusText": "Switching",
                        "headers": {"Upgrade": "websocket"},
                    },
                },
            ),
        )
        deliver(
            session,
            event(
                "Network.webSocketFrameSent",
                {
                    "requestId": "w1",
                    "timestamp": 6.0,
                    "response": {"opcode": 1, "mask": True, "payloadData": "hello"},
                },
            ),
        )
        deliver(
            session,
            event(
                "Network.webSocketFrameReceived",
                {
                    "requestId": "w1",
                    "timestamp": 6.5,
                    "response": {"opcode": 2, "mask": False, "payloadData": "AAE="},
                },
            ),
        )
        files = await session.collect()
        (line,) = _index(files)
        assert (line["status"], line["body_status"], line["url"]) == (101, "websocket", ws)
        frames = [json.loads(x) for x in _files(files)[WEBSOCKET_NAME].data.decode().splitlines()]
        assert [(f["direction"], f.get("payload"), f.get("payload_base64")) for f in frames] == [
            ("sent", "hello", None),
            ("received", None, "AAE="),
        ]
        assert frames[0]["time"] == pytest.approx(1_700_000_006.0)
        assert SECRET.encode() not in b"".join(item.data for item in files)
        deliver(session, event("Network.webSocketClosed", {"requestId": "w1", "timestamp": 7.0}))
        later = await session.collect()
        assert [
            json.loads(x)["direction"]
            for x in _files(later)[WEBSOCKET_NAME].data.decode().splitlines()
        ] == ["closed"]
        assert _index(later) == []


class TestTheIndex:
    async def test_no_match_key_only_raw_request_facts(self) -> None:
        """Replay derives its keys from raw facts with one canonicaliser (M30)."""
        session, recorder = _session()
        await _one_exchange(session, recorder)
        (line,) = _index(await session.collect())
        assert not [key for key in line if "key" in key or "canonical" in key]
        assert {"method", "url", "request", "payload_sha256", "body_status"} <= set(line)

    async def test_offsets_point_at_the_records(self) -> None:
        session, recorder = _session()
        await _one_exchange(session, recorder)
        files = await session.collect()
        (line,) = _index(files)
        data = _files(files)[WARC_NAME].data
        for part in ("request", "response"):
            where = line["warc"][part]
            record = next(
                ArchiveIterator(
                    io.BytesIO(data[where["offset"] : where["offset"] + where["length"]])
                )
            )
            assert record.rec_type == part
            assert (
                record.rec_headers.get_header("WARC-Record-ID").strip("<>")
                == line[f"{part}_record_id"]
            )

    async def test_the_request_and_response_are_concurrent(self) -> None:
        session, recorder = _session()
        await _one_exchange(session, recorder)
        files = await session.collect()
        (request, response) = _warc(files)[:2]
        assert request[1]["WARC-Concurrent-To"] == response[1]["WARC-Record-ID"]
        assert request[1]["WARC-Target-URI"] == URL

    async def test_nothing_captured_hands_over_nothing(self) -> None:
        session, _ = _session()
        assert await session.collect() == []


class TestRequestTimes:
    """When a request started and ended: evidence of how long a source request lived (§4.14).

    ``_request_event`` sends ``wallTime`` 1_700_000_000 at ``timestamp`` 10,
    so a CDP timestamp ``t`` is ``1_700_000_000 + (t - 10)`` on the wall clock.
    """

    WALL = 1_700_000_000.0

    async def test_a_pending_line_carries_the_start(self) -> None:
        session, _ = _session()
        deliver(session, _request_event("n1"))
        (line,) = _index(await session.collect())
        assert (line["kind"], line["started_at"]) == ("pending", self.WALL)

    async def test_a_finished_exchange_carries_its_start_and_end(self) -> None:
        session, recorder = _session()
        await _one_exchange(session, recorder)
        (line,) = _index(await session.collect())
        assert (line["started_at"], line["ended_at"]) == (self.WALL, self.WALL + 1.0)

    async def test_a_failed_request_ends_when_it_failed(self) -> None:
        session, _ = _session()
        deliver(session, _request_event("n1"))
        deliver(
            session,
            event(
                "Network.loadingFailed",
                {"requestId": "n1", "timestamp": 10.25, "errorText": "net::ERR_ABORTED"},
            ),
        )
        (line,) = _index(await session.collect())
        assert (line["ended_at"], line["error_text"], line["status"]) == (
            self.WALL + 0.25,
            "net::ERR_ABORTED",
            None,
        )

    async def test_a_redirect_hop_ends_when_the_next_hop_starts(self) -> None:
        session, _ = _session()
        deliver(session, _request_event("n1", "http://shop.test/old"))
        redirect = {"status": 302, "statusText": "Found", "headers": {"Location": URL}}
        deliver(session, _request_event("n1", URL, redirect=redirect))
        lines = _index(await session.collect())
        hop = next(line for line in lines if line["url"] == "http://shop.test/old")
        assert hop["ended_at"] == self.WALL  # the redirect event's timestamp (10)

    async def test_a_detached_target_ends_its_requests_at_the_detach(self) -> None:
        session, recorder = _session()
        recorder.add_target("worker-s", "W", "worker")
        deliver(session, _request_event("n1", session_id="worker-s"))
        deliver(session, event("Target.detachedFromTarget", {"sessionId": "worker-s"}, "worker-s"))
        (line,) = _index(await session.collect())
        assert line["ended_at"] is not None and line["error_text"] == TARGET_DETACHED_ERROR

    async def test_an_exchange_written_before_its_request_ended_has_no_end(self) -> None:
        """A body read at the Response stage settles the exchange; the request may not have ended."""
        session, recorder = _session()
        recorder.bodies["i1"] = b"hello"
        deliver(session, _request_event("n1"))
        await session.on_paused(paused(request_id="i1", network_id="n1", url=URL))
        deliver(session, _response_event("n1"))
        (line,) = _index(await session.collect())
        assert (line["body_status"], line["started_at"], line["ended_at"]) == (
            "ok",
            self.WALL,
            None,
        )

    async def test_a_streamed_body_line_carries_its_requests_end(self) -> None:
        """The exchange line went out first; how the request ended is on its body line."""
        session, recorder = _session(max_inline_body_bytes=8)
        await _start_streamed(session, recorder, 2000)
        first = _index(await session.collect())
        assert first[0]["ended_at"] is None
        stream_chunks(session, "n1", b"r" * 500)
        deliver(
            session,
            event(
                "Network.loadingFailed",
                {"requestId": "n1", "timestamp": 12.0, "errorText": "net::ERR_ABORTED"},
            ),
        )
        (body,) = _index(await session.collect())
        assert (body["kind"], body["body_status"], body["ended_at"], body["error_text"]) == (
            "body",
            "incomplete",
            self.WALL + 2.0,
            "net::ERR_ABORTED",
        )

    async def test_a_streamed_body_that_finished_has_no_error(self) -> None:
        session, recorder = _session(max_inline_body_bytes=8)
        await _start_streamed(session, recorder, 2000)
        stream_chunks(session, "n1", b"r" * 2000)
        deliver(session, _finished("n1"))
        body = _index(await session.collect())[-1]
        assert (body["body_status"], body["ended_at"], body["error_text"]) == (
            "ok",
            self.WALL + 1.0,
            None,
        )


class TestContentRange:
    def test_content_range_parsing(self) -> None:
        assert parse_content_range("bytes */300") == {
            "unit": "bytes",
            "start": None,
            "end": None,
            "total": 300,
        }
        assert (parse_content_range("bytes 0-1/*") or {})["total"] is None
        assert parse_content_range("garbage") == {"raw": "garbage"}
        assert parse_content_range(None) is None


class TestTheArchiver:
    def test_knobs_and_declarations(self) -> None:
        archiver = NetworkArchiver()
        assert archiver.archiver_id == "browser.network"
        assert archiver.needs == frozenset({"fetch_response"}), "live traffic, nothing else"
        assert archiver.requires_replayable is True
        assert NetworkArchiver(require_replayable=False).requires_replayable is False
        assert "contains_session_state" in archiver.privacy_markers
        assert archiver.redaction_facts() == {
            "stripped_request_headers": ["authorization", "cookie", "proxy-authorization"]
        }

    def test_every_prepared_target_gets_network_enable_and_a_response_pattern(self) -> None:
        session, _ = _session(max_post_data_size=1234)
        target = RecorderTarget(session_id="s", target_id="t", type="service_worker")
        (command,) = session.target_commands(target)
        assert (command.method, dict(command.params)) == (
            "Network.enable",
            {"maxPostDataSize": 1234},
        )
        assert session.fetch_patterns(target) == ({"urlPattern": "*", "requestStage": "Response"},)

    async def test_a_pause_without_a_network_id_is_not_recorded(self) -> None:
        """Traffic the inspector does not know: nothing to pair; the recorder releases it as is."""
        session, recorder = _session()
        await session.on_paused(paused(request_id="i9", network_id=None, url=URL))
        assert recorder.methods() == [] and await session.collect() == []
