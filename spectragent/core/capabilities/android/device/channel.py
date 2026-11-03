"""The channel between the host and a device-side program: framed messages over one stream.

``docs/architecture_design.md`` §11.2 ("主机与设备之间的通道"). Both device-side
programs, the accessibility service in our APK and the shell helper started
from it, listen on an abstract Unix socket. The host reaches one through the
run's adb server (:meth:`~spectragent.core.capabilities.android.adb.AdbClient.connect_abstract`)
and speaks this protocol on that stream. One number, :data:`PROTOCOL_VERSION`,
versions all of it, and the APK's build reads it from this file
(``android/service/build.gradle``), so both ends are built from one constant.

**Frames.** Every message, either way, is one frame::

    u32 length | u32 header length | header | payload

Both numbers are big-endian, and *length* counts everything after itself. The
header is one JSON object in UTF-8. The payload is raw bytes (a screenshot's
PNG) and usually empty. A frame is at most :data:`MAX_FRAME_BYTES`.

**The hello.** The first frame each way is the hello, whose shape never
changes, so two ends of different versions can always tell each other so.
The host sends ``{"kind": "hello", "protocol": N, "token": T}``. The device
answers ``{"kind": "hello", "protocol": N, ...}``, which describes it
(:class:`Hello`), or ``{"kind": "refused", "reason": R, "message": M}`` and
closes; *R* is ``protocol`` or ``token``. The host refuses a device of
another protocol version as well. The token is the session's secret, handed
to the device over adb shell and never over this socket: the service gets it
through its bootstrap provider, the helper on its stdin.

**Requests.** ``{"kind": "request", "id": I, "method": M, "params": {...}}``
is answered ``{"kind": "response", "id": I, "result": {...}}``, with a
payload when the method has one, or ``{"kind": "response", "id": I, "error":
{"code": C, "message": M}}``. Ids are the host's, increasing per channel. The
device answers one connection's requests one at a time, in order. An answer
whose caller gave up (cancelled, timed out) is dropped by its id; the device
still finishes that request before it starts the next one on the connection.
Until that answer arrives the channel counts the request as abandoned
(:attr:`Channel.abandoned`): it may still be running on the device, and any
request answered after it was not.

**Events.** After ``subscribe``, answered ``{"next_seq": S}``, the service
pushes ``{"kind": "event", "seq": S, ...}`` frames on the same stream,
numbered without gaps from *S*. Events and answers leave the device through
one ordered queue, so every event the service received before it answered a
request arrives before that answer. No event is dropped silently. The device
queues what the socket cannot take yet; should its queue outgrow its limit
(the host stopped reading), it sends ``{"kind": "overflow", "next_seq": S}``
in place of event *S* and closes. The host reads the stream as it comes,
whatever its consumer does, so answers never wait behind unread events; a
consumer that falls more than ``max_buffered`` events behind gets the same
explicit end. Either way the consumer receives every event before the end, in
order, then :class:`EventsLost`.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import struct
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

#: The version of everything this module describes. Change it with any change
#: to a frame, a method or a result the two ends exchange.
PROTOCOL_VERSION = 9
#: The largest frame either end sends or accepts.
MAX_FRAME_BYTES = 64 * 1024 * 1024
_LENGTHS = struct.Struct(">II")

JsonObject = dict[str, JsonValue]
Component = Literal["service", "helper"]


class ChannelError(Exception):
    """The channel to a device-side program failed; the subclass says how."""


class FrameError(ChannelError):
    """A frame broke the protocol: too long, cut short, or a header that is not a JSON object."""


class ChannelClosed(ChannelError):
    """The stream ended or broke; every call still waiting gets this."""


class HandshakeRefused(ChannelError):
    """The hello was refused, by the device or by this host.

    Attributes:
        reason: ``protocol`` (the two ends are of different versions),
            ``token`` (not this session's secret) or ``component`` (another
            program than the one asked for answered).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class RemoteError(ChannelError):
    """The device answered a request with an error.

    Attributes:
        method: The request's method.
        code: The device's error code, such as ``gesture_cancelled``.
    """

    def __init__(self, method: str, code: str, message: str) -> None:
        super().__init__(f"{method}: {code}: {message}")
        self.method = method
        self.code = code


