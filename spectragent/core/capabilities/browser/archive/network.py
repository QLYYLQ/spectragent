"""``browser.network`` — every request, response and body the browser exchanged (§4.14).

The archiver is a recorder participant twice over:

- **Bodies at the Fetch Response stage.** Every frame and worker target asks
  for a Response-stage ``Fetch`` pause (the recorder merges the patterns into
  one ``Fetch.enable``), and :meth:`NetworkSession.on_paused` reads the body
  with ``Fetch.getResponseBody`` while Chrome holds the response — the
  recorder continues it in ``finally``, whatever happened here. Nothing
  depends on ``Network.getResponseBody``'s buffer (200 MB in total, 20 MB per
  body, first-in-first-out, dropped on a cross-process navigation). Tiered by
  the declared ``Content-Length``: up to ``max_inline_body_bytes`` read
  inline (bounded by ``body_read_timeout_s`` — at this stage the read
  includes the download); above it never read inline: a ``GET`` of a
  refetch-safe static type is fetched again after the response was
  released, with ``Network.loadNetworkResource`` + ``IO.read`` in ≤ 1 MiB
  chunks up to ``max_streamed_body_bytes`` (``streamed_later``; a ``body``
  index line points the record at a content-addressed blob later), anything
  else is ``too_large``. The tiers are this archiver's policy, not a
  transport limit: Chrome caps a CDP message it *reads* at 100 MiB, which
  binds the replay's fulfil, while a reply it sends may reach 256 MiB
  (``docs/architecture_design.md`` §4.14, "消息上限").
- **Metadata from the archive scope.** The session declares the ``Network.*``
  events it consumes; the recorder hands each one to
  :meth:`NetworkSession.admit_event` in its receive loop, which holds the
  bytes back (POST body, frame payload) in the session's side store and
  admits small metadata without the credential values
  (:func:`~.network_model.admit_network_event`). The admitted event reaches
  :meth:`NetworkSession.on_event` through the ArchiveWatchdog, gets its held
  bytes back, and is paired into exchanges by
  :class:`~.network_model.NetworkModel` (redirect hops, ExtraInfo, workers,
  WebSockets).

Each collection waits up to ``drain_timeout_s`` for body reads in flight,
then swaps everything settled out in one synchronous statement and writes,
for that step: ``exchanges.warc.gz`` (``archive/warc.py``), ``index.jsonl``
(one ``exchange`` line per exchange, exactly once, when it settles; a
``pending`` line for each exchange still in flight, and for each written
``streamed_later`` exchange whose body is still streaming; a ``body`` line per
refetched body) and ``websocket.jsonl`` (frames). There is no match key in the
index: replay (M30) derives keys from the raw request facts with one
canonicaliser, so a rule change leaves no stale key behind.

The archiver always records live traffic. A run that replays an archive
(``network_source: archive``) has none — every byte it serves is already in
the source run's archive — so the cooperation check refuses this archiver
there, by its ``fetch_response`` need, and names the one a replay binds
instead: ``browser.replay_log`` (``archive/replay_log.py``), which records the
replay server's decisions.

.. note::
   No ``from __future__ import annotations`` — the archive package rule.
"""

import asyncio
import base64
import contextlib
import hashlib
import logging
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

from pydantic import Field

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.archive.privacy import MARKER_SESSION_STATE
from spectragent.core.capabilities.browser.archive.base import (
    ArchiveNeed,
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
)
from spectragent.core.capabilities.browser.archive.encoding import jsonl
from spectragent.core.capabilities.browser.archive.network_model import (
    REFETCH_SAFE_TYPES,
    Exchange,
    HeldBytes,
    NetworkModel,
    admit_network_event,
    header_value,
)
from spectragent.core.capabilities.browser.archive.redaction import (
    MARKER_BROWSER_REDACTED,
    browser_ledger,
    stripped_header_set,
)
from spectragent.core.capabilities.browser.archive.warc import PayloadIndex, StepWarc, warc_date
from spectragent.core.capabilities.browser.recorder import (
    FRAME_TARGET_TYPES,
    TARGET_BOOKKEEPING,
    CDPCommand,
    PausedRequest,
    RecorderEvent,
    RecorderTarget,
)

