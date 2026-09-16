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
cursor cold-starts at ``(0, 0)`` — byte-for-byte the pre-humanization behavior.
Any other strategy reports ``humanized == True``, which also switches on the
behaviour around the path: centre cold start and eased, segmented wheel (here)
and the cursor approach before type/select (``ActionWatchdog``). There used to
be a separate ``humanize`` flag that forced ``instant`` when off, silently
discarding a configured strategy; the two could disagree, so it is gone.

No pre-click pause is added by default (``dwell_ms == 0``, and nothing in the
package sets it): a strategy that models the hover before a click —
``learned``, with the model's own pause or a sampled human dwell — expresses it
as repeated waypoints, which ``move_to`` sleeps through without dispatching.
Setting ``dwell_ms`` adds a fixed pause on top.

Playback is scheduled against **absolute deadlines**, not by sleeping each
waypoint's ``dt_ms`` in turn — see :meth:`HumanMouse.move_to`. Sleeping the
interval and *then* awaiting the CDP dispatch adds the round trip to every
tick; measured against headless Chrome that is ~17 ms per ``mouseMoved``,
which replayed a 50 Hz learned path at 33 Hz (1.53x its own duration) and a
windmouse path, whose ease-out tail asks for a waypoint every few ms, at 5.06x.
A trajectory's timing is the whole point of the layer — the learned models were
selected for matching human speed at the rate they were recorded — so wall time
has to follow the path, not the transport.

The strategy arrives here as a ready-made instance, chosen and constructed by
``ConfigComposer`` from the capability's ``mouse_trajectory`` config block. It
used to arrive as a registry key plus an untyped kwargs dict, so a typo or an
unsupported knob only surfaced as a ``ValueError`` here, at first tab setup,
minutes into a run — and the kwargs dict was not reachable from YAML at all.
Both failure modes are now Pydantic validation errors before anything launches.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from time import monotonic
from typing import Any

from agent_cdp import CDPCommandProtocol

from spectragent.core.capabilities.browser.input.instant import InstantTrajectory
from spectragent.core.capabilities.browser.input.trajectory import (
    MouseTrajectoryProtocol,
    TrajectoryPoint,
)

logger = logging.getLogger(__name__)


async def _maybe_await(value: Any) -> None:
    """Await ``value`` if it is awaitable, else ignore (sync callback)."""
    if inspect.isawaitable(value):
        await value


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
        dwell_ms: float = 0.0,
        cold_start: str = "center",
        wheel_steps: int = 8,
        wheel_step_delay_ms: float = 12.0,
    ) -> None:
        self._cdp = cdp
        self._sid = session_id_getter
        self._vw, self._vh = viewport
        self._move_cb = move_callback
        self._dwell_ms = dwell_ms
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
    def position(self) -> tuple[int, int]:
        return self._x, self._y

    # ── helpers ───────────────────────────────────────────────────────────
    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        return (max(0, min(x, self._vw - 1)), max(0, min(y, self._vh - 1)))

    async def _dispatch_move(self, x: int, y: int) -> None:
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y},
            session_id=self._sid(),
        )
        self._x, self._y = x, y
        if self._move_cb is not None:
            try:
                await _maybe_await(self._move_cb(x, y))
            except Exception as exc:  # overlay is best-effort, never blocks
                logger.debug("mouse move_callback failed at (%d,%d): %s", x, y, exc)

    # ── primitives ────────────────────────────────────────────────────────
    async def move_to(
        self, x: float, y: float, *, target_w: float = 0.0, target_h: float = 0.0
    ) -> None:
        """Move the cursor to (x, y) along the active trajectory strategy.

        The path is played on a deadline: waypoint ``i`` is due at
        ``start + sum(dt_ms[:i + 1])`` and each step sleeps until *that*
        instant, so the CDP round trip is spent inside the tick it belongs to
        instead of being added to it. Sleeping ``dt_ms`` and then awaiting the
        dispatch stretched every path by the transport's latency (~17 ms per
        event against headless Chrome), which is what made a 50 Hz learned
        model play back at 33 Hz.

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
        # ``monotonic`` is the clock ``asyncio.sleep`` itself schedules against.
        deadline = monotonic()
        for i, pt in enumerate(path):
            deadline += pt.dt_ms / 1000.0
            is_last = i == last
            # The endpoint is dispatched raw so the click lands exactly on
            # target (and off-viewport targets stay exact); only intermediate
            # overshoot points are clamped into the viewport.
            px, py = (pt.x, pt.y) if is_last else self._clamp(pt.x, pt.y)
            now = monotonic()
            if now < deadline:
                await asyncio.sleep(deadline - now)
            elif not is_last and now >= deadline + path[i + 1].dt_ms / 1000.0:
                continue  # behind schedule, this waypoint's slot is gone: drop it
            # A repeated point is how a strategy says "hold still": its time is
            # spent above, but it dispatches nothing.
            if not jump and (px, py) == (self._x, self._y):
                continue
            await self._dispatch_move(px, py)

    async def click(
        self,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
        *,
        target_w: float = 0.0,
        target_h: float = 0.0,
    ) -> tuple[int, int]:
        """Move to (x, y) then press + release. Returns the final cursor pos."""
        await self.move_to(x, y, target_w=target_w, target_h=target_h)
        if self._dwell_ms > 0:
            await asyncio.sleep(self._dwell_ms / 1000.0)
        sid = self._sid()
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": self._x,
                "y": self._y,
                "button": button,
                "clickCount": click_count,
            },
            session_id=sid,
        )
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": self._x,
                "y": self._y,
                "button": button,
                "clickCount": click_count,
            },
            session_id=sid,
        )
        return self._x, self._y

    async def wheel(self, x: int, y: int, dx: int, dy: int) -> None:
        """Scroll at (x, y) by (dx, dy).

        Off: a single ``mouseWheel`` at (x, y) — legacy behavior. On: move the
        cursor to (x, y) first, then split the delta into ease-out increments
        (humans scroll in segments, decelerating into a stop).
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
            await self._cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": self._x,
                    "y": self._y,
                    "deltaX": step_dx,
                    "deltaY": step_dy,
                },
                session_id=sid,
            )
            if self._wheel_step_delay_ms > 0 and i != steps:
                await asyncio.sleep(self._wheel_step_delay_ms / 1000.0)

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
    ) -> None:
        """Press at start, move along the trajectory, release at end.

        Driven by ``browser.drag`` via ``ActionWatchdog._on_drag``. The
        travel between press and release goes through :meth:`move_to`, so
        a humanised run drags along the same path it would have moved
        along.
        """
        sid = self._sid()
        await self.move_to(start_x, start_y)
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": self._x,
                "y": self._y,
                "button": button,
                "clickCount": 1,
            },
            session_id=sid,
        )
        await self.move_to(end_x, end_y, target_w=target_w, target_h=target_h)
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": self._x,
                "y": self._y,
                "button": button,
                "clickCount": 1,
            },
            session_id=sid,
        )
