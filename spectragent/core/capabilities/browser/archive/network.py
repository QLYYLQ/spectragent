"""``browser.network`` — every request, response and body the browser exchanged (§4.14).

The archiver is a recorder participant twice over:

- **Bodies at the Fetch Response stage.** Every frame and worker target asks
  for a Response-stage ``Fetch`` pause (the recorder merges the patterns into
  one ``Fetch.enable``), and :meth:`NetworkSession.on_paused` decides there,
  while Chrome holds the response, how its body is captured — the recorder
  continues it in ``finally``, whatever happened here. Nothing depends on
  ``Network.getResponseBody``'s buffer (200 MB in total, 20 MB per body,
  first-in-first-out, dropped on a cross-process navigation). By the
  declared ``Content-Length``:

  - up to ``max_inline_body_bytes``, or none declared: read inline with
    ``Fetch.getResponseBody`` (bounded by ``body_read_timeout_s`` — at this
    stage the read includes the download), and the response stays held
    until the read answers. Releasing it earlier gains the page nothing and
    can hide a failed download from it (measured, Chrome 155): once the read
    has started, Chrome hands the page none of the body before the download
    completes, continued or not (a body trickled over 5 s reached a released
    page in one piece at 5.03 s); and a content-encoded body whose download
    fails after the release reaches the page as a *successful* empty
    response — ``loadingFinished`` with ``encodedDataLength`` 0, the
    script's ``load`` event, not ``error`` — where a response still held
    when the download fails reaches it as the network error (8 of 8 each
    way). That is how iQIYI's home page stayed on its skeleton: its main
    chunks, cut off mid-download after the release, ran as empty scripts.
    Chrome answers the read with the bytes that arrived, so a read whose
    request then fails is recorded ``incomplete`` (``network_model.py``).
    ``Network.getResponseBody`` after the load is no substitute: it hands a
    non-UTF-8 document back as decoded text (1875 GBK bytes came back as
    2675 UTF-8 ones);
  - above it, up to :data:`MAX_BODY_BYTES` (64 MiB, the replay's default
    limit for one body): **streamed while the page reads it** —
    ``Network.streamResourceContent`` on the session that reported the
    request, then the response is released, and the page's own download
    arrives as ``Network.dataReceived`` chunks (``streamed_later``; a
    ``body`` index line points the record at a content-addressed blob when
    the request ends). Measured (Chrome 155, a page fetching 60 MiB at
    8 MiB/s): inline, the page's first byte came after 8.1 s, when the whole
    body had been downloaded and handed over; streamed, after 5 ms, and it
    finished with the unrecorded page (7.48 s) — §4.14 "体积分档". A
    document is the exception and is read inline: a navigation's body can
    only stream once it commits, which the pause holds back;
  - over 64 MiB: ``too_large``, never read — and a body that turns out
    larger once decoded (no declared length, or compressed) too.

  The size limit is this archiver's policy, not a transport limit: Chrome
  caps a CDP message it *reads* at 100 MiB, which binds the replay's
  fulfil, while a reply it sends may reach 256 MiB
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
  WebSockets). A streamed chunk never enters the scope at all: admission
  appends it to its stream (the side store) and admits nothing.

Each collection waits up to ``drain_timeout_s`` for inline body reads in
flight that no earlier collection waited for (never for a stream: it ends
when the page's download does; a read that outlived one wait is a slow
download, which ends when it ends), then
swaps everything settled out in one synchronous statement and writes, for
that step: ``exchanges.warc.gz`` (``archive/warc.py``), ``index.jsonl`` (one
``exchange`` line per exchange, exactly once, when it settles; a ``pending``
line for each exchange still in flight, and for each written
``streamed_later`` exchange whose body is still streaming; a ``body`` line
per streamed body when its request ends) and ``websocket.jsonl`` (frames).
A ``pending`` line carries the request's start time (``started_at``); an
``exchange`` line, and a streamed exchange's ``body`` line, the time its
request ended (``ended_at``, ``None`` while it had not) — evidence of how long
a source request lived before the page cancelled it (§4.14).
There is no match key in the index: replay derives keys from the raw
request facts with one canonicaliser, so a rule change leaves no stale key
behind.

The archiver always records live traffic. A run that replays an archive
(``network_source: archive``) has none — every byte it serves is already in
the source run's archive — so the cooperation check refuses this archiver
there, by its ``fetch_response`` need, and names the one a replay binds
instead: ``browser.replay_log`` (``archive/replay_log.py``), which records the
replay server's decisions.
"""