class EventsLost(ChannelError):
    """The event stream ended before event ``next_seq``; every event before it was delivered."""

    def __init__(self, message: str, next_seq: int) -> None:
        super().__init__(message)
        self.next_seq = next_seq


class Hello(BaseModel):
    """How a device-side program describes itself in its hello.

    Attributes:
        protocol: Its :data:`PROTOCOL_VERSION`.
        component: Which program answered.
        package: The APK's package.
        version_code: The installed APK's ``versionCode``.
        version_name: Its ``versionName``.
        sdk_int: The device's API level.
        apk_sha256: The SHA-256 of the APK file the program runs from, read on
            the device: the installed build, measured.
        accessibility_tool: Whether the system takes the service for an
            accessibility tool (``isAccessibilityTool``), as the system read
            the APK; ``None`` from the helper, and on Android 11, which has no
            such attribute.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: int
    component: Component
    package: str
    version_code: int
    version_name: str
    sdk_int: int
    apk_sha256: str
    accessibility_tool: bool | None = None


@dataclass(frozen=True)
class Frame:
    """One frame: its JSON header and its payload."""

    header: JsonObject
    payload: bytes = b""


@dataclass(frozen=True)
class Reply:
    """A device's answer to one request: its result and the payload that came with it."""

    result: JsonObject
    payload: bytes = b""


def encode_frame(header: Mapping[str, JsonValue], payload: bytes = b"") -> bytes:
    """*header* and *payload* as one frame.

    Raises:
        FrameError: The frame would be longer than :data:`MAX_FRAME_BYTES`.
    """
    data = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode()
    length = 4 + len(data) + len(payload)
    if length > MAX_FRAME_BYTES:
        raise FrameError(f"a frame of {length} bytes; the protocol allows {MAX_FRAME_BYTES}")
    return _LENGTHS.pack(length, len(data)) + data + payload


