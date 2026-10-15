"""A scripted recorder handle for archive-session tests (no capability, no scopes).

``BrowserArchiveSession`` s talk to the recorder connection only through the
:class:`~spectragent.core.capabilities.browser.archive.base.RecorderHandle`
surface; :class:`ScriptedRecorder` implements it in memory so a session can be
driven event by event and pause by pause, deterministically: events are
handed to ``session.on_event`` directly, pauses to ``session.on_paused``.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from spectragent.core.capabilities.browser.archive.base import (
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
    RawCaptureStore,
    ReplayLogFeed,
)
from spectragent.core.capabilities.browser.recorder import (
    PausedRequest,
    RecorderEvent,
    RecorderTarget,
)


@dataclass
class ScriptedRecorder:
    """The recorder connection, scripted."""

    targets_by_session: dict[str, RecorderTarget] = field(default_factory=dict)
    #: Fetch interception id → body.
    bodies: dict[str, bytes] = field(default_factory=dict)
    #: Network request id → POST body for ``Network.getRequestPostData``.
    post_data: dict[str, bytes] = field(default_factory=dict)
    #: Network request id → ``bufferedData`` of ``Network.streamResourceContent``
    #: (what arrived before streaming started; empty unless set).
    buffered: dict[str, bytes] = field(default_factory=dict)
    #: method → exception.
    failures: dict[str, BaseException] = field(default_factory=dict)
    #: method → event the reply waits for.
    holds: dict[str, asyncio.Event] = field(default_factory=dict)
    sent: list[tuple[str, dict[str, Any], str | None]] = field(default_factory=list)

    def add_target(self, session_id: str, target_id: str, type_: str = "page") -> RecorderTarget:
        target = RecorderTarget(session_id=session_id, target_id=target_id, type=type_)
        self.targets_by_session[session_id] = target
        return target

    def target(self, session_id: str) -> RecorderTarget | None:
        return self.targets_by_session.get(session_id)

    def targets(self) -> dict[str, RecorderTarget]:
        return dict(self.targets_by_session)

    def session_for_target(self, target_id: str) -> str | None:
        for sid, target in self.targets_by_session.items():
            if target.target_id == target_id:
                return sid
        return None

    async def browser_version(self) -> dict[str, Any]:
        return {"product": "Chrome/155.0", "userAgent": "ScriptedChrome/1.0"}

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.sent]

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        params = dict(params or {})
        self.sent.append((method, params, session_id))
        held = self.holds.get(method)
        if held is not None:
            if timeout is None:
                await held.wait()
            else:
                try:
                    await asyncio.wait_for(held.wait(), timeout)
                except TimeoutError:
                    raise TimeoutError(f"CDP {method} got no reply within {timeout:g}s") from None
        if method in self.failures:
            raise self.failures[method]
        if method == "Fetch.getResponseBody":
            body = self.bodies.get(str(params.get("requestId")), b"")
            return {"body": base64.b64encode(body).decode(), "base64Encoded": True}
        if method == "Network.getRequestPostData":
            data = self.post_data.get(str(params.get("requestId")))
            if data is None:
                raise RuntimeError("CDP error: No post data available for the request")
            return {"postData": data.decode()}
        if method == "Network.streamResourceContent":
            buffered = self.buffered.get(str(params.get("requestId")), b"")
            return {"bufferedData": base64.b64encode(buffered).decode()}
        return {}


def scripted_context(
    recorder: ScriptedRecorder,
    *,
    fragments: frozenset[str] = frozenset({"browser.dom"}),
    parked: list[Any] | None = None,
    cycle: int = 1,
    fresh_profile: bool = True,
    replay_log: ReplayLogFeed | None = None,
    clock: Callable[[], float] | None = None,
) -> BrowserArchiveContext:
    """A context around *recorder*; ``parked`` collects ``park_raw`` calls."""
    sink = parked if parked is not None else []
    extra: dict[str, Any] = {"clock": clock} if clock is not None else {}
    return BrowserArchiveContext(
        recorder=recorder,
        channel=None,  # type: ignore[arg-type] — sessions under test never use it
        raw_captures=RawCaptureStore(),
        park_raw=lambda sid, capture, dpr, request_id: sink.append((sid, capture, dpr)),
        fragments=fragments,
        viewport=(1280, 720),
        cycle=lambda: cycle,
        fresh_profile=fresh_profile,
        replay_log=replay_log,
        **extra,
    )


def open_session(
    archiver: BrowserArchiverBase, recorder: ScriptedRecorder | None = None, **context: Any
) -> tuple[BrowserArchiveSession, ScriptedRecorder]:
    recorder = recorder or ScriptedRecorder()
    return archiver.open(scripted_context(recorder, **context)), recorder


def event(method: str, params: dict[str, Any], session_id: str = "page-s") -> RecorderEvent:
    """A raw CDP event as the recorder's client receives it (hand it over with :func:`deliver`)."""
    return RecorderEvent(session_id=session_id, method=method, params=params)


