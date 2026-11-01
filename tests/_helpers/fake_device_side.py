"""A device-side program in this process: the channel protocol spoken from the device's end.

``spectragent/core/capabilities/android/device/channel.py`` describes the
protocol; ``android/service/`` speaks it on the device. :class:`FakeProgram`
speaks it the same way, as the APK's service or its helper:

- the hello first: a host of another protocol version, or without the
  session's token, is refused (``refused``) and the connection closed;
- one connection's requests answered one at a time, in order, each by the
  :data:`Method` its name selects; a method that raises :class:`Failure`
  answers with that error, an unknown method with ``unknown_method``;
- events (:meth:`FakeProgram.emit`) numbered from 0 for the program's life,
  pushed to every connection that subscribed (the service only), and answers
  and events leaving through one ordered queue per connection, as on the
  device: ``subscribe`` is answered on that queue before any event it
  covers. With ``max_unsent_events``, a connection whose queue holds that many
  unsent events gets ``overflow`` in place of the next one and is closed.

:meth:`FakeProgram.serve` serves one connection; it is a ``SocketServer`` for
``tests/_helpers/fake_adb.py``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from spectragent.core.capabilities.android.device.channel import (
    PROTOCOL_VERSION,
    Component,
    Frame,
    JsonObject,
    encode_frame,
    read_frame,
)


@dataclass(frozen=True)
class Answer:
    """What a method answers: its result and a payload."""

    result: JsonObject = field(default_factory=dict)
    payload: bytes = b""


class Failure(Exception):
    """Raised by a method to answer with an error *code*."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


#: One method: its params in, its answer out.
Method = Callable[[JsonObject], Awaitable[Answer]]


class _Outbound:
    """One connection's queue of frames to send, drained in order by :meth:`run`."""

    def __init__(self, writer: asyncio.StreamWriter, max_unsent_events: int | None) -> None:
        self._writer = writer
        self._max_unsent_events = max_unsent_events
        self._queue: deque[tuple[bytes, bool]] = deque()
        self._unsent_events = 0
        self._ready = asyncio.Event()
        self._last_queued = False

    def send(self, frame: bytes) -> None:
        if not self._last_queued:
            self._queue.append((frame, False))
            self._ready.set()

    def send_event(self, frame: bytes, seq: int) -> None:
        if self._last_queued:
            return
        if self._max_unsent_events is not None and self._unsent_events >= self._max_unsent_events:
            self.send_last(encode_frame({"kind": "overflow", "next_seq": seq}))
            return
        self._unsent_events += 1
        self._queue.append((frame, True))
        self._ready.set()

    def send_last(self, frame: bytes | None = None) -> None:
        """Queue *frame*, if any, as the last frame; the connection closes once it is sent."""
        if frame is not None and not self._last_queued:
            self._queue.append((frame, False))
        self._last_queued = True
        self._ready.set()

    async def run(self) -> None:
        try:
            while True:
                while not self._queue:
                    if self._last_queued:
                        return
                    self._ready.clear()
                    await self._ready.wait()
                frame, is_event = self._queue.popleft()
                if is_event:
                    self._unsent_events -= 1
                self._writer.write(frame)
                await self._writer.drain()
        finally:
            self._writer.close()


class FakeProgram:
    """The service or the helper, answering from *methods* (module docstring)."""

    def __init__(
        self,
        *,
        component: Component,
        token: str,
        methods: Mapping[str, Method],
        protocol: int = PROTOCOL_VERSION,
        refuses_other_protocols: bool = True,
        max_unsent_events: int | None = None,
        apk_sha256: str = "0" * 64,
        accessibility_tool: bool | None = None,
    ) -> None:
        self.component = component
        self.token = token
        self.protocol = protocol
        #: Every request served, in order: its method and params.
        self.calls: list[tuple[str, JsonObject]] = []
        self._methods = dict(methods)
        self._refuses_other_protocols = refuses_other_protocols
        self._max_unsent_events = max_unsent_events
        self._apk_sha256 = apk_sha256
        self._accessibility_tool = accessibility_tool
        self._subscribed: list[_Outbound] = []
        self._next_seq = 0

    def emit(self, **event: JsonValue) -> int:
        """Push one event to every subscribed connection; returns its ``seq``."""
        seq = self._next_seq
        self._next_seq += 1
        if self._subscribed:
            frame = encode_frame({"kind": "event", "seq": seq, **event})
            for outbound in self._subscribed:
                outbound.send_event(frame, seq)
        return seq

    def lose_event(self) -> None:
        """Number the next event as if one were pushed, and push nothing: a device that lost one."""
        self._next_seq += 1

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one connection until the host closes it."""
        outbound = _Outbound(writer, self._max_unsent_events)
        sending = asyncio.ensure_future(outbound.run())
        try:
            refusal = self._refusal(await read_frame(reader))
            if refusal is not None:
                outbound.send_last(encode_frame(refusal))
                return
            outbound.send(encode_frame(self._hello()))
            while (frame := await read_frame(reader)) is not None:
                await self._answer(frame.header, outbound)
        except ConnectionError:
            pass
        finally:
            if outbound in self._subscribed:
                self._subscribed.remove(outbound)
            outbound.send_last()
            await sending

    def _refusal(self, frame: Frame | None) -> JsonObject | None:
        header = frame.header if frame is not None else {}
        if header.get("kind") != "hello":
            return {"kind": "refused", "reason": "protocol", "message": "no hello"}
        if self._refuses_other_protocols and header.get("protocol") != self.protocol:
            return {
                "kind": "refused",
                "reason": "protocol",
                "message": f"this device speaks protocol {self.protocol}",
            }
        if header.get("token") != self.token:
            return {"kind": "refused", "reason": "token", "message": "not this session's token"}
        return None

    def _hello(self) -> JsonObject:
        return {
            "kind": "hello",
            "protocol": self.protocol,
            "component": self.component,
            "package": "org.spectragent.device",
            "version_code": 1,
            "version_name": "1",
            "sdk_int": 33,
            "apk_sha256": self._apk_sha256,
            "accessibility_tool": self._accessibility_tool,
        }

    async def _answer(self, header: JsonObject, outbound: _Outbound) -> None:
        request_id, method, params = header.get("id"), header.get("method"), header.get("params")
        assert isinstance(request_id, int) and isinstance(method, str) and isinstance(params, dict)
        self.calls.append((method, params))
        if method == "subscribe" and self.component == "service":
            self._subscribed.append(outbound)
            # On the queue at once, so before any event emitted from now on.
            outbound.send(self._response(request_id, Answer({"next_seq": self._next_seq})))
            return
        handler = self._methods.get(method)
        try:
            if handler is None:
                raise Failure("unknown_method", f"the {self.component} has no method {method!r}")
            answer = await handler(params)
        except Failure as failure:
            outbound.send(
                encode_frame(
                    {
                        "kind": "response",
                        "id": request_id,
                        "error": {"code": failure.code, "message": str(failure)},
                    }
                )
            )
            return
        outbound.send(self._response(request_id, answer))

    @staticmethod
    def _response(request_id: int, answer: Answer) -> bytes:
        return encode_frame(
            {"kind": "response", "id": request_id, "result": answer.result}, answer.payload
        )
