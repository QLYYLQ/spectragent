"""The fake ADB server speaks the host protocol as adb does and counts device round trips."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import struct

import pytest

from tests._helpers.fake_adb import SHELL_PACKET_MAX, FakeAdbServer, ShellResult, scripted

SERIAL = "emulator-5554"
OTHER = "emulator-5556"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(2 * SHELL_PACKET_MAX + 7)
#: Long enough for a loaded machine; a protocol deadlock fails instead of hanging.
_WAIT_S = 10


async def _call(adb: FakeAdbServer, *requests: str) -> tuple[list[bytes], bytes]:
    """Send *requests* on one connection: each one's status, then all that follows the last."""
    async with asyncio.timeout(_WAIT_S):
        reader, writer = await asyncio.open_connection(adb.host, adb.port)
        try:
            statuses: list[bytes] = []
            for request in requests:
                data = request.encode()
                writer.write(b"%04x" % len(data) + data)
                status = await reader.readexactly(4)
                if status == b"FAIL":
                    length = int(await reader.readexactly(4), 16)
                    statuses.append(b"FAIL " + await reader.readexactly(length))
                    break
                statuses.append(status)
            return statuses, await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()


def _packets(raw: bytes) -> list[tuple[int, bytes]]:
    """Shell-protocol packets: an id byte, a little-endian length, the payload."""
    packets: list[tuple[int, bytes]] = []
    while raw:
        ident, length = struct.unpack("<BI", raw[:5])
        packets.append((ident, raw[5 : 5 + length]))
        raw = raw[5 + length :]
    return packets


@pytest.mark.parametrize("service", ["shell,v2", "shell,v2,raw"])
async def test_shell_v2_frames_stdout_stderr_and_the_exit_code(service: str) -> None:
    device = scripted(
        {"ls /sdcard": ShellResult(stdout=b"Download\n", stderr=b"warn\n", exit_code=2)}
    )
    async with FakeAdbServer({SERIAL: device}) as adb:
        statuses, raw = await _call(adb, f"host:transport:{SERIAL}", f"{service}:ls /sdcard")

    assert statuses == [b"OKAY", b"OKAY"]
    assert _packets(raw) == [(1, b"Download\n"), (2, b"warn\n"), (3, b"\x02")]


async def test_long_output_arrives_in_several_packets() -> None:
    device = scripted({"screencap -p": ShellResult(stdout=PNG)})
    async with FakeAdbServer({SERIAL: device}) as adb:
        _, raw = await _call(adb, f"host:transport:{SERIAL}", "shell,v2:screencap -p")

    stdout = [data for ident, data in _packets(raw) if ident == 1]
    assert len(stdout) == 3
    assert b"".join(stdout) == PNG


async def test_exec_mixes_stderr_into_the_stream_and_drops_the_exit_code() -> None:
    device = scripted({"screencap -p": ShellResult(stdout=PNG, stderr=b"warn\n", exit_code=1)})
    async with FakeAdbServer({SERIAL: device}) as adb:
        statuses, raw = await _call(adb, f"host:transport:{SERIAL}", "exec:screencap -p")

    assert statuses == [b"OKAY", b"OKAY"]
    assert raw == PNG + b"warn\n"


async def test_an_unknown_command_exits_127_as_sh_does() -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        _, raw = await _call(adb, f"host:transport:{SERIAL}", "shell,v2:nope --now")

    assert _packets(raw) == [
        (2, b"/system/bin/sh: nope: inaccessible or not found\n"),
        (3, b"\x7f"),
    ]


async def test_an_unknown_serial_is_refused_by_name() -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        statuses, _ = await _call(adb, f"host:transport:{OTHER}")

    assert statuses == [f"FAIL device '{OTHER}' not found".encode()]


async def test_a_device_service_without_a_transport_is_refused() -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        statuses, _ = await _call(adb, "shell,v2:getprop")

    assert statuses == [b"FAIL device offline (no transport)"]
    assert adb.requests == []


@pytest.mark.parametrize("service", ["shell:getprop", "exec,raw:getprop", "sync:", "tcp:5037"])
async def test_a_service_this_fake_does_not_speak_is_refused(service: str) -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        statuses, _ = await _call(adb, f"host:transport:{SERIAL}", service)

    assert statuses[-1].startswith(
        b"FAIL this fake speaks host:transport:, shell,v2:, exec:, localabstract: and root:"
    )


@pytest.mark.parametrize("host_request", ["host:version", "host:track-devices"])
async def test_a_host_request_this_fake_does_not_speak_is_refused(host_request: str) -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        statuses, _ = await _call(adb, host_request)
        transported, _ = await _call(adb, f"host:transport:{SERIAL}", host_request)

    refusal = (
        "FAIL this fake speaks host:transport:, shell,v2:, exec:, localabstract: and root:, "
        f"not {host_request!r}"
    )
    assert statuses == [refusal.encode()]
    assert transported == [b"OKAY", refusal.encode()]
    assert adb.requests == [], "a host request is the server's, never a device round trip"


