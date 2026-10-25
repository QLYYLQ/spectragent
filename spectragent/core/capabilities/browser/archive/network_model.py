# Portions of this module are derived from Playwright's
# packages/playwright-core/src/server/chromium/crNetworkManager.ts
# (https://github.com/microsoft/playwright, commit d9f2fd3e2232ace8e317a84eda6cb86e25bcf49c):
#
#   Copyright 2017 Google Inc. All rights reserved.
#   Modifications copyright (c) Microsoft Corporation.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# Modified by Spectragent (2026-10-10): re-implemented in Python as a passive
# accumulator for an archive rather than a live request router. Kept from the
# original: pairing Fetch.requestPaused with Network.requestWillBeSent by the
# network request id in either arrival order; one record per redirect hop,
# closed by the next hop's redirectResponse and linked by redirectedFrom;
# worker requests reported on the worker session but paused on the page
# session; ExtraInfo events paired with responses per request id, with
# responses served from cache treated as having none (crbug 1340398); data:
# URLs skipped; the WebSocket event set and its wall-clock baseline. Not
# ported: routing, interception overrides, authentication, CORS preflight
# synthesis, the frame manager. Added: deduplication across two recorder
# sessions of one target, the archive's settle / pending bookkeeping, and the
# browser redaction rules applied as each event is stored.
"""Network evidence, paired from CDP events into exchanges (§4.14 ``browser.network``).

``NetworkModel`` is the bookkeeping half of the network archiver: it receives
the recorder's metadata events (``Network.*``, synchronously, in the archive
scope's loop) and the pauses its session handles, and turns them into one
:class:`Exchange` per request *hop*. It sends nothing over CDP and never
awaits — the session (``archive/network.py``) reads bodies and decides what to
write; this module decides what belongs to which exchange and when an exchange
is settled.

Rules (Chrome facts measured on Chrome 153 and on the fixture site,
``docs/architecture_design.md`` §4.14):

- **Either order.** A ``Fetch.requestPaused`` reaches the session as soon as
  Chrome sends it; the ``Network`` events arrive later, through the archive
  scope's queue. Both sides attach to the hop by network request id and URL,
  whichever comes first.
- **One record per hop.** A ``requestWillBeSent`` carrying ``redirectResponse``
  closes the previous hop as ``redirected`` and opens the next, linked by
  ``redirected_from``.
- **Two sessions, one target.** A service worker is attached twice (browser
  level and under its page); both sessions report every event of it. Events of
  one request id are taken from the first session of each target that reported
  it, and a second pause of the same hop from another session of the same
  target is a duplicate.
- **Settled once.** An exchange is written exactly once, when its terminal
  event arrived (redirected, finished, failed, or a WebSocket handshake answered),
  its body read is over, and its raw response headers (``responseReceivedExtraInfo``,
  the only source of ``Set-Cookie``) arrived — or one collection passed waiting for
  them. Until then every collection lists it as pending.
- **Redaction at storage.** Stripped request headers never enter an exchange
  (``archive/redaction.py``); of the ExtraInfo events only ``headers`` is kept.
- **Nothing large or secret in the scope.** Before an event enters the archive
  scope (whose history keeps its last 1000 events), :func:`admit_network_event`
  holds its bytes back — ``requestWillBeSent``'s POST body, a WebSocket frame's
  payload — for the session's side store, and leaves out what is never stored
  (stripped header values, the ExtraInfo cookie lists, a response's raw
  request headers). The model gets the held bytes back with the event.
"""

import base64
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from spectragent.core.archive.privacy import RedactionEntry
from spectragent.core.capabilities.browser.archive.redaction import (
    blank_stripped_headers,
    redact_post_body,
    strip_headers,
)
from spectragent.core.capabilities.browser.recorder import RecorderTarget

#: URL schemes that are network traffic worth recording.
RECORDED_SCHEMES = ("http://", "https://", "ws://", "wss://")

BodyStatus = Literal["ok", "redirect", "too_large", "streamed_later", "websocket", "incomplete"]
Terminal = Literal["redirected", "finished", "failed", "websocket"]


#: The ``error_text`` the recorder writes for a request whose target went away
#: before the request ended: not a failure Chrome reported (replay's loading
#: reads it apart from a real network failure, arch §4.14 "加载").
TARGET_DETACHED_ERROR = "the target detached before the request finished"


