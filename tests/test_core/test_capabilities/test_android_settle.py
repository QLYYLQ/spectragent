"""When the Android screen counts as still: no new frame from the focused app for the quiet period.

``spectragent/core/capabilities/android/settle.py`` defines it. The reads are
scripted; the quiet periods are short real waits, because the passing of time
is what is being tested.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from spectragent.core.capabilities.android.settle import Drawing, wait_until_still

SETTINGS = "com.android.settings"


def reads(answers: Iterable[Drawing]) -> Callable[[], Awaitable[Drawing]]:
    """Each call answers the next of *answers*, then the last one for ever."""
    remaining = list(answers)

    async def read() -> Drawing:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return read


async def test_the_wait_ends_settled_once_no_frame_is_drawn_for_the_quiet_period() -> None:
    drawing = reads([(SETTINGS, 1), (SETTINGS, 4), (SETTINGS, 8), (SETTINGS, 8)])

    state = await wait_until_still(drawing, quiet_s=0.05, max_wait_s=5)

    assert (state.outcome, state.pending, state.loaded) == ("settled", (), None)
    assert state.waited_ms >= 50


async def test_another_app_in_front_starts_the_quiet_period_again() -> None:
    drawing = reads([("com.google.android.apps.nexuslauncher", 9), (SETTINGS, 9), (SETTINGS, 9)])

    state = await wait_until_still(drawing, quiet_s=0.05, max_wait_s=5)

    assert state.outcome == "settled"


async def test_an_app_that_keeps_drawing_ends_the_wait_at_its_limit() -> None:
    frames = iter(range(1_000_000))

    async def always_drawing() -> Drawing:
        return SETTINGS, next(frames)

    state = await wait_until_still(always_drawing, quiet_s=0.05, max_wait_s=0.2)

    assert (state.outcome, state.pending) == ("capped", ("frames",))
    assert state.waited_ms >= 200


async def test_what_cannot_be_read_ends_the_wait_as_unknown_with_the_reason() -> None:
    async def broken() -> Drawing:
        raise ConnectionError("device offline")

    failed = await wait_until_still(broken, quiet_s=0.05, max_wait_s=5)
    nobody = await wait_until_still(reads([(None, None)]), quiet_s=0.05, max_wait_s=5)
    silent = await wait_until_still(reads([(SETTINGS, None)]), quiet_s=0.05, max_wait_s=5)

    assert (failed.outcome, failed.detail) == ("unknown", "ConnectionError: device offline")
    assert (nobody.outcome, nobody.detail) == ("unknown", "no app has focus")
    assert (silent.outcome, silent.detail) == ("unknown", f"{SETTINGS} reports no rendered frames")