import asyncio
import base64
import binascii
import hashlib
import logging
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
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
    TARGET_DETACHED_ERROR,
    Exchange,
    HeldBytes,
    NetworkModel,
    admit_network_event,
    failure_text,
    header_value,
)
from spectragent.core.capabilities.browser.archive.redaction import (
    MARKER_BROWSER_REDACTED,
    browser_ledger,
    stripped_header_set,
)
from spectragent.core.capabilities.browser.archive.warc import PayloadIndex, StepWarc, warc_date
from spectragent.core.capabilities.browser.recorder import (
    TARGET_BOOKKEEPING,
    CDPCommand,
    PausedRequest,
    RecorderEvent,
    RecorderTarget,
)

logger = logging.getLogger(__name__)

#: The largest body recorded: the replay's default fulfil limit
#: (``replay/archive_source.py`` ``DEFAULT_FULFILL_BODY_BYTES``), so everything
#: a default replay can serve is captured, whatever its type (the user's
#: ruling of 2026-10-13); a larger body is ``too_large``. Not a message limit:
#: Chrome caps a CDP message it reads at 100 MiB (that binds the replay's
#: fulfil), and a reply it sends may reach 256 MiB
#: (``docs/architecture_design.md`` §4.14, "消息上限").
MAX_BODY_BYTES = 64 * 1024 * 1024
#: The event that carries a streamed body's chunks (``Network.streamResourceContent``).
STREAM_CHUNK_EVENT = "Network.dataReceived"
#: ``Content-Range: bytes 0-99/1000`` / ``bytes */1000``.
_CONTENT_RANGE = re.compile(r"^\s*(\w+)\s+(?:(\d+)-(\d+)|\*)/(\d+|\*)\s*$")

