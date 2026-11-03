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
  An app that reports no count, or none rendered, has not drawn yet; a count
  that could not be read is no count either, and the wait goes on. One new
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

Two more rules hold the wait (user ruling 2026-11-03):

- **Not before the minimum after an action.** The screen counts as still no
  sooner than a set time after the last action ended (*not_before*; the
  capability's ``settle_min_after_action_ms``, 0 unless a run sets it).
  AndroidWorld's M3A sleeps 2 s after every action before it reads the screen
  again, and a run that runs M3A as AndroidWorld does sets 2000 ms.
- **The app's content is in the UI tree.** A capture whose tree has no app
  window with content (:func:`shows_an_app`) was taken before the screen
  finished changing: the capability waits again within the same limit
  (*started*) and captures again. The tree comes with the capture, so the
  rule holds only where a capture takes the tree the agent reads. In
  AndroidWorld's seed-42 sweep (2026-11-03) 25 of 1634 captures, in 21 of
  115 runs, had no app window or only app windows with nothing in them, each
  right after a tap, a key or an app start; the agent saw the status bar
  alone, or the window's empty root, and acted on it.

The wait ends ``settled`` after a quiet period with no change; ``capped`` at
its limit, ``pending`` naming what changed within the last quiet period or
had not happened yet (``after_action``, ``events``, ``focus``, ``frames``,
and ``app_window`` for a capture that showed no app); or ``unknown`` at once
when a poll fails or the event stream has ended, the reason in ``detail``. It
never fails a capture. ``loaded`` is always ``None``: none of the sources says
whether an app has finished its initial load. The capability records the
outcome on every fragment of the capture (``observations.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from spectragent.core.capabilities.android.device.forest import (
    AndroidAccessibilityForest,
    AndroidAccessibilityNodeInfo,
)
from spectragent.schemas.observation.capture_state import SettleOutcome, SettleState

#: What had not settled when the wait was capped, in the order they are listed.
PENDING_AFTER_ACTION = "after_action"
PENDING_EVENTS = "events"
PENDING_FOCUS = "focus"
PENDING_FRAMES = "frames"
#: The capture after the wait showed no app window with content (:func:`shows_an_app`).
PENDING_APP_WINDOW = "app_window"
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
            reports no count, or the count could not be read.
    """

    since_event_ms: int | None
    package: str | None
    window: str | None
    frames: int | None


class EventsEnded(Exception):
    """The service's event stream ended, so a wait cannot see events; the message says why."""


async def wait_until_still(
    poll: Callable[[], Awaitable[ScreenPoll]],
    *,
    quiet_s: float,
    max_wait_s: float,
    started: float | None = None,
    not_before: float | None = None,
) -> SettleState:
    """Poll until nothing has changed for *quiet_s* (module docstring), at most *max_wait_s*.

    Times are on the event loop's clock. *started* is when the wait began, now
    unless given: a capture that showed no app goes on waiting within the
    limit of its first wait, which ``waited_ms`` counts from. *not_before* is
    the earliest the screen may count as still: the last action's end plus the
    minimum wait after it.
    """
    clock = asyncio.get_running_loop()
    begun = clock.time() if started is None else started

    def state(
        outcome: SettleOutcome, *, pending: tuple[str, ...] = (), detail: str | None = None
    ) -> SettleState:
        waited_ms = round((clock.time() - begun) * 1000)
        return SettleState(
            outcome=outcome, waited_ms=waited_ms, loaded=None, pending=pending, detail=detail
        )

    quiet_ms = quiet_s * 1000
    focus: tuple[str | None, str | None] | None = None
    focus_changed = begun
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
            (PENDING_AFTER_ACTION, not_before is not None and now < not_before),
            (PENDING_EVENTS, seen.since_event_ms is not None and seen.since_event_ms < quiet_ms),
            (PENDING_FOCUS, now - focus_changed < quiet_s),
            (PENDING_FRAMES, not _drew_at_most_one_frame(drawn, now - quiet_s)),
        )
        pending = tuple(label for label, still_changing in changing if still_changing)
        if not pending:
            return state("settled")
        if now - begun >= max_wait_s:
            return state("capped", pending=pending)
        await asyncio.sleep(min(_POLL_S, quiet_s))


def shows_an_app(forest: AndroidAccessibilityForest) -> bool:
    """Whether *forest* has an app window with content: a node in it that says something or can be acted on.

    A node says something with its text or its content description, and can be
    acted on when it is clickable, long-clickable, editable, scrollable or
    checkable. In the seed-42 sweep (module docstring) the app windows without
    content held their empty root alone, or layout containers and a splash
    image; stable screens with a single element (the brightness slider, a
    video playing full screen) say something.
    """
    return any(
        window.window_type == "TYPE_APPLICATION" and any(map(_has_content, window.tree.nodes))
        for window in forest.windows
    )


def without_an_app(settle: SettleState) -> SettleState:
    """The record of a wait whose capture showed no app (:func:`shows_an_app`) and that waits no more.

    A wait that ended settled or capped ends capped, naming ``app_window``
    among what had not settled; one that could not read the screen
    (``unknown``) stays as it is.
    """
    if settle.outcome == "unknown":
        return settle
    return settle.model_copy(
        update={"outcome": "capped", "pending": (*settle.pending, PENDING_APP_WINDOW)}
    )


def _has_content(node: AndroidAccessibilityNodeInfo) -> bool:
    return bool(
        node.text.strip()
        or node.content_description.strip()
        or node.is_clickable
        or node.is_long_clickable
        or node.is_editable
        or node.is_scrollable
        or node.is_checkable
    )


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