def deliver(session: BrowserArchiveSession, raw: RecorderEvent) -> None:
    """Hand *raw* to *session* the way the recorder and the ArchiveWatchdog do.

    An event the session declared goes through its admission first (the
    recorder's dispatcher, before the archive scope) and reaches ``on_event``
    only as admitted; target bookkeeping reaches ``on_event`` as it is.
    """
    if raw.method in session.event_methods():
        admitted = session.admit_event(raw.method, raw.params, raw.session_id)
        if admitted is not None:
            session.on_event(admitted)
        return
    session.on_event(raw)


def stream_chunks(
    session: BrowserArchiveSession,
    request_id: str,
    body: bytes,
    *,
    session_id: str = "page-s",
    chunk_bytes: int = 1024,
    encoded: int | None = None,
) -> None:
    """*body* arriving as ``Network.dataReceived`` chunks, as Chrome streams it.

    *encoded* is the bytes on the wire spread over the chunks (a compressed
    body's); by default each chunk's own length.
    """
    pieces = [body[i : i + chunk_bytes] for i in range(0, len(body), chunk_bytes)] or [b""]
    wire = [len(piece) for piece in pieces]
    if encoded is not None:
        wire = [encoded // len(pieces)] * len(pieces)
        wire[-1] += encoded - sum(wire)
    for piece, on_wire in zip(pieces, wire, strict=True):
        params: dict[str, Any] = {
            "requestId": request_id,
            "timestamp": 10.5,
            "dataLength": len(piece),
            "encodedDataLength": on_wire,
        }
        if piece:
            params["data"] = base64.b64encode(piece).decode()
        deliver(session, event("Network.dataReceived", params, session_id))


def paused(
    *,
    request_id: str,
    network_id: str | None,
    url: str,
    status: int | None = 200,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    resource_type: str = "XHR",
    session_id: str = "page-s",
    target: RecorderTarget | None = None,
    request_headers: dict[str, str] | None = None,
    post_data_entries: list[bytes] | None = None,
    has_post_data: bool | None = None,
) -> PausedRequest:
    request: dict[str, Any] = {
        "url": url,
        "method": method,
        "headers": request_headers or {"Accept": "*/*"},
    }
    if post_data_entries is not None:
        request["postDataEntries"] = [
            {"bytes": base64.b64encode(chunk).decode()} for chunk in post_data_entries
        ]
    if has_post_data is not None or post_data_entries is not None:
        request["hasPostData"] = bool(has_post_data if has_post_data is not None else True)
    params: dict[str, Any] = {
        "requestId": request_id,
        "request": request,
        "frameId": "frame-1",
        "resourceType": resource_type,
    }
    if network_id is not None:
        params["networkId"] = network_id
    if status is not None:
        params["responseStatusCode"] = status
        params["responseHeaders"] = [
            {"name": k, "value": v} for k, v in (headers or {"Content-Type": "text/plain"}).items()
        ]
    return PausedRequest(
        session_id=session_id,
        request_id=request_id,
        stage="Response" if status is not None else "Request",
        params=params,
        target=target,
    )
