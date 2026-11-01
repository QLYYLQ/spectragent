"""The host side of ADB: an asyncio client for an adb server's host protocol.

``docs/architecture_design.md`` §11.2 ("主机传输层"). The protocol is small. A
request is four hex digits giving its length, then that many bytes; the server
answers ``OKAY``, or ``FAIL`` followed by four hex digits and a message. A
connection does one job: ``host:transport:<serial>`` binds it to a device, one
device service runs on it, and the connection ends with the service.
Concurrency is several connections. No maintained, asyncio-native Python ADB
library exists (checked 2026-10), so this one is ours.

It speaks only what the capability uses:

- ``host:transport:<serial>``, answered by the server;
- ``shell,v2:<command>``, answered by the device in shell-protocol packets: an
  id byte (1 stdout, 2 stderr, 3 exit), a little-endian four-byte length, the
  payload; the exit packet's one byte is the exit code. Unlike ``exec:`` it
  keeps stderr out of stdout (a warning would otherwise land inside a
  screenshot's PNG bytes) and says how the command exited. The client writes
  the command's stdin in the same packets (id 0), and id 4 closes it;
- ``localabstract:<name>``, a stream to the device's abstract Unix socket
  *name*: once the server answers ``OKAY`` the connection carries that
  socket's bytes both ways, so no forwarded host port is needed (adbd refuses
  a name nothing listens on, and the server answers ``FAIL closed``);
- ``root:``, answered by adbd in words before it restarts as root.

``tests/_helpers/fake_adb.py`` answers exactly this, as adb 1.0.41 did on the
emulator (2026-11-01).
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from dataclasses import dataclass
from typing import Literal

#: Shell-protocol packet ids (adb's ``shell_protocol.h``).
_STDIN, _STDOUT, _STDERR, _EXIT, _CLOSE_STDIN = 0, 1, 2, 3, 4
_HEADER = struct.Struct("<BI")
#: The largest request four hex digits can announce.
MAX_REQUEST_BYTES = 0xFFFF
#: The largest stdin packet sent. adbd splits a packet larger than its own
#: buffer (1 MiB) when it reads it, so any size works; this one matches what
#: adbd itself sent (up to 32,816 bytes per stdout packet, measured).
_STDIN_PACKET_MAX = 32 * 1024


class AdbError(Exception):
    """The adb server or the device refused a request, or a reply broke off."""


@dataclass(frozen=True)
class ShellResult:
    """What a command printed on each stream, and the code it exited with."""

    stdout: bytes
    stderr: bytes
    exit_code: int


@dataclass(frozen=True)
class ShellOutput:
    """One piece of a running command's output, on the stream it wrote it to."""

    stream: Literal["stdout", "stderr"]
    data: bytes


class ShellSession:
    """A ``shell,v2`` command that runs while its output is read and its stdin written.

    It owns one connection. :meth:`close` hangs up, which the command sees as
    the end of its stdin; adbd does not signal it otherwise.
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, service: str
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._service = service
        #: The command's exit code, once :meth:`read` met its exit packet.
        self.exit_code: int | None = None

    async def write(self, data: bytes) -> None:
        """Write *data* to the command's stdin."""
        for start in range(0, len(data), _STDIN_PACKET_MAX):
            chunk = data[start : start + _STDIN_PACKET_MAX]
            self._writer.write(_HEADER.pack(_STDIN, len(chunk)) + chunk)
            await self._writer.drain()

    async def close_stdin(self) -> None:
        """Close the command's stdin; a command reading it sees the end."""
        self._writer.write(_HEADER.pack(_CLOSE_STDIN, 0))
        await self._writer.drain()

    async def read(self) -> ShellOutput | None:
        """The next piece of output, or ``None`` once the command exited (:attr:`exit_code` is set).

        Raises:
            AdbError: The stream ended before the exit packet, or a packet
                broke the protocol.
        """
        if self.exit_code is not None:
            return None
        try:
            header = await self._reader.readexactly(_HEADER.size)
        except asyncio.IncompleteReadError:
            raise AdbError(
                f"{_describe(self._service)}: the stream ended before the exit code"
            ) from None
        ident, length = _HEADER.unpack(header)
        payload = await _read(self._reader, length, self._service)
        if ident == _STDOUT:
            return ShellOutput("stdout", payload)
        if ident == _STDERR:
            return ShellOutput("stderr", payload)
        if ident == _EXIT:
            if len(payload) != 1:
                raise AdbError(
                    f"{_describe(self._service)}: an exit packet of {len(payload)} bytes"
                )
            self.exit_code = payload[0]
            return None
        raise AdbError(f"{_describe(self._service)}: unexpected shell packet id {ident}")

    async def close(self) -> None:
        """Hang up. Safe to repeat."""
        await _hang_up(self._writer)


