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
import hashlib
import json
import shlex
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
from tests._helpers.fake_adb import ShellIO, ShellResult, SocketServer, StreamingCommand


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
        self._open: set[asyncio.StreamWriter] = set()

    def emit(self, **event: JsonValue) -> int:
        """Push one event to every subscribed connection; returns its ``seq``."""
        seq = self._next_seq
        self._next_seq += 1
        if self._subscribed:
            frame = encode_frame({"kind": "event", "seq": seq, **event})
            for outbound in self._subscribed:
                outbound.send_event(frame, seq)
        return seq

    def hang_up(self) -> None:
        """Drop every open connection at once, as the program's process dying does."""
        for writer in self._open:
            writer.transport.abort()

    def lose_event(self) -> None:
        """Number the next event as if one were pushed, and push nothing: a device that lost one."""
        self._next_seq += 1

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one connection until the host closes it."""
        outbound = _Outbound(writer, self._max_unsent_events)
        sending = asyncio.ensure_future(outbound.run())
        self._open.add(writer)
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
            self._open.discard(writer)
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


#: Where the fake phone keeps the installed APK.
INSTALLED_PATH = "/data/app/~~kTm2Xl0lQ==/org.spectragent.device-Q2nX9w==/base.apk"
#: The service as the setting names it.
SERVICE_COMPONENT = "org.spectragent.device/.DeviceService"
SERVICE_SOCKET = "spectragent_device"


class FakeDeviceSide:
    """One phone's device side, behind ``tests/_helpers/fake_adb.py``: pass :meth:`device`,
    :meth:`streams` and :meth:`sockets` for its serial.

    It answers what ``device/lifecycle.py`` sends, as the APK and Android do:

    - ``pm path`` and ``sha256sum`` report the installed APK, and
      ``cmd package install -r -S <n>`` installs the *n* bytes on its stdin;
    - ``app_process`` runs the helper: it reads ``<socket> <token>`` on its
      stdin, listens on that socket (a :class:`FakeProgram`), prints
      ``READY 1`` and runs until its stdin ends;
    - ``content write`` to the bootstrap provider's ``/session`` sets, from its
      stdin, the token the service's hello requires; ``content call`` with
      ``health`` reports;
    - the helper's ``enable_service`` appends the service to the setting, and
      the system binds it, so its socket appears, unless *binds* is false: then
      the service vanishes from the setting again and never listens, as
      Android 14 drops a service it refuses. The helper remembers whether it
      wrote the setting, and its ``disable_service`` takes out only what it
      added, which unbinds the service;
    - :meth:`force_stop` does what ``am force-stop`` of the package does: the
      service leaves the setting and its process, token and socket go.
    """

    def __init__(
        self,
        *,
        installed: bytes | None = None,
        enabled: str | None = None,
        binds: bool = True,
        faketouch: bool = True,
        lose_token_once: bool = False,
    ) -> None:
        #: The installed APK's bytes; ``None``: not installed.
        self.installed = installed
        #: ``enabled_accessibility_services``; ``None``: unset.
        self.enabled = enabled
        self.binds = binds
        self.faketouch = faketouch
        self.installs = 0
        self.helper_exits = 0
        #: What each helper read on its stdin.
        self.helper_stdin: list[bytes] = []
        self.helper: FakeProgram | None = None
        self.helper_socket: str | None = None
        self.service = FakeProgram(
            component="service",
            token="",
            methods={"ping": self._service_ping, "gesture": self._gesture},
        )
        self.bound = False
        #: Whether the helper put the service into the setting; it takes out only that.
        self.added = False
        #: How many times the service's token was written to the provider.
        self.tokens_written = 0
        self._lose_token_once = lose_token_once

    def force_stop(self) -> None:
        """``am force-stop org.spectragent.device``: unbound, out of the setting, its token gone."""
        self.bound = False
        self.service.token = ""
        self._disable()

    async def device(self, command: str) -> ShellResult:
        if command == "pm path org.spectragent.device":
            if self.installed is None:
                return ShellResult(exit_code=1)
            return ShellResult(stdout=f"package:{INSTALLED_PATH}\n".encode())
        if command.startswith("sha256sum "):
            digest = hashlib.sha256(self.installed or b"").hexdigest()
            return ShellResult(stdout=f"{digest}  {INSTALLED_PATH}\n".encode())
        if command.startswith("content call --uri content://org.spectragent.device.bootstrap "):
            return self._provider(shlex.split(command))
        if command == "pm has-feature android.hardware.faketouch":
            return ShellResult(stdout=b"true\n" if self.faketouch else b"false\n")
        name = command.split(maxsplit=1)[0] if command.strip() else command
        return ShellResult(stderr=f"/system/bin/sh: {name}: not found\n".encode(), exit_code=127)

    def streams(self, command: str) -> StreamingCommand | None:
        if command == "content write --uri content://org.spectragent.device.bootstrap/session":
            return self._take_token
        if command.startswith("cmd package install -r -S "):
            return self._installer(int(command.rsplit(" ", 1)[1]))
        if command.startswith("CLASSPATH=") and command.endswith(
            "app_process /system/bin org.spectragent.device.ShellHelper"
        ):
            return self._run_helper
        return None

    def sockets(self, name: str) -> SocketServer | None:
        if self.helper is not None and name == self.helper_socket:
            return self.helper.serve
        if name == SERVICE_SOCKET and self.bound:
            return self.service.serve
        return None

    def _provider(self, words: list[str]) -> ShellResult:
        if words[words.index("--method") + 1] != "health":
            return ShellResult(stderr=b"Error while accessing provider\n", exit_code=1)
        health = {
            "protocol": PROTOCOL_VERSION,
            "package": "org.spectragent.device",
            "version_code": 1,
            "version_name": "1",
            "accessibility_tool": False,
            "sdk_int": 33,
            "socket": SERVICE_SOCKET,
            "service_connected": self.bound,
            "listening": self.bound,
            "token_set": bool(self.service.token),
        }
        return ShellResult(stdout=f"Result: Bundle[{{json={json.dumps(health)}}}]\n".encode())

    async def _take_token(self, shell: ShellIO) -> int:
        data = bytearray()
        while (chunk := await shell.read_stdin()) is not None:
            data += chunk
        # A process that restarted after the bootstrap has lost the token.
        self.service.token = "lost" if self._lose_token_once else data.decode().strip()
        self._lose_token_once = False
        self.tokens_written += 1
        return 0

    def _installer(self, size: int) -> StreamingCommand:
        async def install(shell: ShellIO) -> int:
            data = bytearray()
            while (chunk := await shell.read_stdin()) is not None:
                data += chunk
            if len(data) != size:
                await shell.write(b"Failure [INSTALL_FAILED_INVALID_APK]\n")
                return 1
            self.installed = bytes(data)
            self.installs += 1
            await shell.write(b"Success\n")
            return 0

        return install

    async def _run_helper(self, shell: ShellIO) -> int:
        line = await shell.read_stdin() or b""
        self.helper_stdin.append(line)
        socket, token = line.decode().split()
        self.helper_socket = socket
        self.helper = FakeProgram(
            component="helper",
            token=token,
            methods={
                "enable_service": self._enable_method,
                "disable_service": self._disable_method,
            },
        )
        await shell.write(b"READY 1\n")
        while await shell.read_stdin() is not None:  # until the host's session ends
            pass
        self.helper = None
        self.helper_exits += 1
        return 0

    async def _enable_method(self, params: JsonObject) -> Answer:
        previous = self.enabled
        entries = previous.split(":") if previous else []
        changed = SERVICE_COMPONENT not in entries
        if changed:
            self.enabled = ":".join([*entries, SERVICE_COMPONENT])
            self.added = True
        current = self.enabled
        if self.binds:
            self.bound = True
        else:
            self.enabled = previous  # refused, and gone from the setting without a word
        return Answer({"previous": previous, "current": current, "changed": changed})

    async def _disable_method(self, params: JsonObject) -> Answer:
        previous = self.enabled
        if not self.added:
            return Answer({"previous": previous, "current": previous, "changed": False})
        changed = self._disable()
        self.added = False
        return Answer({"previous": previous, "current": self.enabled, "changed": changed})

    def _disable(self) -> bool:
        """Out of the setting, so unbound: the service stops listening and its connections end."""
        entries = self.enabled.split(":") if self.enabled else []
        kept = [entry for entry in entries if entry != SERVICE_COMPONENT]
        self.enabled = ":".join(kept) or None
        self.bound = False
        self.service.hang_up()
        return len(kept) != len(entries)

    async def _service_ping(self, params: JsonObject) -> Answer:
        return Answer({"uptime_ms": 1000, "next_seq": 0})

    async def _gesture(self, params: JsonObject) -> Answer:
        if not self.faketouch:
            raise Failure("gesture_cancelled", "the system cancelled the gesture")
        return Answer({"elapsed_ms": 50})