logger = logging.getLogger(__name__)

#: The inline tier's upper bound: the replay's default fulfil cap, the most a
#: replay serves of one body by default. Not a message limit: Chrome caps a CDP
#: message it reads at 100 MiB (that binds the replay's fulfil), and a reply it
#: sends may reach 256 MiB (``docs/architecture_design.md`` §4.14, "消息上限").
MAX_INLINE_BODY_CAP = 64 * 1024 * 1024
#: Chunk size of a streamed refetch (``IO.read``).
STREAM_CHUNK_BYTES = 1024 * 1024
#: ``Content-Range: bytes 0-99/1000`` / ``bytes */1000``.
_CONTENT_RANGE = re.compile(r"^\s*(\w+)\s+(?:(\d+)-(\d+)|\*)/(\d+|\*)\s*$")

WARC_NAME = "exchanges.warc.gz"
INDEX_NAME = "index.jsonl"
WEBSOCKET_NAME = "websocket.jsonl"


def parse_content_range(value: str | None) -> dict[str, Any] | None:
    """``bytes 0-299/1000`` → ``{"unit": "bytes", "start": 0, "end": 299, "total": 1000}``."""
    if not value:
        return None
    match = _CONTENT_RANGE.match(value)
    if match is None:
        return {"raw": value}
    unit, start, end, total = match.groups()
    return {
        "unit": unit,
        "start": int(start) if start is not None else None,
        "end": int(end) if end is not None else None,
        "total": int(total) if total not in (None, "*") else None,
    }


def _declared_length(headers: Any) -> int | None:
    value = header_value(headers, "content-length")
    if value is None:
        return None
    try:
        length = int(value.strip())
    except ValueError:
        return None
    return length if length >= 0 else None


def _decode_body(reply: Any) -> bytes:
    body = (reply or {}).get("body") or ""
    if (reply or {}).get("base64Encoded"):
        return base64.b64decode(body)
    return str(body).encode("utf-8")