class AdbClient:
    """Speaks the host protocol to one adb server; every call opens its own connection."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    async def shell(self, serial: str, command: str, *, stdin: bytes = b"") -> ShellResult:
        """Run *command* through the device's shell (``sh -c``) and collect its output.

        *stdin* is written to the command's stdin, which is then closed: a
        command that reads it would otherwise wait forever. A cancelled call
        closes its connection.

        Raises:
            AdbError: The server refused the device or the service, or the
                stream ended before the exit packet.
            OSError: The server could not be reached.
            ValueError: The request is longer than the protocol allows.
        """
        session = await self.open_shell(serial, command)
        try:
            await session.write(stdin)
            await session.close_stdin()
            stdout, stderr = bytearray(), bytearray()
            while (output := await session.read()) is not None:
                (stdout if output.stream == "stdout" else stderr).extend(output.data)
            assert session.exit_code is not None  # read() returns None only after the exit packet
            return ShellResult(bytes(stdout), bytes(stderr), session.exit_code)
        finally:
            await session.close()

    async def open_shell(self, serial: str, command: str) -> ShellSession:
        """Start *command* through the device's shell and hand over its running session.

        The caller closes it. Its stdin stays open until
        :meth:`ShellSession.close_stdin` or :meth:`ShellSession.close`.

        Raises:
            AdbError: The server refused the device or the service.
            OSError: The server could not be reached.
            ValueError: The request is longer than the protocol allows.
        """
        service = f"shell,v2:{command}"
        if len(service.encode()) > MAX_REQUEST_BYTES:
            raise ValueError(
                f"an adb request carries at most {MAX_REQUEST_BYTES} bytes; "
                f"{_describe(service)} needs {len(service.encode())}"
            )
        reader, writer = await self._open(serial, service)
        return ShellSession(reader, writer, service)

    async def connect_abstract(
        self, serial: str, name: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A stream to the abstract Unix socket *name* on *serial*; the caller closes it.

        Raises:
            AdbError: The server refused the device, or nothing on the device
                listens on *name* (the server answers ``closed``).
            OSError: The server could not be reached.
        """
        return await self._open(serial, f"localabstract:{name}")

    async def root(self, serial: str) -> str:
        """Ask adbd on *serial* to run as root, and return its answer.

        adbd answers ``restarting adbd as root`` and restarts, which drops the
        device for every client for about 1.5 s (the emulator, API 33,
        measured), or ``adbd is already running as root``; a build that
        refuses says so in words.

        Raises:
            AdbError: The server refused the device or the service.
            OSError: The server could not be reached.
        """
        reader, writer = await self._open(serial, "root:")
        try:
            return (await reader.read()).decode(errors="replace").strip()
        finally:
            await _hang_up(writer)

    async def _open(
        self, serial: str, service: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A connection bound to *serial* on which the device accepted *service*."""
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            await _request(reader, writer, f"host:transport:{serial}")
            await _request(reader, writer, service)
        except BaseException:
            await _hang_up(writer)
            raise
        return reader, writer


async def _hang_up(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


async def _request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: str
) -> None:
    """Send one request and read its status; ``FAIL`` raises with the server's message."""
    data = request.encode()
    writer.write(b"%04x" % len(data) + data)
    await writer.drain()
    status = await _read(reader, 4, request)
    if status == b"OKAY":
        return
    if status != b"FAIL":
        raise AdbError(f"{_describe(request)}: unexpected reply {status!r}")
    head = await _read(reader, 4, request)
    try:
        length = int(head, 16)
    except ValueError:
        raise AdbError(f"{_describe(request)}: malformed failure length {head!r}") from None
    message = (await _read(reader, length, request)).decode(errors="replace")
    raise AdbError(f"{_describe(request)}: {message}")


async def _read(reader: asyncio.StreamReader, size: int, request: str) -> bytes:
    try:
        return await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise AdbError(
            f"{_describe(request)}: the connection closed after {len(exc.partial)} of {size} bytes"
        ) from None


def _describe(request: str) -> str:
    """The request without a shell command's arguments, which can carry typed text."""
    service, sep, command = request.partition(":")
    if service.startswith("shell"):
        program = command.split(maxsplit=1)[0] if command.strip() else ""
        return f"{service} {program!r}"
    return f"{service}{sep}{command}"