async def test_a_malformed_length_closes_the_connection_unanswered() -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        reader, writer = await asyncio.open_connection(adb.host, adb.port)
        writer.write(b"zzzz")
        reply = await asyncio.wait_for(reader.read(), _WAIT_S)
        writer.close()
        await writer.wait_closed()

    assert reply == b""


async def test_one_capture_costs_one_round_trip_per_source() -> None:
    """What the fake is for: counting what one observation capture sent to the device."""
    device = scripted(
        {
            "screencap -p": ShellResult(stdout=PNG),
            "dumpsys window": ShellResult(
                stdout=b"mCurrentFocus=Window{1 u0 com.android.settings}\n"
            ),
        }
    )
    async with FakeAdbServer({SERIAL: device, OTHER: scripted({})}) as adb:
        # Four requests: two host:transport, answered by the server, and two services.
        await asyncio.gather(
            _call(adb, f"host:transport:{SERIAL}", "exec:screencap -p"),
            _call(adb, f"host:transport:{SERIAL}", "shell,v2:dumpsys window"),
        )

    assert adb.round_trips(SERIAL) == 2
    assert adb.round_trips(OTHER) == 0
    assert sorted(request.service for request in adb.requests) == [
        "exec:screencap -p",
        "shell,v2:dumpsys window",
    ]


async def test_a_held_answer_ends_with_the_server() -> None:
    """OKAY comes before the answer, so a client can time out; the block's end cancels the answer."""
    asked, cancelled = asyncio.Event(), asyncio.Event()

    async def hangs(command: str) -> ShellResult:
        asked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return ShellResult()

    async with FakeAdbServer({SERIAL: hangs}) as adb:
        reader, writer = await asyncio.open_connection(adb.host, adb.port)
        for request in (f"host:transport:{SERIAL}", "exec:screencap -p"):
            writer.write(b"%04x" % len(request) + request.encode())
        assert await asyncio.wait_for(reader.readexactly(8), _WAIT_S) == b"OKAYOKAY"
        await asyncio.wait_for(asked.wait(), _WAIT_S)

    assert cancelled.is_set()
    writer.close()
    await writer.wait_closed()


async def test_clients_that_stay_connected_do_not_hold_the_exit() -> None:
    """One client stopped reading a long answer, one is silent since it connected: both are dropped."""
    device = scripted({"screencap": ShellResult(stdout=bytes(32 * 1024 * 1024))})
    async with asyncio.timeout(_WAIT_S):
        async with FakeAdbServer({SERIAL: device}) as adb:
            reader, stalled = await asyncio.open_connection(adb.host, adb.port)
            for request in (f"host:transport:{SERIAL}", "exec:screencap"):
                stalled.write(b"%04x" % len(request) + request.encode())
            await reader.readexactly(8 + 1024)
            # Last, so the block is left before the server has even seen it.
            _, silent = await asyncio.open_connection(adb.host, adb.port)

    for writer in (stalled, silent):
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def test_a_hang_up_macos_reports_as_not_connected_is_not_the_servers_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ending the answer to a client that already reset fails on macOS with ENOTCONN, which is
    not a ConnectionError. The block's end must not raise it as the fake's own error."""

    def not_connected(_writer: asyncio.StreamWriter) -> None:
        raise OSError(errno.ENOTCONN, os.strerror(errno.ENOTCONN))

    monkeypatch.setattr(asyncio.StreamWriter, "write_eof", not_connected)
    device = scripted({"ls": ShellResult(stdout=b"x\n")})
    async with FakeAdbServer({SERIAL: device}) as adb:
        statuses, _ = await _call(adb, f"host:transport:{SERIAL}", "shell,v2:ls")

    assert statuses == [b"OKAY", b"OKAY"]


@pytest.mark.parametrize(
    "fault", [RuntimeError("a bug"), TimeoutError("timed out")], ids=["an error", "an OSError"]
)
async def test_a_fault_in_a_device_script_fails_the_test(fault: Exception) -> None:
    """The block's end raises it, though the client only sees its answer end early.

    ``TimeoutError`` is an ``OSError``: the filter for a client that hung up must not take it.
    """

    async def faulty(command: str) -> ShellResult:
        raise fault

    with pytest.raises(type(fault)):
        async with FakeAdbServer({SERIAL: faulty}) as adb:
            await _call(adb, f"host:transport:{SERIAL}", "shell,v2:ls")
