"""The host-protocol client talks to an adb server as adb does: one job per connection.

Driven by ``tests/_helpers/fake_adb.py``, which answers as adb 1.0.41 did on
the emulator, and by a few lines of raw server where a reply has to break the
protocol in a way the fake never does.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from spectragent.core.capabilities.android.adb import (
    MAX_REQUEST_BYTES,
    AdbClient,
    AdbError,
    ShellOutput,
)
from tests._helpers.fake_adb import (
    SHELL_PACKET_MAX,
    FakeAdbServer,
    ShellIO,
    ShellResult,
    SocketServer,
    StreamingCommand,
    scripted,
)

SERIAL = "emulator-5554"
#: Long enough for a loaded machine; a protocol deadlock fails instead of hanging.
_WAIT_S = 10


async def test_a_command_returns_each_stream_and_its_exit_code_in_one_round_trip() -> None:
    device = scripted(
        {"ls /sdcard": ShellResult(stdout=b"Download\n", stderr=b"warn\n", exit_code=2)}
    )
    async with FakeAdbServer({SERIAL: device}) as adb:
        result = await AdbClient(adb.host, adb.port).shell(SERIAL, "ls /sdcard")

    assert (result.stdout, result.stderr, result.exit_code) == (b"Download\n", b"warn\n", 2)
    assert [request.service for request in adb.requests] == ["shell,v2:ls /sdcard"]


async def test_output_spread_over_several_packets_comes_back_whole() -> None:
    png = b"\x89PNG\r\n\x1a\n" + bytes(2 * SHELL_PACKET_MAX + 7)
    device = scripted({"screencap -p": ShellResult(stdout=png)})
    async with FakeAdbServer({SERIAL: device}) as adb:
        result = await AdbClient(adb.host, adb.port).shell(SERIAL, "screencap -p")

    assert result.stdout == png


async def test_root_returns_what_adbd_answered_before_restarting() -> None:
    device = scripted({"root:": ShellResult(stdout=b"restarting adbd as root\n")})
    async with FakeAdbServer({SERIAL: device}) as adb:
        answer = await AdbClient(adb.host, adb.port).root(SERIAL)

    assert answer == "restarting adbd as root"
    assert [request.service for request in adb.requests] == ["root:"]


async def test_an_unknown_device_is_refused_with_the_servers_reason() -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        with pytest.raises(AdbError, match="emulator-9998' not found"):
            await AdbClient(adb.host, adb.port).shell("emulator-9998", "true")

    assert adb.requests == []


async def test_calls_run_side_by_side_on_their_own_connections() -> None:
    release = asyncio.Event()

    async def device(command: str) -> ShellResult:
        if command == "slow":
            await release.wait()
        return ShellResult(stdout=command.encode())

    async with FakeAdbServer({SERIAL: device}) as adb:
        client = AdbClient(adb.host, adb.port)
        slow = asyncio.ensure_future(client.shell(SERIAL, "slow"))
        async with asyncio.timeout(_WAIT_S):
            fast = await client.shell(SERIAL, "fast")
        assert not slow.done()
        release.set()
        async with asyncio.timeout(_WAIT_S):
            assert (await slow).stdout == b"slow"
    assert fast.stdout == b"fast"


async def test_a_cancelled_call_hangs_up_and_the_next_call_works() -> None:
    held = asyncio.Event()
    hung_up = asyncio.Event()

    async def device(command: str) -> ShellResult:
        if command == "hold":
            held.set()
            await hung_up.wait()
        return ShellResult(stdout=b"ok")

    async with FakeAdbServer({SERIAL: device}) as adb:
        client = AdbClient(adb.host, adb.port)
        call = asyncio.ensure_future(client.shell(SERIAL, "hold"))
        async with asyncio.timeout(_WAIT_S):
            await held.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        hung_up.set()
        async with asyncio.timeout(_WAIT_S):
            assert (await client.shell(SERIAL, "after")).stdout == b"ok"


async def test_a_request_too_long_for_the_protocol_is_refused_before_connecting() -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        with pytest.raises(ValueError, match="at most 65535 bytes"):
            await AdbClient(adb.host, adb.port).shell(SERIAL, "x" * MAX_REQUEST_BYTES)

    assert adb.requests == []


async def test_stdin_given_to_a_command_reaches_it_whole_and_is_then_closed() -> None:
    received = bytearray()

    async def install(shell: ShellIO) -> int:
        while (data := await shell.read_stdin()) is not None:
            received.extend(data)
        await shell.write(b"Success\n")
        return 0

    apk = bytes(range(256)) * 600  # several stdin packets
    streams = {SERIAL: lambda command: install if command.startswith("cmd package") else None}
    async with (
        FakeAdbServer({SERIAL: scripted({})}, streams=streams) as adb,
        asyncio.timeout(_WAIT_S),
    ):
        result = await AdbClient(adb.host, adb.port).shell(
            SERIAL, f"cmd package install -S {len(apk)}", stdin=apk
        )

    assert bytes(received) == apk, "every byte, then the end of stdin"
    assert (result.stdout, result.exit_code) == (b"Success\n", 0)


async def test_an_open_session_is_read_as_the_command_writes_and_ends_when_stdin_closes() -> None:
    async def program(shell: ShellIO) -> int:
        token = await shell.read_stdin()
        await shell.write(b"READY " + (token or b"").strip() + b"\n")
        await shell.write(b"a warning\n", stderr=True)
        while await shell.read_stdin() is not None:  # it runs until its stdin ends
            pass
        return 3

    streams: dict[str, Callable[[str], StreamingCommand | None]] = {SERIAL: lambda _: program}
    async with FakeAdbServer({SERIAL: scripted({})}, streams=streams) as adb:
        async with asyncio.timeout(_WAIT_S):
            session = await AdbClient(adb.host, adb.port).open_shell(SERIAL, "app_process / Main")
        try:
            async with asyncio.timeout(_WAIT_S):
                await session.write(b"secret\n")
                first = await session.read()
                second = await session.read()
                assert session.exit_code is None, "still running while its stdin is open"
                await session.close_stdin()
                assert await session.read() is None
        finally:
            await session.close()

    assert first == ShellOutput("stdout", b"READY secret\n")
    assert second == ShellOutput("stderr", b"a warning\n")
    assert session.exit_code == 3
    assert [request.service for request in adb.requests] == ["shell,v2:app_process / Main"]


async def test_an_abstract_socket_carries_bytes_both_ways() -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"echo:" + await reader.readexactly(4))
        await writer.drain()

    listening: dict[str, SocketServer] = {"spectragent_device": echo}
    async with FakeAdbServer({SERIAL: scripted({})}, sockets={SERIAL: listening.get}) as adb:
        async with asyncio.timeout(_WAIT_S):
            reader, writer = await AdbClient(adb.host, adb.port).connect_abstract(
                SERIAL, "spectragent_device"
            )
        try:
            writer.write(b"ping")
            await writer.drain()
            async with asyncio.timeout(_WAIT_S):
                answer = await reader.readexactly(9)
        finally:
            writer.close()

    assert answer == b"echo:ping"
    assert [request.service for request in adb.requests] == ["localabstract:spectragent_device"]


async def test_an_abstract_socket_nothing_listens_on_is_refused_as_closed() -> None:
    async with FakeAdbServer({SERIAL: scripted({})}, sockets={SERIAL: lambda _: None}) as adb:
        with pytest.raises(AdbError, match="localabstract:nobody: closed"):
            async with asyncio.timeout(_WAIT_S):
                await AdbClient(adb.host, adb.port).connect_abstract(SERIAL, "nobody")


# ── replies the fake never sends ─────────────────────────────────────

#: What one raw connection does once both requests were answered ``OKAY``.
Reply = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@contextlib.asynccontextmanager
async def raw_server(reply: Reply) -> AsyncIterator[int]:
    """A server that answers both requests ``OKAY``, then lets *reply* finish; yields its port."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            for _ in range(2):
                await reader.readexactly(int(await reader.readexactly(4), 16))
                writer.write(b"OKAY")
            await reply(reader, writer)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def test_the_commands_stdin_is_closed_at_once() -> None:
    received: list[bytes] = []

    async def reply(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readexactly(5))
        writer.write(struct.pack("<BI", 3, 1) + b"\x00")
        await writer.drain()

    async with raw_server(reply) as port:
        async with asyncio.timeout(_WAIT_S):
            await AdbClient("127.0.0.1", port).shell(SERIAL, "cat")

    assert received == [struct.pack("<BI", 4, 0)], "a close-stdin packet, so `cat` sees EOF"


async def test_a_stream_that_ends_before_the_exit_code_is_an_error() -> None:
    async def reply(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(struct.pack("<BI", 1, 3) + b"out")
        await writer.drain()

    async with raw_server(reply) as port:
        with pytest.raises(AdbError, match="ended before the exit code"):
            await AdbClient("127.0.0.1", port).shell(SERIAL, "echo out")


async def test_a_refused_service_names_the_program_and_never_its_arguments() -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(int(await reader.readexactly(4), 16))
        writer.write(b"OKAY")
        await reader.readexactly(int(await reader.readexactly(4), 16))
        writer.write(b"FAIL0006closed")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(AdbError) as refused:
            await AdbClient("127.0.0.1", port).shell(SERIAL, "input text 'hunter2'")
    finally:
        server.close()
        await server.wait_closed()

    assert "'input'" in str(refused.value) and "closed" in str(refused.value)
    assert "hunter2" not in str(refused.value)
