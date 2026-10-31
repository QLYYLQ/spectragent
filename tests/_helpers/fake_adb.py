"""An ADB server in this process, answering every device service from a script.

Spectragent talks to ADB through its own asyncio client for the host protocol,
and the provider hands that client a loopback ``AdbEndpoint``
(``docs/architecture_design.md`` §11.2, "主机传输层", "Provider").
:class:`FakeAdbServer` is such an endpoint: it listens on an ephemeral
loopback port and keeps its devices in memory, so a client under test connects
to it exactly as it would to a private adb server, without an adb binary, an
emulator or a phone.

It speaks ``host:transport``, ``shell,v2:`` and ``exec:``, as adb 1.0.41
answered them on the emulator (2026-11-01), and refuses every other request:

- A request is four hex digits giving its length, then that many bytes. The
  answer is ``OKAY``, or ``FAIL`` followed by four hex digits and a message. A
  length that is not four hex digits closes the connection unanswered.
- ``host:transport:<serial>`` binds the connection to one device. A device
  service sent before it is refused (``device offline (no transport)``).
- ``shell,v2:<command>`` (options may precede the colon: ``shell,v2,raw:``)
  answers in shell-protocol packets: one id byte (1 stdout, 2 stderr, 3 exit),
  a four-byte little-endian length, the payload; the exit packet carries one
  byte. Then the connection closes. Real packets are as large as what the
  command wrote at once, so long output spans several; this fake cuts every
  stream into :data:`SHELL_PACKET_MAX`-byte packets, and a client that reads
  one packet per stream fails here too.
- ``exec:<command>`` answers with the command's output and closes, with no
  exit code. Its stderr comes in the same stream: a warning on stderr lands
  inside the bytes of a ``screencap -p`` PNG. This fake writes stdout, then
  stderr.
- Any other request (``host:version``, ``host:track-devices``; ``shell:``
  without ``v2``; ``sync:`` and ``tcp:``, which the design's client will also
  use) is answered ``FAIL``, so a client that starts using one fails a test
  until this fake learns it, instead of getting a guess at the answer. A
  ``host:`` request is the server's, so it is never a device round trip.

Round trips: every service sent to a device is one round trip to it, kept in
:attr:`FakeAdbServer.requests` in arrival order, so a test can check what one
observation capture sent and what it cost (:meth:`FakeAdbServer.round_trips`).
``host:transport`` is answered by the server and costs the device nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import struct
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

#: The largest shell-protocol packet this fake sends. adbd's vary with how the
#: command writes: ``screencap -p`` arrived in 53 packets of up to 32,816 bytes,
#: ``head -c 200000 /dev/zero`` in two (196,608 and 3,392).
SHELL_PACKET_MAX = 32 * 1024

_STDOUT, _STDERR, _EXIT = 1, 2, 3
_LENGTH = re.compile(rb"[0-9a-fA-F]{4}")


@dataclass(frozen=True)
class ShellResult:
    """What one command printed, and how it exited."""

    stdout: bytes = b""
    stderr: bytes = b""
    exit_code: int = 0


#: A device: the answer to one command line. Asynchronous, so a test can hold
#: an answer back (a screen grab that hangs) or change it from call to call.
Device = Callable[[str], Awaitable[ShellResult]]


def scripted(answers: Mapping[str, ShellResult]) -> Device:
    """A device answering each command in *answers*, and nothing else, as ``sh`` would (exit 127)."""

    async def answer(command: str) -> ShellResult:
        if command in answers:
            return answers[command]
        name = command.split(maxsplit=1)[0] if command.strip() else command
        return ShellResult(
            stderr=f"/system/bin/sh: {name}: inaccessible or not found\n".encode(), exit_code=127
        )

    return answer


@dataclass(frozen=True)
class DeviceRequest:
    """One service sent to a device: one round trip to it."""

    serial: str
    service: str


class FakeAdbServer:
    """A loopback ADB server for *devices*, serial → :data:`Device`; an async context manager.

    Inside the ``async with`` block, :attr:`host` and :attr:`port` are where a
    client connects. Leaving it stops listening and drops every connection at
    once, so neither a held answer nor a client that stays connected, or that
    stopped reading, can keep the test waiting. Then it raises the first error
    a connection hit, so a fault in a device script or in this fake fails the
    test instead of only cutting an answer short.
    """

    def __init__(self, devices: Mapping[str, Device]) -> None:
        self.host = "127.0.0.1"
        self.port = 0
        #: Every service sent to a device, in arrival order.
        self.requests: list[DeviceRequest] = []
        self._devices = dict(devices)
        self._server: asyncio.Server | None = None
        self._connections: dict[asyncio.Task[None], asyncio.StreamWriter] = {}
        self._closing = False

    def round_trips(self, serial: str) -> int:
        """How many services were sent to *serial*."""
        return sum(1 for request in self.requests if request.serial == serial)

    async def __aenter__(self) -> FakeAdbServer:
        self._server = await asyncio.start_server(self._accept, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, exc_type: object, *_rest: object) -> None:
        assert self._server is not None
        self._closing = True
        self._server.close()
        for task, writer in self._connections.items():
            # Abort, not close: a graceful close waits for a client that may never read.
            writer.transport.abort()
            task.cancel()
        results = await asyncio.gather(*self._connections, return_exceptions=True)
        await self._server.wait_closed()
        errors = [result for result in results if isinstance(result, Exception)]
        if errors and exc_type is None:
            raise errors[0]

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # A plain function, so a connection is recorded the moment it is made,
        # before its task first runs. The server counts a connection a loop turn
        # before this runs, so one that arrives while the block is being left is
        # dropped here, or the server would wait for it forever.
        if self._closing:
            writer.transport.abort()
            return
        self._connections[asyncio.create_task(self._serve(reader, writer))] = writer

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            serial: str | None = None
            while (request := await _read_request(reader)) is not None:
                if request.startswith("host:transport:"):
                    serial = request.removeprefix("host:transport:")
                    if serial not in self._devices:
                        await _fail(writer, f"device '{serial}' not found")
                        return
                    writer.write(b"OKAY")
                    continue
                if request.startswith("host:"):
                    await _fail(writer, _unspoken(request))
                    return
                if serial is None:
                    await _fail(writer, "device offline (no transport)")
                    return
                self.requests.append(DeviceRequest(serial, request))
                # A client may hang up before the answer ends (a capture that timed out).
                with contextlib.suppress(ConnectionError):
                    await self._answer(self._devices[serial], request, reader, writer)
                return
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _answer(
        self,
        device: Device,
        request: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        service, _, command = request.partition(":")
        kind, *options = service.split(",")
        if not (kind == "shell" and "v2" in options) and not (kind == "exec" and not options):
            await _fail(writer, _unspoken(service))
            return
        # The stream is open once the device accepts it, before the command ends.
        writer.write(b"OKAY")
        await writer.drain()
        # Read what the client sends from now on (stdin, close-stdin) and drop it:
        # closing a socket with unread input resets the connection, and the client
        # could lose output it had not read yet.
        discard = asyncio.ensure_future(_drop_input(reader))
        try:
            result = await device(command)
            if kind == "exec":
                writer.write(result.stdout + result.stderr)
            else:
                for ident, data in ((_STDOUT, result.stdout), (_STDERR, result.stderr)):
                    for start in range(0, len(data), SHELL_PACKET_MAX):
                        writer.write(_packet(ident, data[start : start + SHELL_PACKET_MAX]))
                writer.write(_packet(_EXIT, bytes([result.exit_code])))
            await writer.drain()
        finally:
            discard.cancel()


async def _read_request(reader: asyncio.StreamReader) -> str | None:
    """The next request, or ``None`` when the client closed the connection or sent garbage."""
    try:
        head = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    if not _LENGTH.fullmatch(head):
        return None
    try:
        return (await reader.readexactly(int(head, 16))).decode()
    except asyncio.IncompleteReadError:
        return None


async def _drop_input(reader: asyncio.StreamReader) -> None:
    with contextlib.suppress(ConnectionError):
        await reader.read()


def _unspoken(request: str) -> str:
    return f"this fake speaks host:transport:, shell,v2: and exec:, not {request!r}"


async def _fail(writer: asyncio.StreamWriter, message: str) -> None:
    data = message.encode()
    writer.write(b"FAIL" + b"%04x" % len(data) + data)
    await writer.drain()


def _packet(ident: int, data: bytes) -> bytes:
    return struct.pack("<BI", ident, len(data)) + data