#: How a ``body_note`` begins on a body read at the Fetch Response stage whose
#: request then failed on the network: the read answered with the bytes that
#: arrived before the failure (arch §4.14 "什么时候定下来").
FAILED_AFTER_READ_NOTE = "the download failed after its body was read"


def _network_failure(params: Mapping[str, Any]) -> bool:
    """Whether ``loadingFailed`` reports the download failing, not the page or a policy.

    The page cancelling (``canceled``) and a blocked or CORS-refused response
    end a request whose body may have arrived whole.
    """
    return not (
        params.get("canceled") or params.get("blockedReason") or params.get("corsErrorStatus")
    )


def failure_text(params: Mapping[str, Any]) -> str:
    """Why ``Network.loadingFailed`` says the request failed (its ``errorText``, else the block)."""
    return str(params.get("errorText") or params.get("blockedReason") or "loading failed")


def recordable(url: str) -> bool:
    return url.startswith(RECORDED_SCHEMES)


def header_value(headers: Any, name: str) -> str | None:
    """A header's value from either CDP header shape, case-insensitively."""
    wanted = name.lower()
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value)
    elif isinstance(headers, list):
        for item in headers:
            if isinstance(item, Mapping) and str(item.get("name", "")).lower() == wanted:
                return str(item.get("value", ""))
    return None


def _new_record_id() -> str:
    return f"urn:uuid:{uuid.uuid4()}"


def _initiator(raw: Any) -> dict[str, Any] | None:
    """The initiator without its call stack (large, and nothing replay keys on)."""
    if not isinstance(raw, Mapping):
        return None
    return {
        key: raw[key]
        for key in ("type", "url", "lineNumber", "columnNumber", "requestId")
        if key in raw
    }


def post_body_of(request: Mapping[str, Any]) -> tuple[bytes, str] | None:
    """A CDP ``Request``'s POST body and the field it came from, if it carries one.

    ``postDataEntries`` (base64, every part) wins over ``postData`` (text,
    lossy for binary bodies).
    """
    entries = request.get("postDataEntries")
    if isinstance(entries, list) and any(
        isinstance(e, Mapping) and e.get("bytes") for e in entries
    ):
        body = b"".join(
            base64.b64decode(e["bytes"])
            for e in entries
            if isinstance(e, Mapping) and isinstance(e.get("bytes"), str)
        )
        return body, "postDataEntries"
    if isinstance(request.get("postData"), str):
        return request["postData"].encode("utf-8"), "postData"
    return None


@dataclass(frozen=True)
class HeldBytes:
    """What :func:`admit_network_event` held back from one event's trip through the scope."""

    #: ``requestWillBeSent``'s POST body and where it came from.
    post: tuple[bytes, str] | None = None
    #: A WebSocket frame's ``payloadData`` (text; base64 for a binary frame).
    payload: str | None = None


#: The ``Network.Response`` / ``WebSocketResponse`` fields that repeat the raw
#: *request* headers (cookies and credentials included). Never stored.
RESPONSE_REQUEST_HEADER_FIELDS = ("requestHeaders", "requestHeadersText")


def _without_request_headers(response: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in response.items() if k not in RESPONSE_REQUEST_HEADER_FIELDS}


def admit_network_event(
    method: str, params: Mapping[str, Any], *, strip: frozenset[str]
) -> tuple[dict[str, Any], HeldBytes | None]:
    """*params* as they may travel through the archive scope, and the bytes held back.

    The admission half of ``NetworkSession`` (``RecorderParticipant.admit_event``):
    it runs in the recorder's receive loop, before the event enters the
    archive scope, whose history keeps the last 1000 events (§4.14). Bytes —
    the POST body, a frame's payload — come back as :class:`HeldBytes` for the
    session's side store. What this archiver never stores does not travel at
    all: stripped request headers keep their names (the model drops them into
    the ledger) but lose their values; ``associatedCookies``,
    ``blockedCookies`` and ``exemptedCookies`` are left out with everything
    else the ExtraInfo handlers do not read; a response's raw request headers
    are dropped. *params* itself is never modified.
    """
    slim = dict(params)
    held: HeldBytes | None = None
    if method == "Network.requestWillBeSent":
        request = dict(slim.get("request") or {})
        post = post_body_of(request)
        request.pop("postData", None)
        request.pop("postDataEntries", None)
        request["headers"] = blank_stripped_headers(request.get("headers") or {}, strip)
        slim["request"] = request
        if isinstance(slim.get("redirectResponse"), Mapping):
            slim["redirectResponse"] = _without_request_headers(slim["redirectResponse"])
        if post is not None:
            held = HeldBytes(post=(post[0], f"requestWillBeSent.{post[1]}"))
    elif method == "Network.requestWillBeSentExtraInfo":
        slim = {
            "requestId": params.get("requestId"),
            "headers": blank_stripped_headers(params.get("headers") or {}, strip),
        }
    elif method == "Network.responseReceivedExtraInfo":
        slim = {
            "requestId": params.get("requestId"),
            "statusCode": params.get("statusCode"),
            "headers": params.get("headers") or {},
        }
    elif method in ("Network.responseReceived", "Network.webSocketHandshakeResponseReceived"):
        if isinstance(slim.get("response"), Mapping):
            slim["response"] = _without_request_headers(slim["response"])
    elif method == "Network.webSocketWillSendHandshakeRequest":
        request = dict(slim.get("request") or {})
        request["headers"] = blank_stripped_headers(request.get("headers") or {}, strip)
        slim["request"] = request
    elif method in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
        if isinstance(slim.get("response"), Mapping):
            response = dict(slim["response"])
            payload = response.pop("payloadData", None)
            slim["response"] = response
            if payload is not None:
                held = HeldBytes(payload=str(payload))
    return slim, held


