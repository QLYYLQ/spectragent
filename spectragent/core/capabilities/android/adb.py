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
  screenshot's PNG bytes) and says how the command exited;
- ``root:``, answered by adbd in words before it restarts as root.

``tests/_helpers/fake_adb.py`` answers exactly this, as adb 1.0.41 did on the
emulator (2026-11-01).
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from dataclasses import dataclass

#: Shell-protocol packet ids (adb's ``shell_protocol.h``).
_STDOUT, _STDERR, _EXIT, _CLOSE_STDIN = 1, 2, 3, 4
_HEADER = struct.Struct("<BI")
#: The largest request four hex digits can announce.
MAX_REQUEST_BYTES = 0xFFFF


class AdbError(Exception):
    """The adb server or the device refused a request, or a reply broke off."""


@dataclass(frozen=True)
class ShellResult:
    """What a command printed on each stream, and the code it exited with."""

    stdout: bytes
    stderr: bytes
    exit_code: int


class AdbClient:
    """Speaks the host protocol to one adb server; every call opens its own connection."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    async def shell(self, serial: str, command: str) -> ShellResult:
        """Run *command* through the device's shell (``sh -c``) and collect its output.

        The command's stdin is closed at once: nothing is ever written to it,
        and a command that reads it would otherwise wait forever. A cancelled
        call closes its connection.

        Raises:
            AdbError: The server refused the device or the service, or the
                stream ended before the exit packet.
            OSError: The server could not be reached.
            ValueError: The request is longer than the protocol allows.
        """
        service = f"shell,v2:{command}"
        if len(service.encode()) > MAX_REQUEST_BYTES:
            raise ValueError(
                f"an adb request carries at most {MAX_REQUEST_BYTES} bytes; "
                f"{_describe(service)} needs {len(service.encode())}"
            )
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            await _request(reader, writer, f"host:transport:{serial}")
            await _request(reader, writer, service)
            writer.write(_HEADER.pack(_CLOSE_STDIN, 0))
            await writer.drain()
            return await _read_shell(reader, service)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

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
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            await _request(reader, writer, f"host:transport:{serial}")
            await _request(reader, writer, "root:")
            return (await reader.read()).decode(errors="replace").strip()
        finally:
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


async def _read_shell(reader: asyncio.StreamReader, service: str) -> ShellResult:
    """Collect the shell-protocol packets of one command up to its exit packet."""
    stdout, stderr = bytearray(), bytearray()
    while True:
        try:
            header = await reader.readexactly(_HEADER.size)
        except asyncio.IncompleteReadError:
            raise AdbError(f"{_describe(service)}: the stream ended before the exit code") from None
        ident, length = _HEADER.unpack(header)
        payload = await _read(reader, length, service)
        if ident == _STDOUT:
            stdout += payload
        elif ident == _STDERR:
            stderr += payload
        elif ident == _EXIT:
            if len(payload) != 1:
                raise AdbError(f"{_describe(service)}: an exit packet of {len(payload)} bytes")
            return ShellResult(bytes(stdout), bytes(stderr), payload[0])
        else:
            raise AdbError(f"{_describe(service)}: unexpected shell packet id {ident}")


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
