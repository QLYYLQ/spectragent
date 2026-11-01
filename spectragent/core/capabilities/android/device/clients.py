"""Typed clients for the two device-side programs, each over its own channel (``channel.py``).

``docs/architecture_design.md`` §11.2 ("设备端"). The accessibility service
(:class:`ServiceClient`) is the main route: the window forest, every
accessibility event, gestures, global actions, screenshots and text. The
shell helper (:class:`HelperClient`) adds what the service cannot do:
screenshots without the service's limit of one per 333 ms, any key, touches
injected as ``input`` injects them, and turning the service on and off.

Each method is one request. None sets a deadline of its own: the device
session's queue bounds every job (``session.py``), and a cancelled call
returns at once and drops the answer when it comes. The device answers one
channel's requests in order, so a request after a cancelled one on the same
channel starts once the cancelled one is done on the device. Only on the same
channel: the service, the helper and adb's own shell are separate paths, and
a long press cut short on the service still runs while a helper screenshot
is taken at once. A job that gives up on a device call therefore pings that
same channel, within a limit, before it hands the device on.

**Gestures are timed strokes** (:class:`Stroke`): each finger's path as
points with the time, in ms from the gesture's start, at which the finger is
there; it goes down at the first point and lifts at the last. A tap is two
points at one place (:meth:`Stroke.hold`, held longer for a long press:
Android takes about 500 ms for one), a swipe two points apart
(:meth:`Stroke.line`). A path of many points keeps its timing, so a speed that
varies along the path survives: the helper injects every event at its planned
time, moving every 8 ms between points; the service chains one dispatch per
segment (``continueStroke``) for one finger, each segment after the first
starting once the system reports the one before it done.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from spectragent.core.capabilities.android.device.channel import (
    Channel,
    EventStream,
    Hello,
    JsonObject,
    Reply,
)
from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityForest

#: How long a tap's finger stays down unless asked otherwise.
TAP_MS = 50
#: A capture's node limit unless asked otherwise: nodes past it that are off
#: the screen are left out (``forest.py``).
MAX_NODES = 5000
GlobalAction = Literal["back", "home", "recents"]
Point = tuple[float, float]


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Ping(_Result):
    """A program's answer to ``ping``: its uptime and, from the service, its next event's ``seq``."""

    uptime_ms: int
    next_seq: int | None = None


class TreeCapture(_Result):
    """One capture of the window forest.

    Attributes:
        forest: Every window on every display, with its nodes.
        node_count: How many nodes the forest holds.
        omitted_offscreen: How many nodes off the screen were left out, each
            with everything under it, because the capture had reached its
            node limit.
        capture_ms: How long the service took to read the forest.
    """

    forest: AndroidAccessibilityForest
    node_count: int
    omitted_offscreen: int
    capture_ms: int


class TextTarget(_Result):
    """The field that took text: its package, its class and its view id, as the tree names them."""

    package_name: str | None = None
    class_name: str | None = None
    view_id_resource_name: str | None = None


class SettingChange(_Result):
    """``enabled_accessibility_services`` before and after; ``None`` is unset."""

    previous: str | None
    current: str | None
    changed: bool


class ServiceEvent(_Result):
    """One accessibility event the service received, numbered by ``seq`` from 0 for its life.

    The fields are the ``AccessibilityEvent``'s own; ``type`` is its name
    (``TYPE_WINDOW_CONTENT_CHANGED``) and ``time_ms`` its uptime in ms.
    """

    seq: int
    type: str
    time_ms: int
    package_name: str | None
    class_name: str | None
    window_id: int
    content_change_types: int
    window_changes: int
    text: tuple[str | None, ...]
    content_description: str | None
    before_text: str | None
    from_index: int
    to_index: int
    item_count: int
    added_count: int
    removed_count: int
    scroll_delta_x: int
    scroll_delta_y: int


@dataclass(frozen=True)
class Screenshot:
    """One screenshot and what it cost on the device.

    Attributes:
        png: The screen as PNG bytes.
        width: Its width in pixels.
        height: Its height in pixels.
        producer: The program that took it.
        capture_ms: How long the capture itself took.
        encode_ms: How long the PNG encoding took.
        waited_ms: How long the service waited first, to keep one screenshot
            per 333 ms; always 0 from the helper.
    """

    png: bytes
    width: int
    height: int
    producer: Literal["accessibility_service", "shell_helper"]
    capture_ms: int
    encode_ms: int
    waited_ms: int


@dataclass(frozen=True)
class Stroke:
    """One finger: ``(x, y, t_ms)`` points, *t_ms* from the gesture's start and never going back.

    The finger goes down at the first point and lifts at the last, so a
    stroke has two points at least.
    """

    points: Sequence[tuple[float, float, int]]

    @classmethod
    def line(cls, start: Point, end: Point, duration_ms: int, *, start_ms: int = 0) -> Stroke:
        """From *start* to *end* at one speed over *duration_ms*: a swipe, or a drag."""
        return cls(((*start, start_ms), (*end, start_ms + duration_ms)))

    @classmethod
    def hold(cls, point: Point, duration_ms: int, *, start_ms: int = 0) -> Stroke:
        """Down at *point* for *duration_ms*: a tap, or a long press when held longer."""
        return cls.line(point, point, duration_ms, start_ms=start_ms)


class ServiceEvents:
    """The service's events, each once, in order (:class:`~spectragent.core.capabilities.android.device.channel.EventStream`)."""

    def __init__(self, stream: EventStream) -> None:
        self._stream = stream

    async def get(self) -> ServiceEvent:
        """The next event; raises like :meth:`EventStream.get` once the stream ended."""
        return ServiceEvent.model_validate(await self._stream.get())

    def get_nowait(self) -> ServiceEvent | None:
        """The next event if it has arrived, else ``None``."""
        event = self._stream.get_nowait()
        return None if event is None else ServiceEvent.model_validate(event)