@dataclass
class Exchange:
    """One request hop and everything recorded about it."""

    record_id: str
    network_id: str
    hop: int
    url: str
    method: str = ""
    url_fragment: str | None = None
    resource_type: str | None = None
    frame_id: str | None = None
    loader_id: str | None = None
    document_url: str | None = None
    #: The recorder session (and its target) that reported the request.
    session_id: str | None = None
    target_id: str | None = None
    target_type: str | None = None
    initiator: dict[str, Any] | None = None
    #: Epoch seconds of ``requestWillBeSent`` (or of the pause, before it).
    started_at: float | None = None
    #: Epoch seconds the request ended: its terminal event (``loadingFinished``,
    #: ``loadingFailed``, the next hop's redirect, a target detach) arrived.
    #: ``None`` until then.
    ended_at: float | None = None
    #: ``wallTime - timestamp`` of the request, to put CDP's monotonic
    #: timestamps on the wall clock.
    wall_offset: float | None = None
    has_request_event: bool = False
    #: The request headers — Fetch-visible ones when the request paused —
    #: with the stripped names already gone.
    request_headers: dict[str, Any] = field(default_factory=dict)
    #: ``requestWillBeSentExtraInfo.headers``, stripped; ``None`` until paired.
    raw_request_headers: dict[str, Any] | None = None
    has_post_data: bool = False
    #: The POST body after :func:`redact_post_body`; ``None`` without one.
    post_body: bytes | None = None
    post_content_type: str | None = None
    #: Where the POST body came from (``postDataEntries`` / ``postData`` /
    #: ``getRequestPostData``), or why it is missing.
    post_source: str | None = None
    redactions: list[RedactionEntry] = field(default_factory=list)

    # response ------------------------------------------------------------
    status: int | None = None
    status_text: str = ""
    response_headers: Any = None
    raw_response_headers: dict[str, Any] | None = None
    expects_extra_info: bool = False
    mime_type: str | None = None
    protocol: str | None = None
    remote_address: str | None = None
    from_service_worker: bool = False
    from_disk_cache: bool = False
    served_from_cache: bool = False
    timing: dict[str, Any] | None = None
    encoded_length: float | None = None

    # pause and body --------------------------------------------------------
    #: The recorder session that paused it, and that session's target.
    paused_session: str | None = None
    paused_target: str | None = None
    #: The pause's response status (redirect hops pause too).
    paused_status: int | None = None
    body_reading: bool = False
    body: bytes | None = None
    body_status: BodyStatus | None = None
    body_note: str | None = None
    content_range: dict[str, Any] | None = None

    # end -------------------------------------------------------------------
    terminal: Terminal | None = None
    error_text: str | None = None
    redirected_from: str | None = None
    is_websocket: bool = False
    #: Whether ``responseReceived`` (or the redirect response) was seen.
    has_response_event: bool = False
    #: Whether the exchange already spent its one collection of grace.
    grace_used: bool = False
    written: bool = False
    #: Wall-clock seconds of the latest thing recorded about it.
    captured_at: float = 0.0

    @property
    def over(self) -> bool:
        """Nothing more that would be written can arrive: it ended, or its body is read."""
        return self.terminal not in (None, "redirected") or (
            self.paused_session is not None and self.body_status is not None
        )

    def final_body_status(self) -> BodyStatus:
        """What the exchange line says about the body, once settled."""
        if self.is_websocket:
            return "websocket" if self.status == 101 else "incomplete"
        if self.terminal == "redirected":
            return "redirect"
        if self.body_status is not None:
            return self.body_status
        empty_by_protocol = self.method == "HEAD" or (
            self.status is not None and (self.status in (204, 304) or 100 <= self.status < 200)
        )
        if self.terminal == "finished" and empty_by_protocol:
            return "ok"
        return "incomplete"

    def waiting_for(self) -> str | None:
        """What the exchange still waits for, or ``None`` when it can be written.

        A body read at the Fetch Response stage settles the exchange without
        ``loadingFinished``: a ``fetch()`` whose body the page never consumes
        gets no ``loadingFinished`` at all (measured, Chrome 155). The read
        answers with the bytes that arrived, also when the download then
        fails; that failure reaches the page with its response, so a
        ``loadingFailed`` the exchange gets before it is written makes the
        body incomplete (:meth:`NetworkModel.loading_failed`). Two waits are
        only worth one collection (:data:`GRACE_WAITS`): the
        ``responseReceived`` event of such an exchange, and the raw response
        headers of any.
        """
        if self.is_websocket:
            return None if self.terminal is not None else "handshake"
        if self.body_reading:
            return "body"
        if self.terminal is None:
            if self.paused_session is None or self.body_status is None:
                return "response" if self.status is None else "loading"
            if not self.has_response_event:
                return "response_event"
        if (
            self.expects_extra_info
            and self.raw_response_headers is None
            and not self.served_from_cache
        ):
            return "raw_headers"
        return None


