"""An ADB server in this process, answering every device service from a script.

Spectragent talks to ADB through its own asyncio client for the host protocol,
and the provider hands that client a loopback ``AdbEndpoint``
(``docs/architecture_design.md`` §11.2, "主机传输层", "Provider").
:class:`FakeAdbServer` is such an endpoint: it listens on an ephemeral
loopback port and keeps its devices in memory, so a client under test connects
to it exactly as it would to a private adb server, without an adb binary, an
emulator or a phone.

It speaks ``host:transport``, ``shell,v2:``, ``exec:``, ``localabstract:`` and
``root:``, as adb 1.0.41 answered them on the emulator (2026-11-01), and
refuses every other request:

- A request is four hex digits giving its length, then that many bytes. The
  answer is ``OKAY``, or ``FAIL`` followed by four hex digits and a message. A
  length that is not four hex digits closes the connection unanswered.
- ``host:transport:<serial>`` binds the connection to one device. A device
  service sent before it is refused (``device offline (no transport)``).
- ``shell,v2:<command>`` (options may precede the colon: ``shell,v2,raw:``)
  answers in shell-protocol packets: one id byte (1 stdout, 2 stderr, 3 exit),
  a four-byte little-endian length, the payload; the exit packet carries one
  byte. The client writes stdin in the same packets (id 0; id 4 closes it).
  Real packets are as large as what the command wrote at once, so long output
  spans several; this fake cuts every stream into
  :data:`SHELL_PACKET_MAX`-byte packets, and a client that reads one packet per
  stream fails here too. A command is answered from the device script, which
  never sees stdin, unless the serial's ``streams`` hook picks a
  :data:`StreamingCommand` for it: that one reads stdin as it arrives and
  writes as it runs (``cmd package install -S``, a program that stays up).
- Once a ``shell,v2`` or ``exec`` command has answered, the fake shuts its
  sending side (the client reads the end of the stream) and reads on until the
  client hangs up. A real adb server closes outright, but only after relaying
  the device's close, by which time the client's close-stdin packet has
  arrived. A fake that closed outright at once would answer a close-stdin
  that comes later with a reset, and asyncio raises a reset from
  ``readexactly`` even while the exit packet waits unread in its buffer.
- ``localabstract:<name>`` connects to the abstract Unix socket *name* that the
  serial's ``sockets`` hook says something listens on, and the connection then
  carries that server's bytes both ways; a name nothing listens on is answered
  ``FAIL closed``, as adbd closes the stream it could not connect.
- ``exec:<command>`` answers with the command's output and closes, with no
  exit code. Its stderr comes in the same stream: a warning on stderr lands
  inside the bytes of a ``screencap -p`` PNG. This fake writes stdout, then
  stderr.
- ``root:`` answers with what the device says to the command ``root:`` (its
  stdout: "restarting adbd as root", "adbd is already running as root") and
  closes, as adbd answers before it restarts. A non-zero exit code makes it
  ``FAIL`` with the stderr instead, as the server answered ``FAIL closed``
  when adbd closed the stream at once.
- Any other request (``host:version``, ``host:track-devices``; ``shell:``
  without ``v2``; ``sync:`` and ``tcp:``) is answered ``FAIL``, so a client that starts using one fails a test
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
import errno
import re
import struct
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

#: The largest shell-protocol packet this fake sends. adbd's vary with how the
#: command writes: ``screencap -p`` arrived in 53 packets of up to 32,816 bytes,
#: ``head -c 200000 /dev/zero`` in two (196,608 and 3,392).
SHELL_PACKET_MAX = 32 * 1024

_STDIN, _STDOUT, _STDERR, _EXIT, _CLOSE_STDIN = 0, 1, 2, 3, 4
_HEADER = struct.Struct("<BI")
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


class ShellIO:
    """What a streaming command sees: its stdin as it arrives, and its two output streams."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._stdin_closed = False

    async def read_stdin(self) -> bytes | None:
        """The next data written to stdin; ``None`` once the client closed stdin or hung up."""
        while not self._stdin_closed:
            try:
                ident, length = _HEADER.unpack(await self._reader.readexactly(_HEADER.size))
                data = await self._reader.readexactly(length)
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            if ident == _CLOSE_STDIN:
                break
            if ident == _STDIN and data:
                return data
        self._stdin_closed = True
        return None

    async def write(self, data: bytes, *, stderr: bool = False) -> None:
        """Write *data* to stdout, or to stderr."""
        ident = _STDERR if stderr else _STDOUT
        for start in range(0, len(data), SHELL_PACKET_MAX):
            self._writer.write(_packet(ident, data[start : start + SHELL_PACKET_MAX]))
        await self._writer.drain()


