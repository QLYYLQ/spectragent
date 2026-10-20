"""HumanMouse — the cursor middle layer.

A plain controller (NOT an agent-cdp watchdog) that owns the cursor position
and a trajectory strategy, and performs every mouse primitive — move / click /
wheel / drag — by driving the agent-cdp CDP client directly. ``ActionWatchdog``
holds one ``HumanMouse`` per tab and calls it directly.

Why a direct-call controller rather than an event-driven watchdog: agent-cdp's
scope event loop is a single-consumer FIFO that runs each handler to completion
before dequeuing the next event, so a watchdog handler that emits-and-awaits a
sub-event would deadlock until ``event_timeout``. A controller invoked by direct
``await`` sidesteps the event loop entirely while still being the single
chokepoint for all cursor movement.

The strategy is the only humanization switch. ``instant`` (also the fallback
when no strategy is given) is one waypoint at the destination and reports
``humanized == False``, so ``click`` emits exactly ``mouseMoved → mousePressed
→ mouseReleased`` at the target, ``wheel`` emits a single ``mouseWheel`` and the
cursor cold-starts at ``(0, 0)``.
Any other strategy reports ``humanized == True``, which also switches on the
behaviour around the path: centre cold start and eased, segmented wheel (here)
and the cursor approach before type/select (``ActionWatchdog``). There used to
be a separate ``humanize`` flag that forced ``instant`` when off, silently
discarding a configured strategy; the two could disagree, so it is gone.

No pre-click pause is added here at all: a strategy that models the hover
before a click — ``learned``, with the model's own pause or a sampled human
dwell — expresses it as repeated waypoints, which ``move_to`` sleeps through
without dispatching. (There was a ``dwell_ms`` constructor knob for a fixed
extra pause. Nothing in the package ever set it, it was unreachable from YAML,
and it would have double-counted the pause a strategy already models, so it is
gone rather than kept for a caller that does not exist.)

The *hold* is not optional and is not a knob here either: the button goes down
and comes back up ``trajectory.click_hold_ms()`` later, which is ``0.0`` for
``instant`` and a fresh human draw for every other strategy. It is the
strategy's to decide because it is humanization, not transport — but it is not
a waypoint (nothing moves while the button is down), so it cannot live on the
path. Before this existed the press and the release were two back-to-back CDP
sends, 1.4-3.4 ms apart measured against a real page, where a human holds for
tens to hundreds of ms.

The strategy also decides *where inside a target box* a click lands
(``trajectory.aim_offset_px``), but this layer never applies that offset:
``move_to`` is handed one exact point and its contract is to end exactly on
it. ``ActionWatchdog`` — the one place a measured box becomes a point — asks
for the offset and applies it before calling in here.

Everything paced here — a trajectory's waypoints, an eased scroll's steps — is
played against **absolute deadlines** by :func:`play_on_deadline`, not by
sleeping each interval in turn. Sleeping the interval and *then* awaiting the
CDP dispatch adds the round trip to every tick; measured against headless
Chrome that is ~17 ms per ``mouseMoved``, which replayed a 50 Hz learned path
at 33 Hz (1.53x its own duration) and a windmouse path, whose ease-out tail
asks for a waypoint every few ms, at 5.06x — and ~12 ms per ``mouseWheel``,
which stretched a 12 ms scroll step to 28 ms (2.5x for a 20-step scroll). The
timing is the whole point of the layer — the learned models were selected for
matching human speed at the rate they were recorded, and a scroll's step
rhythm is exactly what an anti-bot system reads — so wall time has to follow
the schedule, not the transport.

The strategy arrives here as a ready-made instance, chosen and constructed by
``ConfigComposer`` from the capability's ``mouse_trajectory`` config block. It
used to arrive as a registry key plus an untyped kwargs dict, so a typo or an
unsupported knob only surfaced as a ``ValueError`` here, at first tab setup,
minutes into a run — and the kwargs dict was not reachable from YAML at all.
Both failure modes are now Pydantic validation errors before anything launches.
"""

from __future__ import annotations

import logging
from asyncio import CancelledError, create_task, shield, wait_for
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent_cdp import CDPCommandProtocol

from spectragent.core.capabilities.browser.input._schedule import (
    maybe_await,
    play_on_deadline,
)
from spectragent.core.capabilities.browser.input.instant import InstantTrajectory
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryProtocol,
    TrajectoryPoint,
)

logger = logging.getLogger(__name__)