class NetworkArchiver(BrowserArchiverBase):
    """Request/response pairs with bodies, as WARC/1.1 plus a step index."""

    __registry_key__ = "browser.network"
    needs: ClassVar[frozenset[ArchiveNeed]] = frozenset({"fetch_response"})

    def __init__(
        self,
        max_inline_body_bytes: Annotated[
            int,
            Field(
                ge=0,
                le=MAX_INLINE_BODY_CAP,
                description=(
                    "Bodies whose declared Content-Length is at most this many bytes are "
                    "read inline at the Fetch Response stage. Larger ones are released "
                    "unread first; a GET of a static resource is then fetched again and "
                    "streamed (streamed_later), anything else is recorded as too_large. "
                    "At most 64 MiB, the replay's default limit for one body."
                ),
            ),
        ] = 5_000_000,
        max_streamed_body_bytes: Annotated[
            int,
            Field(
                ge=0,
                description=(
                    "Most bytes a streamed refetch of a large static body reads before it "
                    "gives up and records too_large."
                ),
            ),
        ] = 100_000_000,
        body_read_timeout_s: Annotated[
            float,
            Field(
                gt=0,
                description=(
                    "Seconds one inline Fetch.getResponseBody may take. At the Response "
                    "stage the read includes the download itself, so this is a download "
                    "budget, not a cache read; a timed-out body is recorded incomplete."
                ),
            ),
        ] = 10.0,
        command_timeout_s: Annotated[
            float,
            Field(
                gt=0, description="Seconds every other recorder command of this archiver may take."
            ),
        ] = 1.0,
        drain_timeout_s: Annotated[
            float,
            Field(
                ge=0,
                description=(
                    "Seconds each collection waits for body reads already in flight before "
                    "it lists them as pending; a pending exchange is written, once, when it "
                    "settles."
                ),
            ),
        ] = 2.0,
        max_post_data_size: Annotated[
            int,
            Field(
                ge=0,
                description=(
                    "Network.enable maxPostDataSize: the largest POST body the Network "
                    "events carry inline (the paused request carries it regardless)."
                ),
            ),
        ] = 65536,
        strip_request_headers: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Request headers stripped at capture in addition to Cookie, "
                    "Authorization and Proxy-Authorization (case-insensitive). They are "
                    "never written; the ledger records their names."
                ),
            ),
        ] = None,
        require_replayable: Annotated[
            bool,
            Field(
                description=(
                    "Refuse to record unless the capability's replayability tier is not "
                    "'off': an archive recorded with unpinned randomness and clocks may "
                    "not replay. Set false to archive an evaluation run as it is."
                ),
            ),
        ] = True,
        allow_session_state: Annotated[
            bool,
            Field(
                description=(
                    "Accept recording a browser that did not start from a fresh profile "
                    "(logged-in state). Off: such a configuration is refused at binding."
                ),
            ),
        ] = False,
    ) -> None:
        self.max_inline_body_bytes = max_inline_body_bytes
        self.max_streamed_body_bytes = max_streamed_body_bytes
        self.body_read_timeout_s = body_read_timeout_s
        self.command_timeout_s = command_timeout_s
        self.drain_timeout_s = drain_timeout_s
        self.max_post_data_size = max_post_data_size
        self.strip_request_headers = list(strip_request_headers or [])
        self.require_replayable = require_replayable
        self.allow_session_state = allow_session_state

    @property
    def requires_replayable(self) -> bool:
        return self.require_replayable

    @property
    def privacy_markers(self) -> frozenset[str]:
        # ``Set-Cookie`` response headers are kept for replay's cookie jar.
        return frozenset({MARKER_BROWSER_REDACTED, MARKER_SESSION_STATE})

    def redaction_facts(self) -> Mapping[str, Any]:
        return {"stripped_request_headers": sorted(stripped_header_set(self.strip_request_headers))}

    def open(self, ctx: BrowserArchiveContext) -> "NetworkSession":
        """The run's capture."""
        return NetworkSession(self, ctx)


@dataclass
class _BodyLine:
    """A refetched body, held until its exchange line is written."""

    record_id: str
    line: dict[str, Any]
    blob: ArchiveFile | None
    captured_at: float