async def read_frame(reader: asyncio.StreamReader) -> Frame | None:
    """The next frame, or ``None`` when the stream ended cleanly between two frames.

    Raises:
        FrameError: The frame is too long, cut short, or its header is not a
            JSON object.
    """
    try:
        lengths = await reader.readexactly(_LENGTHS.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise FrameError("the stream ended inside a frame's lengths") from None
    length, header_length = _LENGTHS.unpack(lengths)
    if not 4 <= length <= MAX_FRAME_BYTES or header_length > length - 4:
        raise FrameError(f"a frame of {length} bytes with a header of {header_length}")
    try:
        body = await reader.readexactly(length - 4)
    except asyncio.IncompleteReadError as exc:
        raise FrameError(
            f"the stream ended {len(exc.partial)} bytes into a frame of {length}"
        ) from None
    try:
        header: JsonValue = json.loads(body[:header_length])
    except ValueError as exc:  # UnicodeDecodeError is a ValueError
        raise FrameError(f"a frame header that is not JSON: {exc}") from None
    if not isinstance(header, dict):
        raise FrameError(f"a frame header that is a JSON {type(header).__name__}, not an object")
    return Frame(header, body[header_length:])


class EventStream:
    """The events a device pushes after ``subscribe``: each once, in order.

    :meth:`get` hands them over one by one. Once the stream has ended, it
    raises why, after every event that came before the end:
    :class:`EventsLost`, or :class:`ChannelClosed` when the channel closed.
    One consumer reads it.
    """

    def __init__(self, max_buffered: int) -> None:
        self._max_buffered = max_buffered
        self._buffer: deque[JsonObject] = deque()
        self._next_seq: int | None = None
        self._end: ChannelError | None = None
        self._ready = asyncio.Event()

    async def get(self) -> JsonObject:
        """The next event: a JSON object with its ``seq`` and what the device reported.

        Raises:
            EventsLost: The stream ended before the next event.
            ChannelClosed: The channel closed.
        """
        while (event := self.get_nowait()) is None:
            self._ready.clear()
            await self._ready.wait()
        return event

    def get_nowait(self) -> JsonObject | None:
        """The next event if it has arrived, else ``None``; raises like :meth:`get` once ended."""
        if self._buffer:
            return self._buffer.popleft()
        if self._end is not None:
            raise self._end
        return None

    def _start(self, next_seq: int) -> None:
        self._next_seq = next_seq

    def _push(self, event: JsonObject) -> None:
        if self._end is not None:
            return
        due = self._next_seq
        if due is None:
            self._finish(EventsLost("an event arrived before subscribe was answered", 0))
            return
        if event.get("seq") != due:
            self._finish(EventsLost(f"event {event.get('seq')} arrived where {due} was due", due))
            return
        if len(self._buffer) >= self._max_buffered:
            self._finish(
                EventsLost(
                    f"{self._max_buffered} events waited unread; events from {due} on were not "
                    "kept",
                    due,
                )
            )
            return
        self._next_seq = due + 1
        self._buffer.append(event)
        self._ready.set()

    def _finish(self, end: ChannelError) -> None:
        if self._end is None:
            self._end = end
            self._ready.set()


@dataclass
class _Pending:
    """A request still waiting for its answer."""

    method: str
    answer: asyncio.Future[Reply]
    #: Run by the reader on the result, before any frame after the answer is read.
    on_result: Callable[[JsonObject], None] | None = None


class Channel:
    """One greeted connection to a device-side program.

    Calls may run side by side; the device answers them in order. A reader
    task reads every frame as it arrives. Made by :meth:`open`.
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, hello: Hello
    ) -> None:
        #: How the program described itself.
        self.hello = hello
        self._reader = reader
        self._writer = writer
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        #: Requests sent whose callers gave up before their answers came.
        self._abandoned: set[int] = set()
        self._send_lock = asyncio.Lock()
        self._events: EventStream | None = None
        self._ended: ChannelClosed | None = None
        self._reading = asyncio.ensure_future(self._read_frames())

    @classmethod
    async def open(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        token: str,
        component: Component,
    ) -> Channel:
        """Greet the program at the other end of the streams and return the open channel.

        The streams are the channel's from now on; a failed greeting closes them.

        Raises:
            HandshakeRefused: The hello was refused.
            ChannelClosed: The stream ended before the answer to the hello.
            FrameError: The answer broke the protocol.
        """
        try:
            writer.write(
                encode_frame({"kind": "hello", "protocol": PROTOCOL_VERSION, "token": token})
            )
            await writer.drain()
            hello = _greeted(await read_frame(reader), component)
        except BaseException:
            await _close(writer)
            raise
        return cls(reader, writer, hello)

    @property
    def closed(self) -> bool:
        return self._ended is not None

    @property
    def abandoned(self) -> int:
        """How many requests sent here may still run on the device though their callers gave up.

        Each counts until its late answer arrives; 0 once the channel closed.
        Any request answered from now on was answered after all of them.
        """
        return 0 if self.closed else len(self._abandoned)

    async def call(self, method: str, params: Mapping[str, JsonValue] | None = None) -> Reply:
        """Send one request and return the device's answer.

        A cancelled call drops the answer when it comes.

        Raises:
            RemoteError: The device answered with an error.
            ChannelClosed: The channel closed before the answer came.
        """
        return await self._request(method, params, None)

    async def subscribe(self, *, max_buffered: int = 100_000) -> EventStream:
        """Start the device's event stream; once per channel.

        Args:
            max_buffered: How many events may wait for the consumer before the
                stream ends with :class:`EventsLost`.

        Raises:
            RuntimeError: This channel subscribed already.
            RemoteError: The program pushes no events.
            ChannelClosed: The channel closed.
        """
        if self._events is not None:
            raise RuntimeError(
                "this channel subscribed already; a subscribe that failed or was cancelled "
                "leaves it so, and a new channel subscribes afresh"
            )
        stream = EventStream(max_buffered)
        self._events = stream

        def start(result: JsonObject) -> None:
            next_seq = result.get("next_seq")
            if not isinstance(next_seq, int):
                raise FrameError(f"subscribe answered without a next_seq: {result}")
            stream._start(next_seq)

        await self._request("subscribe", None, start)
        return stream

    async def close(self) -> None:
        """Close the channel; waiting calls and the event stream get :class:`ChannelClosed`.

        Safe to repeat.
        """
        self._end(ChannelClosed("the channel was closed"))
        self._reading.cancel()
        await asyncio.wait([self._reading])
        await _close(self._writer)

    async def _request(
        self,
        method: str,
        params: Mapping[str, JsonValue] | None,
        on_result: Callable[[JsonObject], None] | None,
    ) -> Reply:
        if self._ended is not None:
            raise ChannelClosed(str(self._ended))
        request_id = next(self._ids)
        frame = encode_frame(
            {"kind": "request", "id": request_id, "method": method, "params": dict(params or {})}
        )
        answer: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _Pending(method, answer, on_result)
        sent = False
        try:
            async with self._send_lock:
                try:
                    self._writer.write(frame)
                    sent = True  # in the transport's buffer: it reaches the device
                    await self._writer.drain()
                except OSError as exc:
                    raise ChannelClosed(f"{method}: the channel broke: {exc}") from None
            return await answer
        except asyncio.CancelledError:
            # Cancelling the waiting task cancels the future it awaits: no answer came.
            answered = answer.done() and not answer.cancelled()
            if sent and not answered and self._ended is None:
                self._abandoned.add(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _read_frames(self) -> None:
        end = ChannelClosed("the device closed the channel")
        try:
            while (frame := await read_frame(self._reader)) is not None:
                self._dispatch(frame)
        except Exception as exc:  # whatever broke the stream ends it for every waiter
            end = ChannelClosed(f"the channel broke: {type(exc).__name__}: {exc}")
        finally:
            self._end(end)

    def _dispatch(self, frame: Frame) -> None:
        header = frame.header
        kind = header.get("kind")
        if kind == "response":
            request_id = header.get("id")
            if not isinstance(request_id, int):
                return
            self._abandoned.discard(request_id)
            pending = self._pending.get(request_id)
            if pending is None or pending.answer.done():
                return  # its caller gave up
            error = header.get("error")
            result = header.get("result")
            if isinstance(error, dict):
                code, message = error.get("code"), error.get("message")
                pending.answer.set_exception(RemoteError(pending.method, str(code), str(message)))
            elif isinstance(result, dict):
                if pending.on_result is not None:
                    pending.on_result(result)
                pending.answer.set_result(Reply(result, frame.payload))
            else:
                raise FrameError(f"an answer to {pending.method} with neither result nor error")
        elif kind in ("event", "overflow"):
            stream = self._events
            if stream is None:
                raise FrameError(f"an {kind} frame before subscribe")
            if kind == "event":
                stream._push({key: value for key, value in header.items() if key != "kind"})
            else:
                next_seq = header.get("next_seq")
                lost = next_seq if isinstance(next_seq, int) else 0
                stream._finish(
                    EventsLost(
                        f"the device's queue of unsent events outgrew its limit; events from "
                        f"{lost} on were not sent",
                        lost,
                    )
                )
        else:
            raise FrameError(f"a frame of kind {kind!r}")

    def _end(self, end: ChannelClosed) -> None:
        """Settle every waiter with *end*, the first reason the channel ended."""
        if self._ended is None:
            self._ended = end
        self._abandoned.clear()
        for pending in self._pending.values():
            if not pending.answer.done():
                pending.answer.set_exception(ChannelClosed(f"{pending.method}: {self._ended}"))
        if self._events is not None:
            self._events._finish(ChannelClosed(str(self._ended)))


def _greeted(frame: Frame | None, component: Component) -> Hello:
    """The program's hello in *frame*, checked against this host's protocol and *component*."""
    if frame is None:
        raise ChannelClosed("the device closed the channel before answering the hello")
    header = frame.header
    kind = header.get("kind")
    if kind == "refused":
        reason = str(header.get("reason"))
        raise HandshakeRefused(reason, f"the device refused the hello: {header.get('message')}")
    if kind != "hello":
        raise FrameError(f"the device answered the hello with a frame of kind {kind!r}")
    protocol = header.get("protocol")
    if protocol != PROTOCOL_VERSION:
        raise HandshakeRefused(
            "protocol",
            f"the {header.get('component')} on the device speaks protocol {protocol} and this "
            f"host speaks {PROTOCOL_VERSION}: install the APK built from this checkout",
        )
    try:
        hello = Hello.model_validate({key: value for key, value in header.items() if key != "kind"})
    except ValidationError as exc:
        raise FrameError(f"a hello this protocol never sends: {exc}") from None
    if hello.component != component:
        raise HandshakeRefused(
            "component", f"the {component} was asked for and the {hello.component} answered"
        )
    return hello


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
