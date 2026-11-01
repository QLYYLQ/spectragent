"""The channel to a device-side program: frames, the hello, request ids, pushed events.

The host side (``spectragent/core/capabilities/android/device/channel.py``)
talks to :class:`~tests._helpers.fake_device_side.FakeProgram`, which speaks the
device's end of the protocol, through the fake ADB server's abstract socket, as
it reaches the APK's service through the run's adb server.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.device.channel import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    Channel,
    ChannelClosed,
    Component,
    EventsLost,
    FrameError,
    HandshakeRefused,
    JsonObject,
    RemoteError,
    encode_frame,
    read_frame,
)
from tests._helpers.fake_adb import FakeAdbServer, SocketServer, scripted
from tests._helpers.fake_device_side import Answer, Failure, FakeProgram, Method

SERIAL = "emulator-5554"
SOCKET = "spectragent_device"
TOKEN = "session-token-0123456789"
#: Long enough for a loaded machine; a protocol deadlock fails instead of hanging.
_WAIT_S = 10


async def _pong(params: JsonObject) -> Answer:
    return Answer({"pong": params.get("n", 0)})


def _program(
    component: Component = "service",
    *,
    methods: dict[str, Method] | None = None,
    protocol: int = PROTOCOL_VERSION,
    refuses_other_protocols: bool = True,
    max_unsent_events: int | None = None,
    apk_sha256: str = "0" * 64,
    accessibility_tool: bool | None = None,
) -> FakeProgram:
    return FakeProgram(
        component=component,
        token=TOKEN,
        methods={"ping": _pong, **(methods or {})},
        protocol=protocol,
        refuses_other_protocols=refuses_other_protocols,
        max_unsent_events=max_unsent_events,
        apk_sha256=apk_sha256,
        accessibility_tool=accessibility_tool,
    )


@asynccontextmanager
async def _streams(
    program: FakeProgram,
) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    listening: dict[str, SocketServer] = {SOCKET: program.serve}
    async with FakeAdbServer({SERIAL: scripted({})}, sockets={SERIAL: listening.get}) as adb:
        yield await AdbClient(adb.host, adb.port).connect_abstract(SERIAL, SOCKET)


@asynccontextmanager
async def _channel(
    program: FakeProgram, *, token: str = TOKEN, component: Component = "service"
) -> AsyncIterator[Channel]:
    async with _streams(program) as (reader, writer):
        async with asyncio.timeout(_WAIT_S):
            channel = await Channel.open(reader, writer, token=token, component=component)
        try:
            yield channel
        finally:
            await channel.close()


def _reader_of(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


# ── frames ────────────────────────────────────────────────────────────


async def test_a_frame_carries_its_json_header_and_binary_payload_unchanged() -> None:
    header: JsonObject = {"kind": "response", "id": 7, "result": {"text": "设置", "n": [1, 2.5]}}
    payload = b"\x89PNG\x00\xff" * 1000
    reader = _reader_of(encode_frame(header, payload) + encode_frame({"kind": "event"}))

    first, second, end = (
        await read_frame(reader),
        await read_frame(reader),
        await read_frame(reader),
    )

    assert first is not None and (first.header, first.payload) == (header, payload)
    assert second is not None and (second.header, second.payload) == ({"kind": "event"}, b"")
    assert end is None, "a stream that ends between frames ends cleanly"


async def test_the_lengths_are_big_endian_and_count_everything_after_themselves() -> None:
    frame = encode_frame({"a": 1}, b"xyz")

    length, header_length = struct.unpack(">II", frame[:8])

    assert header_length == len(b'{"a":1}') and length == 4 + header_length + 3 == len(frame) - 4


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (struct.pack(">II", MAX_FRAME_BYTES + 1, 2) + b"{}", "a frame of"),
        (struct.pack(">II", 10, 20) + b"x" * 6, "with a header of 20"),
        (struct.pack(">II", 12, 8) + b"not json", "not JSON"),
        (struct.pack(">II", 6, 2) + b"[]", "a JSON list, not an object"),
        (struct.pack(">II", 100, 2) + b"{}", "ended 2 bytes into a frame of 100"),
        (b"\x00\x00", "inside a frame's lengths"),
    ],
)
async def test_a_frame_that_breaks_the_protocol_is_refused(raw: bytes, message: str) -> None:
    with pytest.raises(FrameError, match=message):
        await read_frame(_reader_of(raw))


def test_a_frame_longer_than_the_protocol_allows_is_never_sent() -> None:
    with pytest.raises(FrameError, match="the protocol allows"):
        encode_frame({}, bytes(MAX_FRAME_BYTES))


# ── the hello ─────────────────────────────────────────────────────────


async def test_the_hello_returns_how_the_program_describes_itself() -> None:
    program = _program(apk_sha256="ab" * 32, accessibility_tool=False)
    async with _channel(program) as channel:
        hello = channel.hello

    assert (hello.protocol, hello.component, hello.package) == (
        PROTOCOL_VERSION,
        "service",
        "org.spectragent.device",
    )
    assert (hello.apk_sha256, hello.accessibility_tool) == ("ab" * 32, False)


async def test_a_device_of_another_protocol_refuses_the_hello() -> None:
    async with _streams(_program(protocol=PROTOCOL_VERSION + 1)) as (reader, writer):
        with pytest.raises(HandshakeRefused, match="speaks protocol") as refused:
            await Channel.open(reader, writer, token=TOKEN, component="service")

    assert refused.value.reason == "protocol"


async def test_the_host_refuses_a_device_that_greets_in_another_protocol() -> None:
    newer = _program(protocol=PROTOCOL_VERSION + 1, refuses_other_protocols=False)
    async with _streams(newer) as (reader, writer):
        with pytest.raises(HandshakeRefused, match="install the APK built from this") as refused:
            await Channel.open(reader, writer, token=TOKEN, component="service")

    assert refused.value.reason == "protocol"


async def test_a_wrong_token_is_refused_and_no_request_is_served() -> None:
    program = _program()
    async with _streams(program) as (reader, writer):
        with pytest.raises(HandshakeRefused, match="not this session's token") as refused:
            await Channel.open(reader, writer, token="someone-elses-token", component="service")

    assert refused.value.reason == "token"
    assert program.calls == []


async def test_another_program_than_the_one_asked_for_is_refused() -> None:
    async with _streams(_program("helper")) as (reader, writer):
        with pytest.raises(HandshakeRefused, match="the service was asked for") as refused:
            await Channel.open(reader, writer, token=TOKEN, component="service")

    assert refused.value.reason == "component"


# ── requests ──────────────────────────────────────────────────────────


async def test_side_by_side_calls_each_get_their_own_answer() -> None:
    async with _channel(_program()) as channel:
        async with asyncio.timeout(_WAIT_S):
            replies = await asyncio.gather(*(channel.call("ping", {"n": n}) for n in range(50)))

    assert [reply.result["pong"] for reply in replies] == list(range(50))


async def test_the_answer_to_a_cancelled_call_is_dropped_and_the_next_call_gets_its_own() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(params: JsonObject) -> Answer:
        started.set()
        await release.wait()
        return Answer({"late": True})

    program = _program(methods={"slow": slow})
    async with _channel(program) as channel:
        call = asyncio.ensure_future(channel.call("slow"))
        async with asyncio.timeout(_WAIT_S):
            await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        release.set()
        async with asyncio.timeout(_WAIT_S):
            reply = await channel.call("ping", {"n": 3})

    assert reply.result == {"pong": 3}
    assert [method for method, _ in program.calls] == ["slow", "ping"]


async def test_a_payload_comes_back_with_its_answer() -> None:
    async def screenshot(params: JsonObject) -> Answer:
        return Answer({"format": "png"}, b"\x89PNG" + bytes(100_000))

    async with _channel(_program(methods={"screenshot": screenshot})) as channel:
        reply = await channel.call("screenshot")

    assert reply.result == {"format": "png"} and len(reply.payload) == 100_004


async def test_an_error_answer_raises_with_the_devices_code() -> None:
    async def cancelled(params: JsonObject) -> Answer:
        raise Failure("gesture_cancelled", "the system cancelled the gesture")

    async with _channel(_program(methods={"gesture": cancelled})) as channel:
        with pytest.raises(RemoteError, match="the system cancelled") as error:
            await channel.call("gesture")
        assert (await channel.call("ping")).result == {"pong": 0}, "the channel stays open"

    assert (error.value.method, error.value.code) == ("gesture", "gesture_cancelled")


async def test_when_the_device_hangs_up_every_waiting_call_and_later_call_fails() -> None:
    hang_up = asyncio.Event()

    async def never(params: JsonObject) -> Answer:
        await hang_up.wait()
        raise ConnectionError  # the program goes away without answering

    async with _channel(_program(methods={"never": never})) as channel:
        waiting = asyncio.ensure_future(channel.call("never"))
        await asyncio.sleep(0)
        hang_up.set()
        async with asyncio.timeout(_WAIT_S):
            with pytest.raises(ChannelClosed, match="never: the device closed the channel"):
                await waiting
        with pytest.raises(ChannelClosed):
            await channel.call("ping")
        assert channel.closed


# ── events ────────────────────────────────────────────────────────────


async def test_events_arrive_in_order_numbered_from_the_subscription_and_before_later_answers() -> (
    None
):
    program = _program()
    program.emit(type="before")  # seq 0, before anyone subscribed
    async with _channel(program) as channel:
        stream = await channel.subscribe()
        emitted = [program.emit(type="TYPE_WINDOW_CONTENT_CHANGED", n=n) for n in range(500)]
        async with asyncio.timeout(_WAIT_S):
            await channel.call("ping")  # answered after every event above was queued
        received = [stream.get_nowait() for _ in range(500)]
        assert stream.get_nowait() is None

    assert emitted == list(range(1, 501))
    assert [event["seq"] for event in received if event is not None] == emitted
    assert [event["n"] for event in received if event is not None] == list(range(500))


async def test_a_slow_consumer_loses_nothing_and_answers_do_not_wait_for_it() -> None:
    program = _program()
    async with _channel(program) as channel:
        stream = await channel.subscribe()
        for n in range(2000):
            program.emit(n=n)
        async with asyncio.timeout(_WAIT_S):
            # Nothing has read an event yet, and the device still answers at once.
            assert (await channel.call("ping", {"n": 1})).result == {"pong": 1}
            received = []
            for _ in range(2000):
                received.append(await stream.get())
                if len(received) % 500 == 0:
                    await asyncio.sleep(0.01)  # the consumer is busy elsewhere

    assert [event["n"] for event in received] == list(range(2000))


async def test_a_consumer_too_far_behind_gets_every_kept_event_then_an_explicit_end() -> None:
    program = _program()
    async with _channel(program) as channel:
        stream = await channel.subscribe(max_buffered=10)
        for n in range(25):
            program.emit(n=n)
        await channel.call("ping")  # every event above has reached the host
        kept = [(await stream.get())["n"] for _ in range(10)]
        with pytest.raises(EventsLost, match="10 events waited unread") as lost:
            await stream.get()

    assert kept == list(range(10))
    assert lost.value.next_seq == 10


async def test_an_overflow_on_the_device_ends_the_stream_after_every_event_before_it() -> None:
    program = _program(max_unsent_events=5)
    async with _channel(program) as channel:
        stream = await channel.subscribe()
        for n in range(8):  # queued at once, before the device's writer runs
            program.emit(n=n)
        async with asyncio.timeout(_WAIT_S):
            sent = [(await stream.get())["n"] for _ in range(5)]
            with pytest.raises(EventsLost, match="events from 5 on were not sent") as lost:
                await stream.get()

    assert sent == list(range(5))
    assert lost.value.next_seq == 5


async def test_a_gap_in_the_numbering_ends_the_stream_where_the_lost_event_was_due() -> None:
    program = _program()
    async with _channel(program) as channel:
        stream = await channel.subscribe()
        program.emit(n=0)
        program.emit(n=1)
        program.lose_event()  # seq 2 never leaves the device
        program.emit(n=3)
        async with asyncio.timeout(_WAIT_S):
            delivered = [(await stream.get())["n"] for _ in range(2)]
            with pytest.raises(EventsLost, match="event 3 arrived where 2 was due") as lost:
                await stream.get()

    assert delivered == [0, 1]
    assert lost.value.next_seq == 2


async def test_the_helper_pushes_no_events() -> None:
    async with _channel(_program("helper"), component="helper") as channel:
        with pytest.raises(RemoteError) as refused:
            await channel.subscribe()

    assert refused.value.code == "unknown_method"


async def test_a_closed_channel_ends_its_event_stream() -> None:
    async with _channel(_program()) as channel:
        stream = await channel.subscribe()
        await channel.close()
        with pytest.raises(ChannelClosed, match="the channel was closed"):
            await stream.get()