class ServiceClient:
    """The accessibility service, over an open channel."""

    producer: Literal["accessibility_service"] = "accessibility_service"

    def __init__(self, channel: Channel) -> None:
        self.channel = channel

    @property
    def hello(self) -> Hello:
        """How the service described itself: the installed build, measured on the device."""
        return self.channel.hello

    async def ping(self) -> Ping:
        return Ping.model_validate((await self.channel.call("ping")).result)

    async def tree(self, *, max_nodes: int = MAX_NODES) -> TreeCapture:
        """Capture the window forest; off-screen nodes past *max_nodes* are left out."""
        reply = await self.channel.call("tree", {"max_nodes": max_nodes})
        return TreeCapture.model_validate(reply.result)

    async def screenshot(self) -> Screenshot:
        """The screen, taken by the service (API 30+); waits first if the last one was under 333 ms ago."""
        return _screenshot(await self.channel.call("screenshot"), self.producer)

    async def gesture(self, strokes: Sequence[Stroke]) -> int:
        """Dispatch the strokes as one gesture; returns, once the system completed it, how long it took on the device in ms.

        Two-point strokes may be several fingers at once; a stroke of more
        points is timed for one finger only.

        Raises:
            RemoteError: ``gesture_cancelled`` when the system cancelled it (a
                device without ``android.hardware.faketouch`` cancels every
                gesture); ``bad_params`` past the system's limits (20 strokes,
                60 s) or for a timed path with other fingers.
        """
        return _elapsed(await self.channel.call("gesture", {"strokes": _strokes(strokes)}))

    async def tap(self, x: float, y: float, *, duration_ms: int = TAP_MS) -> int:
        return await self.gesture([Stroke.hold((x, y), duration_ms)])

    async def swipe(self, start: Point, end: Point, *, duration_ms: int) -> int:
        return await self.gesture([Stroke.line(start, end, duration_ms)])

    async def global_action(self, action: GlobalAction) -> None:
        await self.channel.call("global_action", {"action": action})

    async def set_text(self, text: str) -> TextTarget:
        """Replace the text of the field that has input focus (``ACTION_SET_TEXT``).

        Raises:
            RemoteError: ``no_focused_field`` when no editable field has input
                focus, ``set_text_failed`` when the field refused.
        """
        return TextTarget.model_validate(
            (await self.channel.call("set_text", {"text": text})).result
        )

    async def commit_text(self, text: str) -> TextTarget:
        """Insert *text* at the cursor through the service's own input method (API 33).

        The current keyboard stays the current keyboard. Only the field's
        package is known.

        Raises:
            RemoteError: ``no_focused_field`` when no field started input with
                the service's input method, ``unsupported`` before API 33.
        """
        return TextTarget.model_validate(
            (await self.channel.call("commit_text", {"text": text})).result
        )

    async def events(self, *, max_buffered: int = 100_000) -> ServiceEvents:
        """Start the event stream; once per channel (:meth:`Channel.subscribe`)."""
        return ServiceEvents(await self.channel.subscribe(max_buffered=max_buffered))

    async def close(self) -> None:
        await self.channel.close()


