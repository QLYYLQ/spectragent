"""The one clock the input layer plays everything against.

A trajectory's waypoints, an eased scroll's steps and a typed string's
keystrokes are all the same problem: a list of intervals that must be spent
*as wall time*, over a transport that charges ~17 ms per CDP round trip.
Sleeping the interval and then awaiting the dispatch adds the transport's
cost to every tick — measured against headless Chrome that replayed a 50 Hz
trajectory at 33 Hz and stretched a 12 ms scroll step to 28 ms — and the
timing is the entire point of this layer: it is the behavioural signal an
anti-bot system reads.

So there is exactly one scheduler, here, and every consumer in
``input/`` drives it. A second one would drift from this one the moment
either was tuned.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Sequence
from time import monotonic
from typing import Any


async def maybe_await(value: Any) -> None:
    """Await ``value`` if it is awaitable, else ignore (sync callback)."""
    if inspect.isawaitable(value):
        await value


async def play_on_deadline(dt_ms: Sequence[float]) -> AsyncIterator[int]:
    """Yield the slots of a schedule, each at its own absolute due time.

    ``dt_ms[i]`` is how long slot *i* waits after slot ``i - 1`` came due, so
    slot *i* is due at ``start + sum(dt_ms[: i + 1])`` — an instant, fixed
    before anything is sent. Each iteration sleeps until *that* instant, so
    whatever the caller awaits between yields is spent inside the slot it
    belongs to instead of being added to it. Sleeping the interval and then
    awaiting the work adds the work's own cost to every slot, which is how a
    CDP round trip (~17 ms per ``mouseMoved``, ~12 ms per ``mouseWheel``
    against headless Chrome) came to dominate both a trajectory's tick rate
    and a scroll's step rhythm.

    When the work is slower than its slot, a slot whose successor is *also*
    already due is **skipped** rather than chased — a dropped frame, not a
    stretched one, so the schedule's total duration and its shape stay honest
    and only the sampling coarsens. What a skipped slot means belongs to the
    caller: :meth:`HumanMouse.move_to` simply never sends that position,
    while :meth:`HumanMouse.wheel` folds its delta into the next step it does
    send and :meth:`HumanKeyboard.type_text` flushes the events it carried
    late (a key event can be late; it can never be dropped, or the key stays
    down). A slot with no time of its own can never make its predecessor
    late, so a schedule of zero-length slots (``wheel_step_delay_ms == 0``)
    plays in full, and the last slot — which has no successor — is never
    skipped.

    A yielded slot the caller decides not to act on has still spent its time:
    that is how a repeated waypoint holds the cursor still.
    """
    last = len(dt_ms) - 1
    # ``monotonic`` is the clock ``asyncio.sleep`` itself schedules against.
    deadline = monotonic()
    for i, dt in enumerate(dt_ms):
        deadline += dt / 1000.0
        successor = dt_ms[i + 1] / 1000.0 if i != last else 0.0
        now = monotonic()
        if now < deadline:
            await asyncio.sleep(deadline - now)
        elif successor > 0 and now >= deadline + successor:
            continue  # behind schedule, this slot's whole span is gone: drop it
        yield i