#: Waits an exchange is held back for at most one collection: the events behind
#: them are already on their way when a collection starts (its barrier drains
#: everything emitted before it), so one more collection is ample, and a
#: Chrome that never sends them must not hold the exchange back for ever.
GRACE_WAITS = frozenset({"response_event", "raw_headers"})


@dataclass(frozen=True)
class Settled:
    """One collection's hand-over: exchanges to write, pending ones, WebSocket frames."""

    exchanges: tuple[Exchange, ...]
    pending: tuple[Exchange, ...]
    frames: tuple[dict[str, Any], ...]


class NetworkModel:
    """Pairs the recorder's network events into :class:`Exchange` s.

    Args:
        strip: Lower-cased request header names never stored.
        target_of: The recorder target a session belongs to (``None`` when
            unknown — the session id then stands for its own target).
        clock: Wall-clock seconds.
    """

    def __init__(
        self,
        *,
        strip: frozenset[str],
        target_of: Callable[[str], RecorderTarget | None],
        clock: Callable[[], float],
    ) -> None:
        self._strip_names = strip
        self._target_of = target_of
        self._clock = clock
        #: network request id → its hops, in order.
        self._chains: dict[str, list[Exchange]] = {}
        #: request id → {target id → the session whose events are taken}.
        self._event_owner: dict[str, dict[str, str]] = {}
        #: request id → ExtraInfo events not paired yet, in arrival order.
        self._request_extra: dict[str, list[dict[str, Any]]] = {}
        self._response_extra: dict[str, list[dict[str, Any]]] = {}
        #: Request ids Chrome reported served from cache (no ExtraInfo: crbug 1340398).
        self._from_cache: set[str] = set()
        #: WebSocket request id → its exchange.
        self._sockets: dict[str, Exchange] = {}
        self._frames: list[dict[str, Any]] = []

    # ── helpers ───────────────────────────────────────────────────────

    def _target_id(self, session_id: str | None) -> tuple[str, str | None]:
        """(target id, target type) of a session; the session stands in if unknown."""
        if session_id is None:
            return "", None
        target = self._target_of(session_id)
        if target is None:
            return session_id, None
        return target.target_id, target.type

    def _accepted(self, session_id: str | None, request_id: str) -> bool:
        """Whether this session's events of *request_id* are the ones taken."""
        target_id, _ = self._target_id(session_id)
        owners = self._event_owner.setdefault(request_id, {})
        return owners.setdefault(target_id, session_id or "") == (session_id or "")

    def _new_hop(self, network_id: str, url: str) -> Exchange:
        chain = self._chains.setdefault(network_id, [])
        hop = Exchange(record_id=_new_record_id(), network_id=network_id, hop=len(chain), url=url)
        chain.append(hop)
        return hop

    def _current(self, request_id: str) -> Exchange | None:
        """The hop of *request_id* that events are still arriving for."""
        for hop in reversed(self._chains.get(request_id, [])):
            if hop.has_request_event and hop.terminal is None:
                return hop
        return None

    def _touch(self, hop: Exchange) -> None:
        hop.captured_at = self._clock()

    def _stripped(self, hop: Exchange, headers: Any, location: str) -> Any:
        """*headers* without the stripped names; the names go to the hop's ledger once."""
        kept, entries = strip_headers(headers, self._strip_names, location=location)
        known = {(e.location, e.field) for e in hop.redactions}
        hop.redactions.extend(e for e in entries if (e.location, e.field) not in known)
        return kept

    def _set_post_body(self, hop: Exchange, post: tuple[bytes, str] | None) -> None:
        """Store *post* (body, source) unless the hop already has its body."""
        if hop.post_body is None and post is not None:
            self.set_post_body(hop, *post)

    def set_post_body(self, hop: Exchange, body: bytes, source: str) -> None:
        """Store a POST body, redacted (``redact_post_body``), with its ledger."""
        content_type = header_value(hop.request_headers, "content-type")
        redacted, entries = redact_post_body(body, content_type)
        hop.post_body = redacted
        hop.post_content_type = content_type
        hop.post_source = source
        hop.redactions.extend(
            RedactionEntry(location=f"/request/post{e.location}", field=e.field, rule=e.rule)
            for e in entries
        )

    # ── Network events (synchronous, archive-scope loop) ─────────────

    def request_will_be_sent(
        self,
        session_id: str | None,
        params: Mapping[str, Any],
        *,
        post: tuple[bytes, str] | None = None,
    ) -> None:
        """One request hop; *post* is its POST body held back at admission, with its source."""
        request = params.get("request") or {}
        url = str(request.get("url") or "")
        request_id = str(params.get("requestId") or "")
        if not request_id or not recordable(url) or not self._accepted(session_id, request_id):
            return
        redirect = params.get("redirectResponse")
        previous = self._current(request_id) if redirect is not None else None
        if previous is not None and isinstance(redirect, Mapping):
            self._apply_response(
                previous, redirect, has_extra=bool(params.get("redirectHasExtraInfo"))
            )
            previous.terminal = "redirected"
            previous.ended_at = self.end_time(previous, params.get("timestamp"))
            self._touch(previous)
        chain = self._chains.get(request_id, [])
        hop = next((h for h in chain if not h.has_request_event and h.url == url), None)
        if hop is None:
            hop = self._new_hop(request_id, url)
        if previous is not None:
            hop.redirected_from = previous.record_id
        target_id, target_type = self._target_id(session_id)
        hop.has_request_event = True
        hop.method = str(request.get("method") or hop.method)
        hop.url_fragment = request.get("urlFragment") or None
        hop.resource_type = params.get("type") or hop.resource_type
        hop.frame_id = params.get("frameId") or hop.frame_id
        hop.loader_id = params.get("loaderId") or None
        hop.document_url = params.get("documentURL") or None
        hop.session_id = session_id
        hop.target_id = target_id
        hop.target_type = target_type
        hop.initiator = _initiator(params.get("initiator"))
        wall, stamp = params.get("wallTime"), params.get("timestamp")
        if isinstance(wall, int | float):
            hop.started_at = float(wall)
            if isinstance(stamp, int | float):
                hop.wall_offset = float(wall) - float(stamp)
        hop.has_post_data = hop.has_post_data or bool(request.get("hasPostData"))
        if hop.paused_session is None:
            # Fetch-visible headers win when the request paused (measured, arch
            # §4.14 "回放实测": they are what replay's Request stage sees too).
            hop.request_headers = self._stripped(
                hop, request.get("headers") or {}, "/request/headers"
            )
        self._set_post_body(hop, post)
        self._touch(hop)

    def request_will_be_sent_extra_info(
        self, session_id: str | None, params: Mapping[str, Any]
    ) -> None:
        request_id = str(params.get("requestId") or "")
        if not request_id or not self._accepted(session_id, request_id):
            return
        # Only the headers: ``associatedCookies`` (cookie values) is never stored.
        self._request_extra.setdefault(request_id, []).append(
            {"headers": params.get("headers") or {}}
        )

    def response_received(self, session_id: str | None, params: Mapping[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        if not request_id or not self._accepted(session_id, request_id):
            return
        hop = self._current(request_id)
        response = params.get("response")
        if hop is None or not isinstance(response, Mapping):
            return  # e.g. a file upload's response, or a data: URL
        self._apply_response(hop, response, has_extra=bool(params.get("hasExtraInfo")))
        hop.resource_type = params.get("type") or hop.resource_type
        hop.frame_id = params.get("frameId") or hop.frame_id
        self._touch(hop)

    def response_received_extra_info(
        self, session_id: str | None, params: Mapping[str, Any]
    ) -> None:
        request_id = str(params.get("requestId") or "")
        if not request_id or not self._accepted(session_id, request_id):
            return
        # Only the status and the headers: ``blockedCookies`` /
        # ``exemptedCookies`` values are never stored.
        self._response_extra.setdefault(request_id, []).append(
            {"statusCode": params.get("statusCode"), "headers": params.get("headers") or {}}
        )

    def request_served_from_cache(self, session_id: str | None, params: Mapping[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        if request_id and self._accepted(session_id, request_id):
            self._from_cache.add(request_id)

    def loading_finished(self, session_id: str | None, params: Mapping[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        if not request_id or not self._accepted(session_id, request_id):
            return
        hop = self._current(request_id)
        if hop is None:
            return
        hop.terminal = "finished"
        hop.ended_at = self.end_time(hop, params.get("timestamp"))
        length = params.get("encodedDataLength")
        hop.encoded_length = float(length) if isinstance(length, int | float) else None
        self._touch(hop)

    def loading_failed(self, session_id: str | None, params: Mapping[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        if not request_id or not self._accepted(session_id, request_id):
            return
        hop = self._current(request_id)
        if hop is None:
            return
        hop.terminal = "failed"
        hop.ended_at = self.end_time(hop, params.get("timestamp"))
        hop.error_text = failure_text(params)
        if hop.body_status == "ok" and hop.paused_session is not None and _network_failure(params):
            # Fetch.getResponseBody answered with the bytes that arrived before
            # the download failed: what the page got was the failure.
            self.body_finished(
                hop,
                status="incomplete",
                note=f"{FAILED_AFTER_READ_NOTE} ({hop.error_text})",
                content_range=hop.content_range,
            )
        self._touch(hop)

    def target_detached(self, session_id: str | None) -> None:
        """A target went away: what it was still loading will never finish."""
        if session_id is None:
            return
        for chain in self._chains.values():
            for hop in chain:
                if hop.session_id == session_id and hop.terminal is None and not hop.written:
                    hop.terminal = "failed"
                    hop.ended_at = self._clock()
                    hop.error_text = TARGET_DETACHED_ERROR
                    self._touch(hop)

    # ── WebSocket events ─────────────────────────────────────────────

    def websocket_created(self, session_id: str | None, params: Mapping[str, Any]) -> None:
        request_id = str(params.get("requestId") or "")
        url = str(params.get("url") or "")
        if not request_id or not recordable(url) or not self._accepted(session_id, request_id):
            return
        if request_id in self._sockets:
            return
        hop = self._new_hop(request_id, url)
        hop.is_websocket = True
        hop.has_request_event = True
        hop.method = "GET"
        hop.resource_type = "WebSocket"
        hop.session_id = session_id
        hop.target_id, hop.target_type = self._target_id(session_id)
        hop.initiator = _initiator(params.get("initiator"))
        self._sockets[request_id] = hop
        self._touch(hop)

    def websocket_handshake_request(
        self, session_id: str | None, params: Mapping[str, Any]
    ) -> None:
        hop = self._socket(session_id, params)
        if hop is None:
            return
        wall, stamp = params.get("wallTime"), params.get("timestamp")
        if isinstance(wall, int | float):
            hop.started_at = float(wall)
            if isinstance(stamp, int | float):
                hop.wall_offset = float(wall) - float(stamp)
        request = params.get("request") or {}
        hop.request_headers = self._stripped(hop, request.get("headers") or {}, "/request/headers")
        self._touch(hop)

    def websocket_handshake_response(
        self, session_id: str | None, params: Mapping[str, Any]
    ) -> None:
        hop = self._socket(session_id, params)
        response = params.get("response")
        if hop is None or not isinstance(response, Mapping):
            return
        status = response.get("status")
        hop.status = int(status) if isinstance(status, int | float) else None
        hop.status_text = str(response.get("statusText") or "")
        hop.response_headers = dict(response.get("headers") or {})
        hop.terminal = "websocket"
        self._touch(hop)

    def websocket_frame(
        self,
        session_id: str | None,
        params: Mapping[str, Any],
        *,
        direction: str,
        payload: str | None = None,
    ) -> None:
        """One frame; *payload* is its ``payloadData``, held back at admission."""
        hop = self._socket(session_id, params)
        if hop is None:
            return
        line: dict[str, Any] = {
            "record_id": hop.record_id,
            "direction": direction,
            "time": self._wall(hop, params.get("timestamp")),
        }
        response = params.get("response")
        if isinstance(response, Mapping):
            opcode = response.get("opcode")
            line["opcode"] = opcode
            # Text frames (opcode 1) carry text; binary frames (2) base64.
            line["payload_base64" if opcode == 2 else "payload"] = payload
        if direction == "error":
            line["error"] = params.get("errorMessage")
        self._frames.append(line)

    def websocket_closed(self, session_id: str | None, params: Mapping[str, Any]) -> None:
        hop = self._socket(session_id, params)
        if hop is None:
            return
        self._frames.append(
            {
                "record_id": hop.record_id,
                "direction": "closed",
                "time": self._wall(hop, params.get("timestamp")),
            }
        )
        if hop.terminal is None:
            hop.terminal = "failed"
            hop.error_text = "closed before the handshake was answered"
        self._touch(hop)
        # No frame follows a close: the socket's bookkeeping can go.
        self._sockets.pop(hop.network_id, None)

    def _socket(self, session_id: str | None, params: Mapping[str, Any]) -> Exchange | None:
        request_id = str(params.get("requestId") or "")
        if not request_id or not self._accepted(session_id, request_id):
            return None
        return self._sockets.get(request_id)

    @staticmethod
    def _wall(hop: Exchange, stamp: Any) -> float | None:
        if hop.wall_offset is None or not isinstance(stamp, int | float):
            return None
        return hop.wall_offset + float(stamp)

    def end_time(self, hop: Exchange, stamp: Any) -> float:
        """When *hop*'s terminal event happened: its CDP timestamp on the wall clock, else now."""
        wall = self._wall(hop, stamp)
        return wall if wall is not None else self._clock()

    # ── pauses (the session's on_paused) ─────────────────────────────

    def pause_started(
        self, *, session_id: str, target_id: str, params: Mapping[str, Any]
    ) -> Exchange | None:
        """Attach a Response-stage pause to its hop; ``None`` when it records nothing.

        ``None`` for a pause the inspector does not know (no ``networkId``),
        for a non-network URL, and for the same hop paused again by a second
        session of the same target.
        """
        network_id = params.get("networkId")
        request = params.get("request") or {}
        url = str(request.get("url") or "")
        if network_id is None or not recordable(url):
            return None
        network_id = str(network_id)
        status = params.get("responseStatusCode")
        status = int(status) if isinstance(status, int | float) else None
        chain = self._chains.get(network_id, [])
        if any(
            h.url == url
            and h.paused_target == target_id
            and h.paused_session != session_id
            and h.paused_status == status
            for h in chain
        ):
            return None  # the same hop, paused once per session of one target
        hop = next((h for h in chain if h.url == url and h.paused_session is None), None)
        if hop is None:
            hop = self._new_hop(network_id, url)
        hop.paused_session = session_id
        hop.paused_target = target_id
        hop.paused_status = status
        hop.body_reading = True
        hop.method = str(request.get("method") or hop.method)
        hop.resource_type = params.get("resourceType") or hop.resource_type
        hop.frame_id = params.get("frameId") or hop.frame_id
        if hop.started_at is None:
            hop.started_at = self._clock()
        hop.has_post_data = hop.has_post_data or bool(request.get("hasPostData"))
        hop.request_headers = self._stripped(hop, request.get("headers") or {}, "/request/headers")
        if status is not None:
            hop.status = status
            hop.status_text = str(params.get("responseStatusText") or hop.status_text)
            hop.response_headers = list(params.get("responseHeaders") or [])
        # The paused request is the side path (a task, never the scope): its
        # body comes straight from the pause.
        post = post_body_of(request)
        self._set_post_body(hop, (post[0], f"requestPaused.{post[1]}") if post else None)
        self._touch(hop)
        return hop

    def body_finished(
        self,
        hop: Exchange,
        *,
        status: BodyStatus | None,
        body: bytes | None = None,
        note: str | None = None,
        content_range: dict[str, Any] | None = None,
    ) -> None:
        """The pause's body handling is over (``status`` ``None``: a redirect, no body)."""
        hop.body_reading = False
        hop.body_status = status
        hop.body = body
        hop.body_note = note
        hop.content_range = content_range
        self._touch(hop)

    # ── response details ─────────────────────────────────────────────

    def _apply_response(
        self, hop: Exchange, response: Mapping[str, Any], *, has_extra: bool
    ) -> None:
        status = response.get("status")
        if isinstance(status, int | float):
            hop.status = int(status)
        hop.has_response_event = True
        hop.status_text = str(response.get("statusText") or hop.status_text)
        if hop.paused_session is None or hop.response_headers is None:
            hop.response_headers = dict(response.get("headers") or {})
        hop.expects_extra_info = has_extra
        hop.mime_type = response.get("mimeType") or hop.mime_type
        hop.protocol = response.get("protocol") or hop.protocol
        ip, port = response.get("remoteIPAddress"), response.get("remotePort")
        if ip:
            hop.remote_address = f"{ip}:{port}" if port is not None else str(ip)
        hop.from_service_worker = bool(response.get("fromServiceWorker"))
        hop.from_disk_cache = bool(response.get("fromDiskCache"))
        timing = response.get("timing")
        hop.timing = dict(timing) if isinstance(timing, Mapping) else None

    def _pair_extra_info(self, hop: Exchange) -> None:
        """Give a hop its raw request / response headers from the ExtraInfo events.

        ExtraInfo arrives on its own channel, in any order relative to the
        events it describes; per request id the n-th response that expects
        it gets the n-th event (Playwright's ``ResponseExtraInfoTracker``).
        The response side is matched on ``statusCode`` where Chrome sent one.
        """
        request_id = hop.network_id
        if hop.served_from_cache or request_id in self._from_cache:
            hop.served_from_cache = True
        if hop.raw_request_headers is None:
            pending = self._request_extra.get(request_id) or []
            if pending:
                extra = pending.pop(0)
                hop.raw_request_headers = self._stripped(
                    hop, extra.get("headers") or {}, "/request/raw_headers"
                )
        if (
            hop.raw_response_headers is None
            and hop.expects_extra_info
            and not hop.served_from_cache
        ):
            pending = self._response_extra.get(request_id) or []
            for index, extra in enumerate(pending):
                code = extra.get("statusCode")
                if code is None or hop.status is None or code == hop.status:
                    hop.raw_response_headers = dict(extra.get("headers") or {})
                    del pending[index]
                    break

    # ── the hand-over ────────────────────────────────────────────────

    def take(self) -> Settled:
        """Everything that settled since the last call, the pending rest, the frames.

        Synchronous on purpose — no ``await`` — so the hand-over is atomic for
        every producer on the loop: an event handled after this returns lands
        in the next hand-over, never in both or neither. A settled exchange is
        marked written and never returned again; its chain is forgotten once
        every hop of it is written and the request is over.
        """
        settled: list[Exchange] = []
        pending: list[Exchange] = []
        for request_id in list(self._chains):
            chain = self._chains[request_id]
            for hop in chain:
                if hop.written:
                    continue
                self._pair_extra_info(hop)
                waiting = hop.waiting_for()
                if waiting in GRACE_WAITS and not hop.grace_used:
                    hop.grace_used = True
                    pending.append(hop)
                elif waiting is None or waiting in GRACE_WAITS:
                    hop.written = True
                    settled.append(hop)
                else:
                    pending.append(hop)
            if chain and all(h.written for h in chain) and chain[-1].over:
                self._forget(request_id)
        frames, self._frames = self._frames, []
        return Settled(tuple(settled), tuple(pending), tuple(frames))

    def _forget(self, request_id: str) -> None:
        """Drop a finished request's bookkeeping (an open socket keeps its frames)."""
        self._chains.pop(request_id, None)
        self._request_extra.pop(request_id, None)
        self._response_extra.pop(request_id, None)
        self._from_cache.discard(request_id)
        if request_id not in self._sockets:
            self._event_owner.pop(request_id, None)

    @property
    def open_exchanges(self) -> int:
        """Exchanges not written yet (diagnostics and tests)."""
        return sum(1 for chain in self._chains.values() for hop in chain if not hop.written)