class NetworkSession(BrowserArchiveSession):
    """One run's network capture."""

    archiver: NetworkArchiver

    def __init__(self, archiver: NetworkArchiver, ctx: BrowserArchiveContext) -> None:
        super().__init__(archiver, ctx)
        self._strip = stripped_header_set(archiver.strip_request_headers)
        self.model = NetworkModel(strip=self._strip, target_of=ctx.recorder.target, clock=ctx.clock)
        #: The side store: bytes held back at admission, by the admitted
        #: event's id, until that event is handled (§4.14: bytes never travel
        #: in events).
        self._held: dict[str, HeldBytes] = {}
        self._payloads = PayloadIndex()
        #: Body reads in flight (one per pause being handled).
        self._reads: set[asyncio.Future[None]] = set()
        #: Streamed refetches in flight, with the exchange each one completes.
        self._refetches: dict[asyncio.Task[None], Exchange] = {}
        self._body_lines: list[_BodyLine] = []
        #: Exchange record ids already written (what a body line may follow).
        self._written: set[str] = set()
        #: Unexpected failures of this session's own code; fail the next collection.
        self._errors: list[str] = []
        self._closed = False

    # ── recorder participant ─────────────────────────────────────────

    def target_commands(self, target: RecorderTarget) -> Sequence[CDPCommand]:
        return (
            CDPCommand("Network.enable", {"maxPostDataSize": self.archiver.max_post_data_size}),
        )

    def fetch_patterns(self, target: RecorderTarget) -> Sequence[Mapping[str, Any]]:
        return ({"urlPattern": "*", "requestStage": "Response"},)

    @property
    def pause_budget_s(self) -> float:
        """The longest :meth:`on_paused` runs: a POST body read, then the body read."""
        return self.archiver.command_timeout_s + self.archiver.body_read_timeout_s

    def event_methods(self) -> Collection[str]:
        return self.EVENT_METHODS

    def admit_event(
        self, method: str, params: Mapping[str, Any], session_id: str | None
    ) -> RecorderEvent | None:
        """Metadata for the archive scope; the bytes stay here (§4.14)."""
        if self._closed:
            return None
        slim, held = admit_network_event(method, params, strip=self._strip)
        event = RecorderEvent(session_id=session_id, method=method, params=slim)
        if held is not None:
            self._held[event.event_id] = held
        return event

    def on_event(self, event: RecorderEvent) -> None:
        held = self._held.pop(event.event_id, None)
        handler = self._EVENTS.get(event.method)
        if handler is None:
            return
        try:
            handler(self, event, held)
        except Exception as exc:  # a bug here must not pass silently
            self._errors.append(f"{event.method}: {exc!r}")
            raise

    def _on_request(self, event: RecorderEvent, held: HeldBytes | None) -> None:
        post = held.post if held is not None else None
        self.model.request_will_be_sent(event.session_id, event.params, post=post)

    def _on_request_extra(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.request_will_be_sent_extra_info(event.session_id, event.params)

    def _on_response(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.response_received(event.session_id, event.params)

    def _on_response_extra(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.response_received_extra_info(event.session_id, event.params)

    def _on_cache(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.request_served_from_cache(event.session_id, event.params)

    def _on_finished(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.loading_finished(event.session_id, event.params)

    def _on_failed(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.loading_failed(event.session_id, event.params)

    def _on_detached(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.target_detached(event.session_id)

    def _on_ws_created(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.websocket_created(event.session_id, event.params)

    def _on_ws_request(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.websocket_handshake_request(event.session_id, event.params)

    def _on_ws_response(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.websocket_handshake_response(event.session_id, event.params)

    def _on_ws_frame(self, event: RecorderEvent, held: HeldBytes | None, direction: str) -> None:
        payload = held.payload if held is not None else None
        self.model.websocket_frame(
            event.session_id, event.params, direction=direction, payload=payload
        )

    def _on_ws_sent(self, event: RecorderEvent, held: HeldBytes | None) -> None:
        self._on_ws_frame(event, held, "sent")

    def _on_ws_received(self, event: RecorderEvent, held: HeldBytes | None) -> None:
        self._on_ws_frame(event, held, "received")

    def _on_ws_error(self, event: RecorderEvent, held: HeldBytes | None) -> None:
        self._on_ws_frame(event, held, "error")

    def _on_ws_closed(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.websocket_closed(event.session_id, event.params)

    _EVENTS: ClassVar[dict[str, Any]] = {
        "Network.requestWillBeSent": _on_request,
        "Network.requestWillBeSentExtraInfo": _on_request_extra,
        "Network.responseReceived": _on_response,
        "Network.responseReceivedExtraInfo": _on_response_extra,
        "Network.requestServedFromCache": _on_cache,
        "Network.loadingFinished": _on_finished,
        "Network.loadingFailed": _on_failed,
        "Target.detachedFromTarget": _on_detached,
        "Network.webSocketCreated": _on_ws_created,
        "Network.webSocketWillSendHandshakeRequest": _on_ws_request,
        "Network.webSocketHandshakeResponseReceived": _on_ws_response,
        "Network.webSocketFrameSent": _on_ws_sent,
        "Network.webSocketFrameReceived": _on_ws_received,
        "Network.webSocketFrameError": _on_ws_error,
        "Network.webSocketClosed": _on_ws_closed,
    }
    #: The CDP events this session asks the recorder for: every handled one
    #: except the recorder's own target bookkeeping, which it gets anyway.
    EVENT_METHODS: ClassVar[frozenset[str]] = frozenset(_EVENTS) - TARGET_BOOKKEEPING

    async def on_paused(self, paused: PausedRequest) -> None:
        """Read a paused response's body; the recorder continues it afterwards.

        The future registered first thing — before any ``await`` — is what a
        collection drains; it resolves in ``finally`` whatever the read did.
        """
        if self._closed or paused.stage != "Response":
            return
        params = paused.params
        status = params.get("responseStatusCode")
        if not isinstance(status, int | float):
            return  # responseErrorReason: no response; the Network events say why
        url = paused.url
        if paused.network_id is None:
            # Not traffic the inspector knows. A defensive fallback: the
            # archiver's own refetch (``Network.loadNetworkResource``) neither
            # pauses here nor emits Network events (measured, Chrome 155), but a
            # Chrome that paused it would hand over no networkId. Released as is.
            return
        target = paused.target
        target_id = target.target_id if target is not None else paused.session_id
        hop = self.model.pause_started(
            session_id=paused.session_id, target_id=target_id, params=params
        )
        if hop is None:
            return
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._reads.add(done)
        try:
            await self._read_body(paused, hop, int(status), url, target)
        except asyncio.CancelledError:
            if hop.body_reading:
                self.model.body_finished(
                    hop, status="incomplete", note="the body read was cancelled"
                )
            raise
        except Exception as exc:
            self._errors.append(f"on_paused {url}: {exc!r}")
            if hop.body_reading:
                self.model.body_finished(hop, status="incomplete", note=f"internal error: {exc!r}")
        finally:
            self._reads.discard(done)
            if not done.done():
                done.set_result(None)

    async def _read_body(
        self,
        paused: PausedRequest,
        hop: Exchange,
        status: int,
        url: str,
        target: RecorderTarget | None,
    ) -> None:
        params = paused.params
        archiver = self.archiver
        headers = params.get("responseHeaders") or []
        if hop.has_post_data and hop.post_body is None:
            await self._fetch_post_data(paused, hop)
        if 300 <= status < 400 and header_value(headers, "location") is not None:
            self.model.body_finished(hop, status=None)  # a redirect hop: no body
            return
        content_range = parse_content_range(header_value(headers, "content-range"))
        declared = _declared_length(headers)
        if declared is not None and declared > archiver.max_inline_body_bytes:
            refetchable = (
                status == 200
                and hop.method == "GET"
                and (params.get("resourceType") or hop.resource_type) in REFETCH_SAFE_TYPES
                and declared <= archiver.max_streamed_body_bytes
            )
            note = f"declared Content-Length {declared} > max_inline_body_bytes"
            if refetchable:
                self.model.body_finished(hop, status="streamed_later", note=note)
                frame_id = (
                    params.get("frameId")
                    if target is None or target.type in FRAME_TARGET_TYPES
                    else None
                )
                self._spawn_refetch(hop, paused.session_id, frame_id)
            else:
                self.model.body_finished(
                    hop, status="too_large", note=note, content_range=content_range
                )
            return
        try:
            reply = await self.ctx.recorder.send(
                "Fetch.getResponseBody",
                {"requestId": paused.request_id},
                paused.session_id,
                timeout=archiver.body_read_timeout_s,
            )
        except TimeoutError:
            self.model.body_finished(
                hop,
                status="incomplete",
                note=f"Fetch.getResponseBody did not answer within {archiver.body_read_timeout_s:g}s",
                content_range=content_range,
            )
            return
        except Exception as exc:  # a CDP error: no body for this response
            empty = status in (204, 304) or hop.method == "HEAD"
            self.model.body_finished(
                hop,
                status="ok" if empty else "incomplete",
                body=b"" if empty else None,
                note=None if empty else f"Fetch.getResponseBody failed: {exc}",
                content_range=content_range,
            )
            return
        self.model.body_finished(
            hop, status="ok", body=_decode_body(reply), content_range=content_range
        )

    async def _fetch_post_data(self, paused: PausedRequest, hop: Exchange) -> None:
        """The last POST source: ``Network.getRequestPostData`` on the reporting session."""
        session = hop.session_id or paused.session_id
        try:
            reply = await self.ctx.recorder.send(
                "Network.getRequestPostData",
                {"requestId": hop.network_id},
                session,
                timeout=self.archiver.command_timeout_s,
            )
        except Exception as exc:
            hop.post_source = f"missing (getRequestPostData failed: {exc})"
            return
        data = (reply or {}).get("postData")
        if not isinstance(data, str):
            return
        body = (
            base64.b64decode(data) if (reply or {}).get("base64Encoded") else data.encode("utf-8")
        )
        self.model.set_post_body(hop, body, "getRequestPostData")

    # ── streamed refetch ─────────────────────────────────────────────

    def _spawn_refetch(self, hop: Exchange, session_id: str, frame_id: str | None) -> None:
        task = asyncio.get_running_loop().create_task(self._refetch(hop, session_id, frame_id))
        self._refetches[task] = hop
        task.add_done_callback(lambda done: self._refetches.pop(done, None))

    async def _refetch(self, hop: Exchange, session_id: str, frame_id: str | None) -> None:
        """Fetch a large static body again, after the page got its response, in chunks.

        Follows Playwright's ``createResponseBodyCallback`` (crNetworkManager.ts
        @ d9f2fd3e, Apache-2.0; see THIRD_PARTY_NOTICES.md): only a ``GET`` of a
        refetch-safe static type, ``Network.loadNetworkResource`` then
        ``IO.read`` until ``eof``. Modified: run after the response was
        released, ≤ 1 MiB per read, stopped at ``max_streamed_body_bytes``.
        """
        archiver = self.archiver
        send = self.ctx.recorder.send
        line: dict[str, Any] = {"kind": "body", "record_id": hop.record_id}
        blob: ArchiveFile | None = None
        stream: str | None = None
        try:
            params: dict[str, Any] = {
                "url": hop.url,
                "options": {"disableCache": False, "includeCredentials": True},
            }
            if frame_id:
                params["frameId"] = frame_id
            reply = await send(
                "Network.loadNetworkResource",
                params,
                session_id,
                timeout=archiver.body_read_timeout_s,
            )
            resource = (reply or {}).get("resource") or {}
            stream = resource.get("stream")
            if not resource.get("success") or not stream:
                line.update(
                    body_status="incomplete",
                    note=f"loadNetworkResource failed: {resource.get('netErrorName') or resource.get('httpStatusCode')}",
                )
                return
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = await send(
                    "IO.read",
                    {"handle": stream, "size": STREAM_CHUNK_BYTES},
                    session_id,
                    timeout=archiver.body_read_timeout_s,
                )
                data = (chunk or {}).get("data") or ""
                piece = (
                    base64.b64decode(data)
                    if (chunk or {}).get("base64Encoded")
                    else data.encode("utf-8")
                )
                chunks.append(piece)
                size += len(piece)
                if size > archiver.max_streamed_body_bytes:
                    line.update(
                        body_status="too_large",
                        note=f"refetch exceeded max_streamed_body_bytes ({size} bytes read)",
                    )
                    return
                if (chunk or {}).get("eof"):
                    break
            body = b"".join(chunks)
            digest = hashlib.sha256(body).hexdigest()
            blob = ArchiveFile(
                archiver_id=archiver.archiver_id,
                name=f"body-{digest[:32]}.bin",
                media_type=hop.mime_type or "application/octet-stream",
                data=body,
                meta=ArchiveProvenance(
                    captured_at=self.ctx.clock(), ids={"record_id": hop.record_id}
                ),
                content_addressed=True,
            )
            line.update(body_status="ok", payload_sha256=digest, payload_length=len(body))
        except asyncio.CancelledError:
            line.update(body_status="incomplete", note="refetch cancelled")
            raise
        except Exception as exc:
            line.update(body_status="incomplete", note=f"refetch failed: {exc}")
        finally:
            if stream:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        send("IO.close", {"handle": stream}, session_id), archiver.command_timeout_s
                    )
            self._body_lines.append(_BodyLine(hop.record_id, line, blob, self.ctx.clock()))

    # ── collection ───────────────────────────────────────────────────

    async def collect(self) -> list[ArchiveFile]:
        """Wait (bounded) for bodies in flight, swap, then write this step's files."""
        archiver = self.archiver
        inflight = [*self._reads, *self._refetches]
        if inflight and archiver.drain_timeout_s > 0:
            await asyncio.wait(inflight, timeout=archiver.drain_timeout_s)
        if self._errors:
            errors, self._errors = self._errors, []
            raise RuntimeError(
                f"{archiver.archiver_id}: the capture failed and the archive would be "
                f"incomplete: {'; '.join(errors)}"
            )
        # ── the swap: synchronous from here on ──
        settled = self.model.take()
        self._written.update(exchange.record_id for exchange in settled.exchanges)
        ready = [line for line in self._body_lines if line.record_id in self._written]
        self._body_lines = [
            line for line in self._body_lines if line.record_id not in self._written
        ]
        # A written ``streamed_later`` exchange whose body is still streaming:
        # its record must not end on a line that promises a body (Flag 1).
        streaming = [
            hop
            for task, hop in self._refetches.items()
            if not task.done() and hop.record_id in self._written
        ]
        return self._files(settled.exchanges, settled.pending, settled.frames, ready, streaming)

    def _files(
        self,
        exchanges: Sequence[Exchange],
        pending: Sequence[Exchange],
        frames: Sequence[dict[str, Any]],
        bodies: Sequence[_BodyLine],
        streaming: Sequence[Exchange] = (),
    ) -> list[ArchiveFile]:
        if not (exchanges or pending or frames or bodies or streaming):
            return []
        now = self.ctx.clock()
        archiver_id = self.archiver.archiver_id
        warc = StepWarc(self._payloads)
        lines: list[dict[str, Any]] = [
            self._exchange_line(warc, exchange) for exchange in exchanges
        ]
        lines.extend(
            {
                "kind": "pending",
                "record_id": exchange.record_id,
                "method": exchange.method,
                "url": exchange.url,
                "waiting_for": exchange.waiting_for() or "raw_headers",
            }
            for exchange in pending
        )
        lines.extend(
            {
                "kind": "pending",
                "record_id": exchange.record_id,
                "method": exchange.method,
                "url": exchange.url,
                "waiting_for": "streamed_body",
            }
            for exchange in streaming
        )
        lines.extend(body.line for body in bodies)
        captured = [exchange.captured_at for exchange in exchanges] + [
            b.captured_at for b in bodies
        ]
        captured_at = max(captured) if captured else now
        meta = ArchiveProvenance(captured_at=min(captured_at, now))
        files: list[ArchiveFile] = []
        if warc.records:
            files.append(
                ArchiveFile(
                    archiver_id=archiver_id,
                    name=WARC_NAME,
                    media_type="application/warc",
                    data=warc.getvalue(),
                    meta=meta,
                )
            )
        files.append(
            ArchiveFile(
                archiver_id=archiver_id,
                name=INDEX_NAME,
                media_type="application/x-ndjson",
                data=jsonl(lines),
                meta=meta,
            )
        )
        if frames:
            files.append(
                ArchiveFile(
                    archiver_id=archiver_id,
                    name=WEBSOCKET_NAME,
                    media_type="application/x-ndjson",
                    data=jsonl(frames),
                    meta=ArchiveProvenance(captured_at=now),
                )
            )
        files.extend(body.blob for body in bodies if body.blob is not None)
        return files

    def _exchange_line(self, warc: StepWarc, exchange: Exchange) -> dict[str, Any]:
        body_status = exchange.final_body_status()
        body = exchange.body if body_status == "ok" else None
        if body_status == "ok" and body is None:
            body = b""  # empty by protocol (204, 304, HEAD)
        payload_sha256 = hashlib.sha256(body).hexdigest() if body is not None else None
        response_headers = exchange.raw_response_headers or exchange.response_headers or {}
        known_response = (
            body_status in ("ok", "redirect", "websocket") and exchange.status is not None
        )
        written = warc.write_exchange(
            record_id=exchange.record_id,
            url=exchange.url,
            method=exchange.method or "GET",
            request_headers=exchange.request_headers,
            request_body=exchange.post_body,
            date=warc_date(exchange.started_at or exchange.captured_at),
            response=(
                (exchange.status or 0, exchange.status_text, response_headers, body or b"")
                if known_response
                else None
            ),
            payload_sha256=payload_sha256,
            remote_address=exchange.remote_address,
        )
        original = {
            name: header_value(response_headers, name)
            for name in ("content-encoding", "transfer-encoding", "content-length")
            if header_value(response_headers, name) is not None
        }
        post = (
            {
                "sha256": hashlib.sha256(exchange.post_body).hexdigest(),
                "length": len(exchange.post_body),
                "content_type": exchange.post_content_type,
                "source": exchange.post_source,
            }
            if exchange.post_body is not None
            else (
                {"missing": exchange.post_source or "not captured"}
                if exchange.has_post_data
                else None
            )
        )
        line: dict[str, Any] = {
            "kind": "exchange",
            "record_id": exchange.record_id,
            "request_record_id": written.request_record_id,
            "response_record_id": written.response_record_id,
            "warc": {
                "request": {"offset": written.request_offset, "length": written.request_length},
                "response": (
                    {"offset": written.response_offset, "length": written.response_length}
                    if written.response_record_id is not None
                    else None
                ),
            },
            "revisit_of": written.revisit_of,
            "redirected_from": exchange.redirected_from,
            "network_id": exchange.network_id,
            "hop": exchange.hop,
            "method": exchange.method,
            "url": exchange.url,
            "url_fragment": exchange.url_fragment,
            "status": exchange.status,
            "status_text": exchange.status_text,
            "resource_type": exchange.resource_type,
            "mime_type": exchange.mime_type,
            "protocol": exchange.protocol,
            "remote_address": exchange.remote_address,
            "frame_id": exchange.frame_id,
            "loader_id": exchange.loader_id,
            "document_url": exchange.document_url,
            "session_id": exchange.session_id,
            "target_id": exchange.target_id,
            "target_type": exchange.target_type,
            "paused_session_id": exchange.paused_session,
            "initiator": exchange.initiator,
            "started_at": exchange.started_at,
            "timing": exchange.timing,
            "from_service_worker": exchange.from_service_worker,
            "from_cache": exchange.served_from_cache or exchange.from_disk_cache,
            "encoded_length": exchange.encoded_length,
            "request": {
                "headers": exchange.request_headers,
                "raw_headers": exchange.raw_request_headers,
                "post": post,
            },
            "response_headers": None if written.response_record_id else response_headers,
            "raw_response_headers": exchange.raw_response_headers is not None,
            "original_encoding": original or None,
            "payload_sha256": payload_sha256,
            "payload_length": len(body) if body is not None else None,
            "content_range": exchange.content_range,
            "body_status": body_status,
            "body_note": exchange.body_note,
            "error_text": exchange.error_text,
        }
        if exchange.redactions:
            line["redaction"] = browser_ledger(exchange.redactions).model_dump(mode="json")
        return line

    async def aclose(self) -> None:
        self._closed = True
        self._held.clear()
        tasks = list(self._refetches)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
