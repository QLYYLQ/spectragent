"""When an Android screen counts as still, and the wait for it before a capture.

The capability defines "still" (``docs/architecture_design.md`` §4.4, §11.2).
**The screen is still once, for the quiet period, our accessibility service has
received no event, the window manager has reported the same focused app and
window, and that app has rendered at most one new frame.** Each poll asks three
things:

- the service's events: a ping on the channel that carries them first, so that
  every event the service received before the ping has arrived
  (``device/channel.py``), then the time of the newest event against the
  ping's uptime, both on the device's clock. Events that came in before the
  wait (while the model was thinking) count by their own time, not by when the
  host read them;
- the window manager's focus (``dumpsys window``): which app and which window
  have it. No window with focus is a transition in progress;
- how many frames the focused app has rendered (``dumpsys gfxinfo <package>``).
  An app that reports no count, or none rendered, has not drawn yet. One new
  frame within the quiet period is allowed: a focused text field's cursor
  redraws about every 500 ms and sends no event, and a poll took 200-300 ms
  under load, so "no new frame for 300 ms" fell between two blinks too seldom
  to end before the limit (measured on Settings' search screen).

Each source covers what the others miss, measured on the API 33 emulator under
load (2026-11-02, ``tmp/settle_probe`` in the worktree, not tracked):

- a cold start sends no event while its process starts: after ``am start`` of
  Settings, 1.6 s passed without an event, while the window manager reported
  no focused window, and then Settings with no frame rendered yet;
- the accessibility window list lags the window manager by 1-2 s after an app
  starts, and the service's ``TYPE_WINDOWS_CHANGED`` events come as it catches
  up: waiting for them makes a later tree capture match the screenshot;
- windows the focused app does not draw itself (the keyboard, the notification
  shade, another app's dialog) are seen only through their events;
- what none of them sees: a pause in drawing with no event. Settings, started
  cold, drew a first burst, paused about 0.4 s, and drew more for another
  1.3 s; a capture in the pause counts the screen as still.

The wait ends ``settled`` after a quiet period with no change; ``capped`` at
its limit, ``pending`` naming what changed within the last quiet period
(``events``, ``focus``, ``frames``); or ``unknown`` at once when a poll fails
or the event stream has ended, the reason in ``detail``. It never fails a
capture. ``loaded`` is always ``None``: none of the three says whether an app
has finished its initial load. The capability records the outcome on every
fragment of the capture (``observations.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from spectragent.schemas.observation.capture_state import SettleOutcome, SettleState

#: What had not settled when the wait was capped, in the order they are listed.
PENDING_EVENTS = "events"
PENDING_FOCUS = "focus"
PENDING_FRAMES = "frames"
_POLL_S = 0.1


@dataclass(frozen=True)
class ScreenPoll:
    """One look at the device.

    Attributes:
        since_event_ms: How long before this poll, on the device's clock, the
            service received its newest event so far; ``None`` when it has
            received none since it subscribed.
        package: The focused app, as the window manager reports it.
        window: The window with input focus; ``None`` when none has it.
        frames: How many frames the focused app has rendered; ``None`` when it
            reports no count.
    """

    since_event_ms: int | None
    package: str | None
    window: str | None
    frames: int | None


class EventsEnded(Exception):
    """The service's event stream ended, so a wait cannot see events; the message says why."""


async def wait_until_still(
    poll: Callable[[], Awaitable[ScreenPoll]], *, quiet_s: float, max_wait_s: float
) -> SettleState:
    """Poll until nothing has changed for *quiet_s* (module docstring), at most *max_wait_s*."""
    clock = asyncio.get_running_loop()
    started = clock.time()

    def state(
        outcome: SettleOutcome, *, pending: tuple[str, ...] = (), detail: str | None = None
    ) -> SettleState:
        waited_ms = round((clock.time() - started) * 1000)
        return SettleState(
            outcome=outcome, waited_ms=waited_ms, loaded=None, pending=pending, detail=detail
        )

    quiet_ms = quiet_s * 1000
    focus: tuple[str | None, str | None] | None = None
    focus_changed = started
    #: The focused app's frame counts since another app had focus: (when, how many).
    drawn: list[tuple[float, int]] = []
    while True:
        try:
            seen = await poll()
        except Exception as exc:
            return state("unknown", detail=f"{type(exc).__name__}: {exc}")
        now = clock.time()
        if focus is not None and seen.package != focus[0]:
            drawn.clear()  # another app's frames
        if (seen.package, seen.window) != focus or seen.window is None:
            focus, focus_changed = (seen.package, seen.window), now
        if seen.frames:
            drawn.append((now, seen.frames))
        else:  # not drawn yet
            drawn.clear()
        changing = (
            (PENDING_EVENTS, seen.since_event_ms is not None and seen.since_event_ms < quiet_ms),
            (PENDING_FOCUS, now - focus_changed < quiet_s),
            (PENDING_FRAMES, not _drew_at_most_one_frame(drawn, now - quiet_s)),
        )
        pending = tuple(label for label, still_changing in changing if still_changing)
        if not pending:
            return state("settled")
        if now - started >= max_wait_s:
            return state("capped", pending=pending)
        await asyncio.sleep(min(_POLL_S, quiet_s))


def _drew_at_most_one_frame(drawn: list[tuple[float, int]], since: float) -> bool:
    """Whether the counts in *drawn* grew by at most one frame since *since*.

    Measured against the newest count taken at or before *since*; older ones
    are dropped from *drawn*. ``False`` when no count is that old yet.
    """
    old = [index for index, (at, _) in enumerate(drawn) if at <= since]
    if not old:
        return False
    del drawn[: old[-1]]
    return 0 <= drawn[-1][1] - drawn[0][1] <= 1
