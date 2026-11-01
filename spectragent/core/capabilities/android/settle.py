"""When an Android screen counts as still, and the wait for it before a capture.

The capability defines "still" (``docs/architecture_design.md`` §4.4, §11.2).
Without a UI tree or accessibility events, it reads what the window manager
and the renderer report: **the screen is still once the focused app has
rendered no new frame for the quiet period.** Each poll reads the focused app
(``dumpsys window``) and how many frames it has rendered so far
(``dumpsys gfxinfo <package>``); another app in front or another frame restarts
the quiet period. The wait ends:

- ``settled`` after a quiet period with no change;
- ``capped`` at the wait's limit, pending ``frames`` (the app was still drawing);
- ``unknown`` at once when a poll fails, nothing has focus, or the focused app
  reports no frame count; the reason goes in ``detail``.

It never fails a capture. ``loaded`` is always ``None``: these reads cannot
tell whether an app has finished its initial load.

What it does not see, measured on the emulator (API 33, 2026-11-01): a cold
Settings launch drew in bursts with a one-second pause between them, so a
quiet period shorter than such a pause ends the wait before the app has
finished (the agent can still wait); a focused text field's blinking cursor
draws a frame about every 500 ms, so a quiet period longer than that never
ends before the limit; and windows the focused app does not draw itself (the
keyboard, the notification shade) are not watched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from spectragent.schemas.observation.capture_state import SettleOutcome, SettleState

#: What a still screen has not done yet when the wait is capped: drawn its last frame.
PENDING_FRAMES = "frames"
_POLL_S = 0.1

#: One poll: the focused app's package and its rendered frames, either ``None`` when unknown.
Drawing = tuple[str | None, int | None]


async def wait_until_still(
    read: Callable[[], Awaitable[Drawing]], *, quiet_s: float, max_wait_s: float
) -> SettleState:
    """Poll *read* until the focused app has drawn nothing new for *quiet_s*, at most *max_wait_s*."""
    loop = asyncio.get_running_loop()
    started = loop.time()

    def state(
        outcome: SettleOutcome, *, pending: tuple[str, ...] = (), detail: str | None = None
    ) -> SettleState:
        waited_ms = round((loop.time() - started) * 1000)
        return SettleState(
            outcome=outcome, waited_ms=waited_ms, loaded=None, pending=pending, detail=detail
        )

    last: Drawing | None = None
    changed_at = started
    while True:
        try:
            drawing = await read()
        except Exception as exc:
            return state("unknown", detail=f"{type(exc).__name__}: {exc}")
        package, frames = drawing
        if package is None:
            return state("unknown", detail="no app has focus")
        if frames is None:
            return state("unknown", detail=f"{package} reports no rendered frames")
        now = loop.time()
        if drawing != last:
            last, changed_at = drawing, now
        elif now - changed_at >= quiet_s:
            return state("settled")
        if now - started >= max_wait_s:
            return state("capped", pending=(PENDING_FRAMES,))
        await asyncio.sleep(min(_POLL_S, quiet_s))