#: A command that runs while it reads stdin and writes output; it returns its exit code.
StreamingCommand = Callable[[ShellIO], Awaitable[int]]
#: A serial's streaming commands: the one that runs a command line, or ``None``
#: to answer that line from the device script.
Streams = Callable[[str], StreamingCommand | None]
#: Serves one connection to an abstract socket, both ways, until it returns.
SocketServer = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
#: A serial's abstract sockets: what listens on a name, or ``None`` when nothing does.
Sockets = Callable[[str], SocketServer | None]


@dataclass(frozen=True)
class DeviceRequest:
    """One service sent to a device: one round trip to it."""

    serial: str
    service: str


class FakeAdbServer:
    """A loopback ADB server for *devices*, serial → :data:`Device`; an async context manager.

    *streams* and *sockets* (serial → hook) add a serial's streaming commands
    and abstract sockets; each hook is asked anew for every request, so a test
    can start or stop a socket's server while the server runs.

    Inside the ``async with`` block, :attr:`host` and :attr:`port` are where a
    client connects: *port*, or an ephemeral port when it is 0. Leaving it
    stops listening and drops every connection at once, so neither a held
    answer nor a client that stays connected, or that stopped reading, can
    keep the test waiting. Then it raises the first error a connection hit, so
    a fault in a device script or in this fake fails the test instead of only
    cutting an answer short.
    """

    def __init__(
        self,
        devices: Mapping[str, Device],
        *,
        port: int = 0,
        streams: Mapping[str, Streams] | None = None,
        sockets: Mapping[str, Sockets] | None = None,
    ) -> None:
        self.host = "127.0.0.1"
        self.port = port
        #: Every service sent to a device, in arrival order.
        self.requests: list[DeviceRequest] = []
        self._devices = dict(devices)
        self._streams = dict(streams or {})
        self._sockets = dict(sockets or {})
        self._server: asyncio.Server | None = None
        self._connections: dict[asyncio.Task[None], asyncio.StreamWriter] = {}
        self._closing = False

    def round_trips(self, serial: str) -> int:
        """How many services were sent to *serial*."""
        return sum(1 for request in self.requests if request.serial == serial)

    async def __aenter__(self) -> FakeAdbServer:
        self._server = await asyncio.start_server(self._accept, self.host, self.port)
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
                # Ending the answer (``write_eof``) on a socket the client already reset
                # fails with ENOTCONN, an OSError that is not a ConnectionError (seen on
                # macOS). Any other error is a fault in the device script and fails the test.
                try:
                    await self._answer(serial, request, reader, writer)
                except OSError as exc:
                    if not isinstance(exc, ConnectionError) and exc.errno != errno.ENOTCONN:
                        raise
                return
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _answer(
        self,
        serial: str,
        request: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        device = self._devices[serial]
        service, _, command = request.partition(":")
        kind, *options = service.split(",")
        if request == "root:":
            result = await device(request)
            if result.exit_code:
                await _fail(writer, result.stderr.decode())
                return
            writer.write(b"OKAY" + result.stdout)
            await writer.drain()
            return
        if kind == "localabstract" and not options:
            listening = self._sockets.get(serial)
            server = listening(command) if listening is not None else None
            if server is None:
                await _fail(writer, "closed")
                return
            writer.write(b"OKAY")
            await writer.drain()
            await server(reader, writer)
            return
        if not (kind == "shell" and "v2" in options) and not (kind == "exec" and not options):
            await _fail(writer, _unspoken(service))
            return
        # The stream is open once the device accepts it, before the command ends.
        writer.write(b"OKAY")
        await writer.drain()
        streams = self._streams.get(serial)
        streaming = streams(command) if streams is not None and kind == "shell" else None
        if streaming is not None:
            shell = ShellIO(reader, writer)
            exit_code = await streaming(shell)
            writer.write(_packet(_EXIT, bytes([exit_code])))
            writer.write_eof()
            while await shell.read_stdin() is not None:
                pass
            await _drop_input(reader)
            return
        # Read what the client sends from now on (stdin, close-stdin) and drop it
        # until it hangs up: closing a socket with unread input resets the
        # connection, and so does input arriving after it closed.
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
            writer.write_eof()
            await discard
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
    return (
        "this fake speaks host:transport:, shell,v2:, exec:, localabstract: and root:, "
        f"not {request!r}"
    )


async def _fail(writer: asyncio.StreamWriter, message: str) -> None:
    data = message.encode()
    writer.write(b"FAIL" + b"%04x" % len(data) + data)
    await writer.drain()


def _packet(ident: int, data: bytes) -> bytes:
    return _HEADER.pack(ident, len(data)) + data
