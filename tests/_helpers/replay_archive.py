"""Recorded network archives for replay tests — M29's own session, committed by the real WAL.

:class:`RecordingScript` drives one real ``browser.network`` session
(:class:`~spectragent.core.capabilities.browser.archive.network.NetworkSession`)
the way the recorder would — ``Network.*`` events through the session's own
admission, Response-stage ``Fetch.requestPaused`` pauses through
``on_paused`` — over :class:`~tests._helpers.archive_recorder.ScriptedRecorder`.
Each :meth:`RecordingScript.commit` collects the session like a commit point
and commits the batch with ``WALWriter`` through ``encode_archive_file`` (what
``TraceWriter`` commits with), plus the environment fingerprint built by the
real ``BrowserFingerprint``. So the ``index.jsonl`` lines and WARC bytes a
replay test loads are exactly what a recording writes: nothing here spells an
index field, a WARC header or a trace path.

:func:`indexed` builds an :class:`IndexedRecord` in memory for matcher tests
that need no bytes (two-run affinity, tie-breaks), keyed by the same
canonicaliser the loader uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from spectragent.core.archive import ArchiveFile
from spectragent.core.capabilities.browser.archive import NetworkArchiver
from spectragent.core.capabilities.browser.archive.encoding import canonical_json
from spectragent.core.capabilities.browser.archive.fingerprint import BrowserFingerprint
from spectragent.core.capabilities.browser.archive.network import MAX_BODY_BYTES, NetworkSession
from spectragent.core.capabilities.browser.backends.browser_defaults import BrowserDefaults
from spectragent.core.capabilities.browser.color_scheme import ColorScheme
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.capabilities.browser.recorder import RecorderTarget
from spectragent.core.capabilities.browser.replay import PinnedReplayability
from spectragent.core.capabilities.browser.replay.archive_index import IndexedRecord
from spectragent.core.capabilities.browser.replay.canonical import request_keys
from spectragent.core.capabilities.browser.replay.site_rules import SiteRule
from spectragent.core.capabilities.browser.storage_seed import StorageSeed
from spectragent.core.trace.encoding import EncodedFragment, encode_archive_file
from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.browser_storage import BrowserStorageState
from spectragent.schemas.step_record import StepRecord
from tests._helpers.archive_recorder import (
    ScriptedRecorder,
    deliver,
    event,
    open_session,
    paused,
    stream_chunks,
)
from tests._helpers.trace_builder import agent_result, new_wal, step_record

#: The recording's T0 (epoch milliseconds) the fingerprint carries.
RECORDED_T0_MS = 1_760_000_000_000
#: The browser defaults the recording measured: what ``FakeBrowser`` and the fake providers answer.
RECORDED_DEFAULTS = BrowserDefaults(timezone="UTC", locale="en-US")
#: The inline body tier of the scripted archiver: anything declared larger is not read inline.
INLINE_LIMIT = 4096
PAGE_SESSION = "page-s"
#: How much longer than ``late_body_timeout_s`` a script waits for a read that never
#: answers to be given up: the event loop's own scheduling slack on a loaded machine.
LATE_READ_MARGIN_S = 0.05


class RecordingScript:
    """One recorded run, exchange by exchange and commit by commit."""

    def __init__(
        self,
        base: Path,
        run_id: str = "run-recorded",
        *,
        fingerprint: bool = True,
        t0_ms: int = RECORDED_T0_MS,
        launch_flags: list[str] | None = None,
        replayability: Any = None,
        browser_defaults: BrowserDefaults | None = RECORDED_DEFAULTS,
        color_scheme: ColorScheme | None = "light",
        input_platform: InputPlatform | None = "macos",
        config: TraceConfig | None = None,
        **archiver_knobs: Any,
    ) -> None:
        knobs: dict[str, Any] = {
            "require_replayable": False,
            "drain_timeout_s": 0.2,
            "max_inline_body_bytes": INLINE_LIMIT,
            **archiver_knobs,
        }
        self.archiver = NetworkArchiver(**knobs)
        session, recorder = open_session(self.archiver)
        assert isinstance(session, NetworkSession)
        self.session: NetworkSession = session
        self.recorder: ScriptedRecorder = recorder
        self.page: RecorderTarget = recorder.add_target(PAGE_SESSION, "PAGE")
        if config is None:
            self.wal: WALWriter = new_wal(base, run_id)
        else:  # a run whose configuration a test reads (the flywheel metrics)
            self.wal = WALWriter(base, run_id)
            self.wal.init_run(config)
        self._fingerprint = BrowserFingerprint() if fingerprint else None
        #: The recording's tier (``pinned``, seed 7, unless given) and launch flags.
        self.replayability = replayability or PinnedReplayability(random_seed=7)
        self.launch_flags = list(launch_flags) if launch_flags is not None else ["--headless=new"]
        #: What the recording's browser fell back to (``None``: a recording that measured none).
        self.browser_defaults = browser_defaults
        self.input_platform: InputPlatform | None = input_platform
        self._plan = self.replayability.plan(
            t0_ms=t0_ms, browser_user_agent="ScriptedChrome/1.0", color_scheme=color_scheme
        )
        self._n = 0
        self._wall = 1_700_000_000.0
        self._run_version = 0
        self._trailing = 0

    @property
    def run_dir(self) -> Path:
        return self.wal.run_dir

    # ── one exchange ──────────────────────────────────────────────────

    def _ids(self) -> tuple[str, str]:
        self._n += 1
        return f"net-{self._n}", f"int-{self._n}"

    def _request(
        self,
        network_id: str,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str],
        post: bytes | None,
        resource_type: str,
        redirect: dict[str, Any] | None = None,
    ) -> None:
        self._wall += 1.0
        request: dict[str, Any] = {"url": url, "method": method, "headers": dict(headers)}
        if post is not None:
            request["hasPostData"] = True
        params: dict[str, Any] = {
            "requestId": network_id,
            "loaderId": "L1",
            "documentURL": "http://shop.test/",
            "request": request,
            "timestamp": self._wall - 1_600_000_000.0,
            "wallTime": self._wall,
            "type": resource_type,
            "frameId": "frame-1",
            "initiator": {"type": "script"},
        }
        if redirect is not None:
            params["redirectResponse"] = redirect
            params["redirectHasExtraInfo"] = False
        deliver(self.session, event("Network.requestWillBeSent", params, PAGE_SESSION))

    def _response(
        self, network_id: str, url: str, status: int, headers: Mapping[str, str], resource_type: str
    ) -> None:
        deliver(
            self.session,
            event(
                "Network.responseReceived",
                {
                    "requestId": network_id,
                    "type": resource_type,
                    "frameId": "frame-1",
                    "hasExtraInfo": False,
                    "response": {
                        "url": url,
                        "status": status,
                        "statusText": "",
                        "headers": dict(headers),
                        "mimeType": headers.get("Content-Type", "text/plain"),
                        "protocol": "http/1.1",
                    },
                },
                PAGE_SESSION,
            ),
        )

    def _stamp(self) -> float:
        """A CDP timestamp a quarter second after the latest request (``wallTime`` minus 1.6e9)."""
        return self._wall - 1_600_000_000.0 + 0.25

    def _finished(self, network_id: str) -> None:
        deliver(
            self.session,
            event(
                "Network.loadingFinished",
                {"requestId": network_id, "timestamp": self._stamp(), "encodedDataLength": 10},
                PAGE_SESSION,
            ),
        )

    def _failed(self, network_id: str, error_text: str) -> None:
        deliver(
            self.session,
            event(
                "Network.loadingFailed",
                {"requestId": network_id, "timestamp": self._stamp(), "errorText": error_text},
                PAGE_SESSION,
            ),
        )

    async def exchange(
        self,
        url: str,
        *,
        method: str = "GET",
        status: int = 200,
        body: bytes = b"ok",
        headers: Mapping[str, str] | None = None,
        request_headers: Mapping[str, str] | None = None,
        post: bytes | None = None,
        resource_type: str = "XHR",
    ) -> str:
        """One request answered and finished; its network request id."""
        network_id, interception = self._ids()
        response_headers = {"Content-Type": "text/plain", **(headers or {})}
        sent = {"Accept": "*/*", **(request_headers or {})}
        self.recorder.bodies[interception] = body
        self._request(
            network_id, url, method=method, headers=sent, post=post, resource_type=resource_type
        )
        await self.session.on_paused(
            paused(
                request_id=interception,
                network_id=network_id,
                url=url,
                status=status,
                headers=response_headers,
                method=method,
                resource_type=resource_type,
                request_headers=sent,
                post_data_entries=[post] if post else None,
                target=self.page,
            )
        )
        self._response(network_id, url, status, response_headers, resource_type)
        self._finished(network_id)
        return network_id

    async def redirect(
        self, url: str, location: str, *, status: int = 302, body: bytes = b"landed"
    ) -> str:
        """A redirect hop and the page it lands on (one network request id, two records)."""
        network_id, first = self._ids()
        _, second = self._ids()
        self._request(
            network_id,
            url,
            method="GET",
            headers={"Accept": "*/*"},
            post=None,
            resource_type="Document",
        )
        await self.session.on_paused(
            paused(
                request_id=first,
                network_id=network_id,
                url=url,
                status=status,
                headers={"Location": location},
                resource_type="Document",
                target=self.page,
            )
        )
        self._request(
            network_id,
            location,
            method="GET",
            headers={"Accept": "*/*"},
            post=None,
            resource_type="Document",
            redirect={"status": status, "statusText": "Found", "headers": {"Location": location}},
        )
        self.recorder.bodies[second] = body
        await self.session.on_paused(
            paused(
                request_id=second,
                network_id=network_id,
                url=location,
                headers={"Content-Type": "text/html"},
                resource_type="Document",
                target=self.page,
            )
        )
        self._response(network_id, location, 200, {"Content-Type": "text/html"}, "Document")
        self._finished(network_id)
        return network_id

    async def too_large(self, url: str, *, declared: int = MAX_BODY_BYTES + 1) -> str:
        """A response declaring more than the 64 MiB limit: recorded without a body, never read."""
        return await self.exchange(url, headers={"Content-Length": str(declared)}, body=b"")

    async def streamed_later(
        self, url: str, body: bytes, *, hold: bool = False
    ) -> Callable[[], None] | None:
        """A body over the inline tier, streamed while the page reads it; *hold* keeps it back.

        The response is released at once; the body arrives in
        ``Network.dataReceived`` chunks and the request ends. With *hold* the
        chunks and the end wait until the returned callable is called — so
        the exchange line is committed first and its body line in a later
        commit.
        """
        assert len(body) > INLINE_LIMIT
        network_id, interception = self._ids()
        headers = {"Content-Length": str(len(body)), "Content-Type": "text/javascript"}
        sent = {"Accept": "*/*"}
        self._request(
            network_id, url, method="GET", headers=sent, post=None, resource_type="Script"
        )
        await self.session.on_paused(
            paused(
                request_id=interception,
                network_id=network_id,
                url=url,
                headers=headers,
                resource_type="Script",
                request_headers=sent,
                target=self.page,
            )
        )
        self._response(network_id, url, 200, headers, "Script")

        def finish() -> None:
            stream_chunks(self.session, network_id, body, session_id=PAGE_SESSION)
            self._finished(network_id)

        if hold:
            return finish
        finish()
        return None

    def failed(
        self,
        url: str,
        *,
        error_text: str = "net::ERR_FAILED",
        status: int | None = None,
        request_headers: Mapping[str, str] | None = None,
        resource_type: str = "XHR",
    ) -> str:
        """A request that failed: an ``incomplete`` record.

        Without *status* it failed before any response (``net::ERR_ABORTED``:
        the page cancelled it before the server answered); with one, its
        response headers had arrived and the body never did. Never paused at
        the Response stage, so the recorder read no body.
        """
        network_id, _ = self._ids()
        sent = {"Accept": "*/*", **(request_headers or {})}
        self._request(
            network_id, url, method="GET", headers=sent, post=None, resource_type=resource_type
        )
        if status is not None:
            self._response(network_id, url, status, {"Content-Type": "text/plain"}, resource_type)
        self._failed(network_id, error_text)
        return network_id

    def from_memory_cache(self, url: str, *, resource_type: str = "Image") -> str:
        """A request the browser answered from its memory cache: never paused, so no body."""
        network_id, _ = self._ids()
        self._request(
            network_id,
            url,
            method="GET",
            headers={"Accept": "*/*"},
            post=None,
            resource_type=resource_type,
        )
        deliver(
            self.session,
            event("Network.requestServedFromCache", {"requestId": network_id}, PAGE_SESSION),
        )
        self._response(network_id, url, 200, {"Content-Type": "image/webp"}, resource_type)
        self._finished(network_id)
        return network_id

    async def body_read_times_out(self, url: str, **exchange: Any) -> str:
        """An exchange whose body read never answers: not while the response is held, nor after.

        Give the script short ``body_read_timeout_s`` and ``late_body_timeout_s``:
        the hold waits out the first, this helper the second.
        """
        self.recorder.holds["Fetch.getResponseBody"] = asyncio.Event()
        try:
            network_id = await self.exchange(url, **exchange)
            await asyncio.sleep(self.archiver.late_body_timeout_s + LATE_READ_MARGIN_S)
            return network_id
        finally:
            del self.recorder.holds["Fetch.getResponseBody"]

    async def streamed_cancelled(self, url: str, body: bytes) -> str:
        """A streamed body the page cancelled halfway: its ``body`` line is ``incomplete``."""
        finish = await self.streamed_later(url, body, hold=True)
        assert finish is not None
        network_id = f"net-{self._n}"
        stream_chunks(self.session, network_id, body[: len(body) // 2], session_id=PAGE_SESSION)
        self._failed(network_id, "net::ERR_ABORTED")
        return network_id

    def in_flight(self, url: str) -> str:
        """A request that never settles: every commit lists it ``pending``."""
        network_id, _ = self._ids()
        self._request(
            network_id, url, method="GET", headers={"Accept": "*/*"}, post=None, resource_type="XHR"
        )
        return network_id

    def websocket(self, url: str) -> str:
        """A WebSocket handshake answered 101: a ``websocket`` record."""
        network_id, _ = self._ids()
        deliver(
            self.session, event("Network.webSocketCreated", {"requestId": network_id, "url": url})
        )
        deliver(
            self.session,
            event(
                "Network.webSocketWillSendHandshakeRequest",
                {
                    "requestId": network_id,
                    "timestamp": 5.0,
                    "wallTime": self._wall,
                    "request": {"headers": {"Upgrade": "websocket"}},
                },
            ),
        )
        deliver(
            self.session,
            event(
                "Network.webSocketHandshakeResponseReceived",
                {
                    "requestId": network_id,
                    "response": {"status": 101, "statusText": "Switching", "headers": {}},
                },
            ),
        )
        return network_id

    # ── commits ───────────────────────────────────────────────────────

    async def _batch(self) -> list[ArchiveFile]:
        files = list(await self.session.collect())
        if self._fingerprint is not None:
            record = await self._fingerprint.collect(
                recorder=self.recorder,
                tabs=[self.page],
                plan_fingerprint=self._plan.fingerprint(),
                viewport=(1280, 720),
                session_metadata={"launch_flags": self.launch_flags},
                archivers=[self.archiver],
                fresh_profile=True,
                background_tabs="run",
                initial_storage=StorageSeed(
                    canonical_json(BrowserStorageState().model_dump(mode="json")), "fresh"
                ).fingerprint(),
                clock=lambda: self._wall,
                browser_defaults=self.browser_defaults,
                input_platform=self.input_platform,
            )
            if record is not None:
                files.append(record)
        return files

    def _encoded(self, files: list[ArchiveFile], **where: int) -> list[Any]:
        encoded = []
        for item in files:
            if item.scope == "run":
                encoded.append(encode_archive_file(item, run_version=self._run_version))
                self._run_version += 1
            else:
                encoded.append(encode_archive_file(item, **where))
        return encoded

    async def commit(
        self,
        step: int,
        record: StepRecord | None = None,
        fragments: Sequence[EncodedFragment] = (),
    ) -> list[ArchiveFile]:
        """Collect and commit as step *step* (with *record* and *fragments*, when given); the batch."""
        files = await self._batch()
        self.wal.commit_step(
            step,
            record if record is not None else step_record(step),
            fragments=list(fragments),
            extra_files=self._encoded(files, step=step),
        )
        return files

    async def commit_trailing(self) -> list[ArchiveFile]:
        files = await self._batch()
        self.wal.commit_trailing(extra_files=self._encoded(files, trailing=self._trailing))
        self._trailing += 1
        return files

    def finish(
        self,
        status: TraceStatus = TraceStatus.COMPLETE,
        *,
        result: AgentResult | None = None,
        incomplete_reason: str = "recording failed at step 1: ArchiveCollectionError: boom",
    ) -> Path:
        """Write ``result.json`` (*result*, or a ``done`` one) and close the trace with *status*."""
        self.wal.write_result(result if result is not None else agent_result())
        self.wal.finalize(
            status,
            incomplete_reason=incomplete_reason if status is TraceStatus.INCOMPLETE else None,
        )
        return self.run_dir


def indexed(
    url: str,
    *,
    record_id: str,
    order: int,
    run_id: str = "run-a",
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    status: int = 200,
    request_headers: Mapping[str, str] | None = None,
    rule: SiteRule | None = None,
    **fields: Any,
) -> IndexedRecord:
    """An in-memory record keyed by the loader's own canonicaliser (no bytes behind it)."""
    return IndexedRecord(
        record_id=record_id,
        run_id=run_id,
        order=order,
        method=method,
        url=url,
        request_headers=dict(request_headers or {}),
        body=body,
        content_type=content_type,
        keys=request_keys(method, url, body, content_type, rule),
        status=status,
        **fields,
    )