class HelperClient:
    """The shell helper, over an open channel."""

    producer: Literal["shell_helper"] = "shell_helper"

    def __init__(self, channel: Channel) -> None:
        self.channel = channel

    @property
    def hello(self) -> Hello:
        return self.channel.hello

    async def ping(self) -> Ping:
        return Ping.model_validate((await self.channel.call("ping")).result)

    async def screenshot(self) -> Screenshot:
        """The screen, straight from SurfaceFlinger (API 31-33), with no limit on how often."""
        return _screenshot(await self.channel.call("screenshot"), self.producer)

    async def key(self, keycode: str) -> None:
        """Press and release one key, named as Android names it (``KEYCODE_ENTER``)."""
        await self.channel.call("key", {"code": keycode})

    async def gesture(self, stroke: Stroke) -> int:
        """Inject one finger's timed stroke, each event at its planned time; returns, once it lifted, how long it took in ms."""
        return _elapsed(await self.channel.call("gesture", {"strokes": _strokes([stroke])}))

    async def tap(self, x: float, y: float, *, duration_ms: int = TAP_MS) -> int:
        return await self.gesture(Stroke.hold((x, y), duration_ms))

    async def swipe(self, start: Point, end: Point, *, duration_ms: int) -> int:
        return await self.gesture(Stroke.line(start, end, duration_ms))

    async def enable_service(self) -> SettingChange:
        """Append the service to ``enabled_accessibility_services``, other services kept.

        Whether the system bound it shows only in a ping: Android 14 and later
        drop a service they refuse from the setting without an error.
        """
        return SettingChange.model_validate((await self.channel.call("enable_service")).result)

    async def disable_service(self) -> SettingChange:
        """Take the service out of ``enabled_accessibility_services``, other services kept.

        The system unbinds it; its package is not stopped.
        """
        return SettingChange.model_validate((await self.channel.call("disable_service")).result)

    async def close(self) -> None:
        await self.channel.close()


def _strokes(strokes: Sequence[Stroke]) -> list[JsonValue]:
    return [{"points": [[x, y, t] for x, y, t in stroke.points]} for stroke in strokes]


def _elapsed(reply: Reply) -> int:
    return _GestureResult.model_validate(reply.result).elapsed_ms


class _GestureResult(_Result):
    elapsed_ms: int


def _screenshot(
    reply: Reply, producer: Literal["accessibility_service", "shell_helper"]
) -> Screenshot:
    result: JsonObject = reply.result
    timing = _ScreenshotResult.model_validate(result)
    return Screenshot(
        png=reply.payload,
        width=timing.width,
        height=timing.height,
        producer=producer,
        capture_ms=timing.capture_ms,
        encode_ms=timing.encode_ms,
        waited_ms=timing.waited_ms,
    )


class _ScreenshotResult(_Result):
    width: int
    height: int
    format: Literal["png"]
    capture_ms: int
    encode_ms: int
    waited_ms: int = 0