WARC_NAME = "exchanges.warc.gz"
INDEX_NAME = "index.jsonl"
WEBSOCKET_NAME = "websocket.jsonl"
#: How a ``body_note`` begins when the body read did not answer while the
#: response was held (``body_read_timeout_s``; the seconds follow): replay's
#: loading reads it as the recorder's timeout, not the page's or the
#: network's (arch §4.14 "加载").
BODY_READ_TIMEOUT_NOTE = "Fetch.getResponseBody did not answer within"


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
                le=MAX_BODY_BYTES,
                description=(
                    "Bodies whose declared Content-Length is at most this many bytes, or "
                    "that declare none, are read inline at the Fetch Response stage, while "
                    "Chrome holds the response until the whole body is in. A larger body, "
                    "up to 64 MiB (the replay's default limit for one body), is streamed "
                    "while the page reads it, without holding the response "
                    "(streamed_later); a document is read inline at any size up to 64 MiB. "
                    "A body over 64 MiB is recorded as too_large, never read. At most 64 MiB."
                ),
            ),
        ] = 5_000_000,
        body_read_timeout_s: Annotated[
            float,
            Field(
                gt=0,
                description=(
                    "Seconds Chrome holds a response while its inline Fetch.getResponseBody "
                    "runs. At the Response stage the read includes the download itself, so "
                    "this is a download budget, not a cache read. The response is held until "
                    "the read answers: once the read has started Chrome gives the page none "
                    "of the body before the download completes, and a response released "
                    "while its read runs reaches the page as a successful empty or cut-short "
                    "body if the download then fails. A read still running at the limit is "
                    "recorded incomplete and the response released."
                ),
            ),
        ] = 70.0,
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
                    "never written; the ledger records their names. None (the default): "
                    "only those three."
                ),
            ),
        ] = None,
        require_replayable: Annotated[
            bool,
            Field(
                description=(
                    "Refuse to record unless the capability's replayability tier is not "
                    "'none': an archive recorded with unpinned randomness and clocks may "
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
    """A streamed body's outcome, held until its exchange line is written."""

    record_id: str
    line: dict[str, Any]
    blob: ArchiveFile | None
    captured_at: float


@dataclass
class _Stream:
    """A body streamed while the page reads it (``Network.streamResourceContent``).

    Chunks come in ``Network.dataReceived`` on the session streaming was
    enabled on, appended at admission; :meth:`outcome` judges the body when
    the request ends.
    """

    hop: Exchange
    #: The session streaming was enabled on, which carries the chunks.
    session_id: str
    #: The declared ``Content-Length`` — of the encoded body when compressed.
    declared: int
    #: No ``Content-Encoding``: the declared length is the body's own.
    identity: bool
    #: ``bufferedData`` of the enabling reply: what arrived before streaming.
    prefix: bytes = b""
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    #: Sum of the chunks' ``encodedDataLength`` (bytes on the wire).
    encoded: int = 0
    #: Why the body cannot be kept (``too_large`` or ``incomplete``), once known.
    dropped: tuple[str, str] | None = None

    def start(self, prefix: bytes) -> None:
        self.prefix = prefix
        self.size = len(prefix)
        self._check_size()

    def take(self, params: Mapping[str, Any]) -> None:
        """One ``Network.dataReceived`` of this request: its chunk, if it carries one."""
        encoded = params.get("encodedDataLength")
        if isinstance(encoded, int | float):
            self.encoded += int(encoded)
        data = params.get("data")
        if not data or self.dropped is not None:
            return
        try:
            chunk = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            self._drop("incomplete", f"a streamed chunk did not decode ({exc})")
            return
        self.chunks.append(chunk)
        self.size += len(chunk)
        self._check_size()

    def _check_size(self) -> None:
        if self.size > MAX_BODY_BYTES:
            self._drop("too_large", f"streamed past {MAX_BODY_BYTES} bytes (64 MiB)")

    def _drop(self, status: str, note: str) -> None:
        self.dropped = (status, note)
        self.prefix, self.chunks = b"", []

    def outcome(self, *, finished: bool, ended: str) -> tuple[str, bytes | None, str | None]:
        """``(body_status, body, note)`` once the request ended (*ended* says how).

        Complete when every declared byte arrived — an uncompressed body's
        declared length is its own, whatever the terminal event (a page that
        cancels its reader after the last byte gets ``loadingFailed``,
        measured) — or, for a compressed one, when the load finished or the
        bytes on the wire reached the declared length.
        """
        if self.dropped is not None:
            status, note = self.dropped
            return status, None, note
        if self.identity:
            complete = self.size == self.declared
        else:
            complete = finished or self.encoded >= self.declared
        if not complete:
            return "incomplete", None, f"streamed {self.size} of {self.declared} bytes; {ended}"
        return "ok", self.prefix + b"".join(self.chunks), None


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
        #: Body reads in flight (one per pause being handled).
        self._reads: set[asyncio.Future[None]] = set()
        #: Reads in flight that a collection already waited for: a slow
        #: download, which no later collection waits for again.
        self._waited: set[asyncio.Future[None]] = set()
        #: Bodies streaming while the page reads them, by network request id.
        self._streams: dict[str, _Stream] = {}
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
        """The longest :meth:`on_paused` runs.

        A POST body read and a stream that will not start (two commands),
        then the body read inline.
        """
        return 2 * self.archiver.command_timeout_s + self.archiver.body_read_timeout_s

    def event_methods(self) -> Collection[str]:
        return self.EVENT_METHODS

    def admit_event(
        self, method: str, params: Mapping[str, Any], session_id: str | None
    ) -> RecorderEvent | None:
        """Metadata for the archive scope; the bytes stay here (§4.14).

        A streamed chunk is not admitted at all: it goes straight into its
        stream — or nowhere, when nothing streams that request on that
        session (Chrome reports ``dataReceived`` for every request).
        """
        if self._closed:
            return None
        if method == STREAM_CHUNK_EVENT:
            stream = self._streams.get(str(params.get("requestId") or ""))
            if stream is not None and stream.session_id == session_id:
                stream.take(params)
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
        self._end_stream(event, finished=True, ended="loadingFinished")

    def _on_failed(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.loading_failed(event.session_id, event.params)
        error = event.params.get("errorText") or "no error text"
        self._end_stream(event, finished=False, ended=f"loadingFailed ({error})")

    def _on_detached(self, event: RecorderEvent, _held: HeldBytes | None) -> None:
        self.model.target_detached(event.session_id)
        for request_id, stream in list(self._streams.items()):
            if stream.session_id == event.session_id:
                self._settle_stream(
                    request_id,
                    finished=False,
                    ended="the target detached",
                    ended_at=self.ctx.clock(),
                    error_text=TARGET_DETACHED_ERROR,
                )

    def _end_stream(self, event: RecorderEvent, *, finished: bool, ended: str) -> None:
        """A request's end settles its stream, when it is the streaming session's."""
        request_id = str(event.params.get("requestId") or "")
        stream = self._streams.get(request_id)
        if stream is not None and stream.session_id == event.session_id:
            self._settle_stream(
                request_id,
                finished=finished,
                ended=ended,
                ended_at=self.model.end_time(stream.hop, event.params.get("timestamp")),
                error_text=None if finished else failure_text(event.params),
            )

    def _settle_stream(
        self,
        request_id: str,
        *,
        finished: bool,
        ended: str,
        ended_at: float,
        error_text: str | None,
    ) -> None:
        """Judge a stream whose request ended and queue its ``body`` line (and blob).

        The exchange line went out before the request ended, so the request's
        end — when, and the failure if it failed — is written on this line.
        """
        stream = self._streams.pop(request_id)
        status, body, note = stream.outcome(finished=finished, ended=ended)
        hop = stream.hop
        line: dict[str, Any] = {
            "kind": "body",
            "record_id": hop.record_id,
            "body_status": status,
            "ended_at": ended_at,
            "error_text": error_text,
        }
        blob: ArchiveFile | None = None
        if body is not None:
            digest = hashlib.sha256(body).hexdigest()
            blob = ArchiveFile(
                archiver_id=self.archiver.archiver_id,
                name=f"body-{digest[:32]}.bin",
                media_type=hop.mime_type or "application/octet-stream",
                data=body,
                meta=ArchiveProvenance(
                    captured_at=self.ctx.clock(), ids={"record_id": hop.record_id}
                ),
                content_addressed=True,
            )
            line.update(payload_sha256=digest, payload_length=len(body))
        else:
            line["note"] = note
        self._body_lines.append(_BodyLine(hop.record_id, line, blob, self.ctx.clock()))

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
    #: except the recorder's own target bookkeeping, which it gets anyway, and
    #: the streamed chunks, which admission keeps (they never reach the scope).
    EVENT_METHODS: ClassVar[frozenset[str]] = (frozenset(_EVENTS) - TARGET_BOOKKEEPING) | frozenset(
        {STREAM_CHUNK_EVENT}
    )

    async def on_paused(self, paused: PausedRequest) -> None:
        """Take a paused response's body, or start streaming it; the recorder continues it afterwards.

        The future registered first thing — before any ``await`` — is what a
        collection drains; it resolves in ``finally`` whatever the read did
        (a stream outlives it: the stream ends with its request).
        """
        if self._closed or paused.stage != "Response":
            return
        params = paused.params
        status = params.get("responseStatusCode")
        if not isinstance(status, int | float):
            return  # responseErrorReason: no response; the Network events say why
        url = paused.url
        if paused.network_id is None:
            # Not traffic the inspector knows: nothing to pair it with, and
            # nothing to stream it by. Released as is (defensive: the archiver
            # sends no request of its own).
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
            await self._read_body(paused, hop, int(status))
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
            self._waited.discard(done)
            if not done.done():
                done.set_result(None)

    async def _read_body(self, paused: PausedRequest, hop: Exchange, status: int) -> None:
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
        if declared is not None and declared > MAX_BODY_BYTES:
            self.model.body_finished(
                hop,
                status="too_large",
                note=f"declared Content-Length {declared} > {MAX_BODY_BYTES} (64 MiB)",
                content_range=content_range,
            )
            return
        note: str | None = None
        empty_by_protocol = status in (204, 304) or hop.method == "HEAD"
        if (
            declared is not None
            and declared > archiver.max_inline_body_bytes
            and not empty_by_protocol
        ):
            if (params.get("resourceType") or hop.resource_type) == "Document":
                # A navigation's body streams only once it commits, which this
                # pause holds back (measured, Chrome 155): read it here.
                note = f"a document is read inline (declared Content-Length {declared})"
            else:
                refused = await self._start_stream(paused, hop, declared, headers, content_range)
                if refused is None:
                    return
                note = refused
        # The response stays held until the read answers (module docstring):
        # released earlier, a download that then fails would reach the page
        # as a successful empty body.
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
                note=f"{BODY_READ_TIMEOUT_NOTE} {archiver.body_read_timeout_s:g}s",
                content_range=content_range,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a CDP error: no body for this response
            self.model.body_finished(
                hop,
                status="ok" if empty_by_protocol else "incomplete",
                body=b"" if empty_by_protocol else None,
                note=None if empty_by_protocol else f"Fetch.getResponseBody failed: {exc}",
                content_range=content_range,
            )
            return
        self._keep_body(hop, reply, note, content_range)

    def _keep_body(
        self,
        hop: Exchange,
        reply: Any,
        note: str | None,
        content_range: dict[str, Any] | None,
    ) -> None:
        """The body ``Fetch.getResponseBody`` answered with: kept, or ``too_large``."""
        body = _decode_body(reply)
        if len(body) > MAX_BODY_BYTES:
            self.model.body_finished(
                hop,
                status="too_large",
                note=f"read {len(body)} bytes > {MAX_BODY_BYTES} (64 MiB)",
                content_range=content_range,
            )
            return
        self.model.body_finished(
            hop, status="ok", body=body, note=note, content_range=content_range
        )

    async def _start_stream(
        self,
        paused: PausedRequest,
        hop: Exchange,
        declared: int,
        headers: Any,
        content_range: dict[str, Any] | None,
    ) -> str | None:
        """Stream *hop*'s body while the page reads it; ``None`` once it streams, else why not.

        ``Network.streamResourceContent`` goes to the session that reported the
        request (a dedicated worker's, though its request pauses on the
        page's), before the response is released: nothing has arrived yet, and
        every chunk then comes in ``Network.dataReceived``. The stream is
        registered first, so no chunk can come before it is. When the
        command fails the caller reads the body inline instead, still paused.
        """
        session = hop.session_id or paused.session_id
        encoding = (header_value(headers, "content-encoding") or "identity").strip().lower()
        stream = _Stream(
            hop=hop, session_id=session, declared=declared, identity=encoding == "identity"
        )
        self._streams[hop.network_id] = stream
        try:
            reply = await self.ctx.recorder.send(
                "Network.streamResourceContent",
                {"requestId": hop.network_id},
                session,
                timeout=self.archiver.command_timeout_s,
            )
            stream.start(base64.b64decode((reply or {}).get("bufferedData") or ""))
        except asyncio.CancelledError:
            self._streams.pop(hop.network_id, None)
            raise
        except Exception as exc:
            self._streams.pop(hop.network_id, None)
            return f"streaming could not start ({exc}); read inline"
        self.model.body_finished(
            hop,
            status="streamed_later",
            note=f"declared Content-Length {declared} > max_inline_body_bytes: streamed",
            content_range=content_range,
        )
        return None

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

    # ── collection ───────────────────────────────────────────────────

    async def collect(self) -> list[ArchiveFile]:
        """Wait (bounded) for inline reads in flight, swap, then write this step's files.

        Never for a stream: it ends when the page's download does, which a
        stalled consumer (a paused video, a body nobody reads) may never let
        happen; it stays ``pending`` until it ends. Nor, a second time, for an
        inline read: one still running after a collection waited for it is a
        slow download (up to ``body_read_timeout_s``), and waiting for it in
        every collection would add ``drain_timeout_s`` to every step it spans.
        """
        archiver = self.archiver
        inflight = [read for read in self._reads if read not in self._waited]
        if inflight and archiver.drain_timeout_s > 0:
            await asyncio.wait(inflight, timeout=archiver.drain_timeout_s)
        self._waited.update(read for read in inflight if not read.done())
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
        # its record must not end on a line that promises a body.
        streaming = [
            stream.hop for stream in self._streams.values() if stream.hop.record_id in self._written
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
        warc = StepWarc(PayloadIndex())
        lines: list[dict[str, Any]] = [
            self._exchange_line(warc, exchange) for exchange in exchanges
        ]
        lines.extend(
            {
                "kind": "pending",
                "record_id": exchange.record_id,
                "method": exchange.method,
                "url": exchange.url,
                "started_at": exchange.started_at,
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
                "started_at": exchange.started_at,
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
            "ended_at": exchange.ended_at,
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
        self._streams.clear()