#: Release is cleanup, not trajectory pacing. It must finish before teardown
#: closes the input scope, even when the CDP transport never answers.
_DRAG_RELEASE_TIMEOUT_S = 2.0


@dataclass
class PointerPosition:
    """One tab's confirmed pointer position; None while movement is uncertain."""

    position: tuple[int, int] | None = None

    def snapshot(self) -> tuple[int, int] | None:
        return self.position


class HumanMouse:
    """Owns cursor state + a trajectory strategy; performs move/click/wheel/drag.

    One instance per tab. Single source of truth for the cursor position —
    ``ActionWatchdog`` no longer tracks ``_mouse_x/_mouse_y`` itself.
    """

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        session_id_getter: Callable[[], str | None],
        trajectory: MouseTrajectoryProtocol | None = None,
        viewport: tuple[int, int] = (1280, 720),
        move_callback: Callable[[int, int], Any] | None = None,
        cold_start: str = "center",
        pointer_position: PointerPosition | None = None,
        wheel_steps: int = 8,
        wheel_step_delay_ms: float = 12.0,
    ) -> None:
        self._pointer_position = (
            pointer_position if pointer_position is not None else PointerPosition()
        )
        self._cdp = cdp
        self._sid = session_id_getter
        self._vw, self._vh = viewport
        self._move_cb = move_callback
        self._wheel_steps = max(1, wheel_steps)
        self._wheel_step_delay_ms = wheel_step_delay_ms

        # Nothing configured → the no-op strategy, exactly the legacy
        # instant jump. The strategy alone decides whether we humanize.
        self._traj: MouseTrajectoryProtocol = (
            trajectory if trajectory is not None else InstantTrajectory()
        )
        self._humanized = self._traj.humanized

        # Cursor origin. Humanized runs start from viewport centre (matching the
        # debug overlay's initial 50vw/50vh) so the first action isn't a long
        # top-left diagonal; otherwise (0, 0) preserves the legacy starting point.
        if self._humanized and cold_start == "center":
            self._x, self._y = self._vw // 2, self._vh // 2
        else:
            self._x, self._y = 0, 0

    # ── introspection ────────────────────────────────────────────────────
    @property
    def humanized(self) -> bool:
        return self._humanized

    @property
    def trajectory(self) -> MouseTrajectoryProtocol:
        """The active strategy, for the humanization decisions taken elsewhere.

        ``ActionWatchdog`` needs one of them — ``aim_offset_px``, applied
        where a measured box becomes a point, which is upstream of every
        method here. Reading it through the controller keeps the strategy
        single-instanced: there is one per tab and this is it.
        """
        return self._traj

    @property
    def position(self) -> tuple[int, int]:
        return self._x, self._y

    @property
    def observed_position(self) -> tuple[int, int] | None:
        """Last successfully acknowledged mouseMoved; None before real movement.

        Wheel coordinates select the event's target and do not move the pointer.
        In-flight, failed or cancelled moves make the current position unknown:
        a lost acknowledgement does not prove Chrome did not move.
        """
        return self._pointer_position.snapshot()

    # ── helpers ───────────────────────────────────────────────────────────
    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        return (max(0, min(x, self._vw - 1)), max(0, min(y, self._vh - 1)))

    @staticmethod
    def _button_mask(button: str) -> int:
        masks = {"none": 0, "left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}
        if button not in masks:
            raise ValueError(f"Unknown mouse button {button!r}")
        return masks[button]

    async def _dispatch_move(self, x: int, y: int, *, button: str = "none") -> None:
        # Chrome may move before its acknowledgement is lost. Never present
        # the preceding point as current while delivery is uncertain.
        self._pointer_position.position = None
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": x,
                "y": y,
                "button": button,
                "buttons": self._button_mask(button),
            },
            session_id=self._sid(),
        )
        self._x, self._y = x, y
        self._pointer_position.position = (x, y)
        if self._move_cb is not None:
            try:
                await maybe_await(self._move_cb(x, y))
            except Exception as exc:  # overlay is best-effort, never blocks
                logger.debug("mouse move_callback failed at (%d,%d): %s", x, y, exc)

    # ── primitives ────────────────────────────────────────────────────────
    async def move_to(
        self,
        x: float,
        y: float,
        *,
        target_w: float = 0.0,
        target_h: float = 0.0,
        button: str = "none",
    ) -> None:
        """Move the cursor to (x, y) along the active trajectory strategy.

        ``button`` is the button held during this movement, local to a drag.
        Both CDP fields (the name and its bitmask) are derived from it: Chrome
        needs both to preserve native dragging and pointer capture.

        The path is the schedule handed to :func:`play_on_deadline`: waypoint
        ``i`` is due at ``start + sum(dt_ms[:i + 1])``, so the CDP round trip
        is spent inside the tick it belongs to instead of being added to it.
        Sleeping ``dt_ms`` and then awaiting the dispatch stretched every path
        by the transport's latency (~17 ms per event against headless Chrome),
        which is what made a 50 Hz learned model play back at 33 Hz.

        When the transport cannot keep up — a dispatch slower than the tick,
        which is the normal case for the sub-millisecond waypoints at the end
        of a Fitts ease-out — an intermediate waypoint whose whole slot has
        already elapsed is **dropped**: it merges into the next waypoint
        dispatched, exactly as a display drops a frame. Positions then stay on
        their own timeline (the total duration and the speed profile stay
        honest, the path is just sampled more coarsely) rather than the path
        being replayed in slow motion. The final waypoint is never dropped, so
        the endpoint stays exact, and a repeated waypoint still waits out its
        time without dispatching — a strategy's pre-click dwell survives.
        """
        ex, ey = round(x), round(y)
        path = self._traj.generate(self._x, self._y, ex, ey, target_w=target_w, target_h=target_h)
        if not path:
            path = [TrajectoryPoint(ex, ey, 0.0)]

        # A single-point path is the instant jump: always dispatched, even
        # onto the current position (legacy behaviour).
        jump = len(path) == 1
        last = len(path) - 1
        async for i in play_on_deadline([pt.dt_ms for pt in path]):
            pt = path[i]
            # The endpoint is dispatched raw so the click lands exactly on
            # target (and off-viewport targets stay exact); only intermediate
            # overshoot points are clamped into the viewport.
            px, py = (pt.x, pt.y) if i == last else self._clamp(pt.x, pt.y)
            # A repeated point is how a strategy says "hold still": its time is
            # spent by the schedule, but it dispatches nothing.
            if not jump and (px, py) == (self._x, self._y):
                continue
            await self._dispatch_move(px, py, button=button)

    async def click(
        self,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        *,
        target_w: float = 0.0,
        target_h: float = 0.0,
        before_press: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> tuple[int, int]:
        """Move to (x, y), press, hold, release. Returns the final cursor pos.

        The button's two events are two slots of one schedule — the press due
        immediately on arrival, the release ``trajectory.click_hold_ms()``
        after it — played by :func:`play_on_deadline` like everything else in
        this layer. Two reasons it is the schedule and not a sleep: the
        press's own CDP round trip is then spent *inside* the hold instead of
        being added to it (the same transport cost that made a 50 Hz path play
        at 33 Hz), and a second timing path here would drift from the
        scheduler the moment either was tuned.

        With ``instant`` both slots are due immediately, so nothing sleeps and
        the click uses the same three primitives at the target. Slots are sent in
        order and a slot's events ride along with the next one dispatched, so
        a transport slower than the hold shortens the hold — it can never
        release a button that was never pressed.

        ``before_press`` is awaited once the cursor has arrived, with the
        point the press is about to be dispatched at: the caller's look at
        what is under the pointer right before it goes down
        (``ActionWatchdog`` measures the click's effect there). Its time is
        spent before the hold's schedule starts, so it never shortens the
        hold.
        """
        await self.move_to(x, y, target_w=target_w, target_h=target_h)
        if before_press is not None:
            await before_press(self._x, self._y)
        sid = self._sid()
        common = {
            "x": self._x,
            "y": self._y,
            "button": button,
            "clickCount": click_count,
        }
        events = [
            {"type": "mousePressed", **common},
            {"type": "mouseReleased", **common},
        ]
        schedule = [0.0, self._traj.click_hold_ms()]
        unsent = 0
        async for slot in play_on_deadline(schedule):
            for params in events[unsent : slot + 1]:
                await self._cdp.send("Input.dispatchMouseEvent", params, session_id=sid)
            unsent = slot + 1
        return self._x, self._y

    async def wheel(self, x: int, y: int, dx: int, dy: int) -> None:
        """Scroll at (x, y) by (dx, dy).

        Off: a single ``mouseWheel`` at (x, y) — legacy behavior. On: move the
        cursor to (x, y) first, then split the delta into ease-out increments
        (humans scroll in segments, decelerating into a stop) played on the
        same absolute-deadline schedule as :meth:`move_to`: the first step is
        due immediately and each later one ``wheel_step_delay_ms`` after the
        one before, so the scroll takes the ``(steps - 1)`` delays it asks for.
        The rhythm of those steps is the behavioural signal the segmented
        scroll exists to produce, so it must not be the transport's rhythm —
        sleeping the delay and then awaiting the dispatch stretched a 12 ms
        step to ~28 ms against headless Chrome, a 20-step scroll to 2.5x its
        requested duration.

        A step whose slot is gone before it could be sent is merged into the
        next step dispatched rather than chased, so a slow transport coarsens
        the scroll instead of slowing it, and the increments still sum exactly
        to ``(dx, dy)`` — the last step is never dropped and carries the
        rounding remainder.
        """
        sid = self._sid()
        if not self._humanized:
            await self._cdp.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy},
                session_id=sid,
            )
            self._x, self._y = x, y
            return

        await self.move_to(x, y)
        steps = self._wheel_steps
        # The ease-out split, resolved up front so that merging two steps is
        # adding two increments rather than re-deriving the curve.
        increments: list[tuple[int, int]] = []
        prev_progress = 0.0
        sent_dx = sent_dy = 0
        for i in range(1, steps + 1):
            progress = 1.0 - (1.0 - i / steps) ** 2  # ease-out
            frac = progress - prev_progress
            prev_progress = progress
            step_dx = round(dx * frac)
            step_dy = round(dy * frac)
            sent_dx += step_dx
            sent_dy += step_dy
            if i == steps:  # absorb rounding remainder so the total is exact
                step_dx += dx - sent_dx
                step_dy += dy - sent_dy
            increments.append((step_dx, step_dy))

        schedule = [0.0] + [self._wheel_step_delay_ms] * (steps - 1)
        unsent = 0
        async for i in play_on_deadline(schedule):
            merged = increments[unsent : i + 1]
            unsent = i + 1
            await self._cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": self._x,
                    "y": self._y,
                    "deltaX": sum(d for d, _ in merged),
                    "deltaY": sum(d for _, d in merged),
                },
                session_id=sid,
            )

    async def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        button: str = "left",
        *,
        target_w: float = 0.0,
        target_h: float = 0.0,
        before_press: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        """Press at start, move along the trajectory, release at end.

        Driven by ``browser.drag`` via ``ActionWatchdog._on_drag``. The
        travel between press and release goes through :meth:`move_to`, so
        a humanised run drags along the same path it would have moved
        along. ``before_press`` is awaited at the start point before the
        button goes down, as in :meth:`click`.
        """
        buttons = self._button_mask(button)
        if not buttons:
            raise ValueError(f"Cannot drag with mouse button {button!r}")
        sid = self._sid()
        await self.move_to(start_x, start_y)
        if before_press is not None:
            await before_press(self._x, self._y)
        try:
            # Even a failed acknowledgement may have delivered the press. Always
            # attempt the release once dispatching the press has begun.
            await self._cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": self._x,
                    "y": self._y,
                    "button": button,
                    "buttons": buttons,
                    "clickCount": 1,
                },
                session_id=sid,
            )
            await self.move_to(end_x, end_y, target_w=target_w, target_h=target_h, button=button)
        except BaseException:
            try:
                await self._release_drag(sid, button)
            except CancelledError:
                raise  # An external cancellation during cleanup outranks an input error.
            except Exception:
                logger.warning("Could not release interrupted drag", exc_info=True)
            raise
        else:
            await self._release_drag(sid, button)

    async def _release_drag(self, sid: str | None, button: str) -> None:
        """Finish this gesture before propagating a cancellation of its release."""
        release = create_task(
            wait_for(
                self._cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "x": self._x,
                        "y": self._y,
                        "button": button,
                        "buttons": 0,
                        "clickCount": 1,
                    },
                    session_id=sid,
                ),
                timeout=_DRAG_RELEASE_TIMEOUT_S,
            )
        )
        cancellation: CancelledError | None = None
        while not release.done():
            try:
                await shield(release)
            except CancelledError as exc:
                cancellation = cancellation or exc
            except Exception:
                break  # Retrieve the failure below, preserving an earlier cancellation.
        if cancellation is not None:
            try:
                release.result()
            except BaseException:
                logger.warning("Could not release cancelled drag", exc_info=True)
            raise cancellation
        release.result()
