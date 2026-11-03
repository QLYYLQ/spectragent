"""When the Android screen counts as still: no event, the same focus, no new frame, for the quiet period.

``spectragent/core/capabilities/android/settle.py`` defines it. The polls are
scripted; the quiet periods are short real waits, because the passing of time
is what is being tested. Each source has a test that fails without it, from
what the emulator showed (``settle.py``'s docstring): a cold start that sends
no event while the window manager reports no focused window, then an app with
no frame drawn; the keyboard, seen only through its events. So do the two
rules after them: no still screen before the minimum after an action, and
which trees show no app, from the captures of AndroidWorld's seed-42 sweep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityForest
from spectragent.core.capabilities.android.settle import (
    EventsEnded,
    ScreenPoll,
    shows_an_app,
    wait_until_still,
    without_an_app,
)
from spectragent.schemas.observation.capture_state import SettleState

SETTINGS = "com.android.settings"
LAUNCHER = "com.google.android.apps.nexuslauncher"
WINDOW = "com.android.settings/com.android.settings.Settings"
#: An event long before the wait: the model was thinking.
LONG_AGO = 60_000


def still(frames: int = 40, since_event_ms: int | None = LONG_AGO) -> ScreenPoll:
    return ScreenPoll(since_event_ms, SETTINGS, WINDOW, frames)


def polls(answers: Iterable[ScreenPoll]) -> Callable[[], Awaitable[ScreenPoll]]:
    """Each call answers the next of *answers*, then the last one for ever."""
    remaining = list(answers)

    async def poll() -> ScreenPoll:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return poll


async def test_the_wait_ends_settled_once_nothing_changes_for_the_quiet_period() -> None:
    state = await wait_until_still(
        polls([still(1), still(4), still(8), still(8)]), quiet_s=0.05, max_wait_s=5
    )

    assert (state.outcome, state.pending, state.loaded) == ("settled", (), None)
    assert state.waited_ms >= 50


async def test_events_from_before_the_wait_do_not_hold_it() -> None:
    """They count by their own time on the device, not by when the host read them."""
    state = await wait_until_still(polls([still(since_event_ms=5_000)]), quiet_s=0.05, max_wait_s=5)

    assert state.outcome == "settled"


async def test_an_event_within_the_quiet_period_holds_the_wait() -> None:
    """The keyboard draws in a window of its own: only its events say it is still changing."""
    state = await wait_until_still(polls([still(since_event_ms=10)]), quiet_s=0.05, max_wait_s=0.2)

    assert (state.outcome, state.pending) == ("capped", ("events",))
    assert state.waited_ms >= 200


async def test_a_cold_start_is_not_still_before_its_app_has_drawn() -> None:
    """No event comes while the process starts (1.6 s measured): no window has focus, then no frame."""
    starting = [
        ScreenPoll(LONG_AGO, LAUNCHER, None, 29),  # the launcher lost focus, nothing has it
        ScreenPoll(LONG_AGO, LAUNCHER, None, 29),
        ScreenPoll(LONG_AGO, SETTINGS, None, None),  # its process is not up yet
        ScreenPoll(LONG_AGO, SETTINGS, None, 0),
        ScreenPoll(LONG_AGO, SETTINGS, WINDOW, 0),  # its window has focus, nothing drawn
        ScreenPoll(LONG_AGO, SETTINGS, WINDOW, 0),
    ]

    capped = await wait_until_still(polls(starting), quiet_s=0.05, max_wait_s=0.6)
    drawn = await wait_until_still(
        polls([*starting, still(2), still(2)]), quiet_s=0.05, max_wait_s=5
    )

    assert (capped.outcome, capped.pending) == ("capped", ("frames",))
    assert drawn.outcome == "settled"


async def test_no_window_with_focus_holds_the_wait() -> None:
    state = await wait_until_still(
        polls([ScreenPoll(LONG_AGO, LAUNCHER, None, 29)]), quiet_s=0.05, max_wait_s=0.2
    )

    assert (state.outcome, state.pending) == ("capped", ("focus",))


async def test_one_new_frame_within_the_quiet_period_is_still_a_still_screen() -> None:
    """A focused text field's cursor redraws about every 500 ms and sends no event.

    Here it redraws every other poll, so the count never holds for a whole quiet
    period, as polls of 200-300 ms under load saw between two blinks.
    """
    polled = iter(range(1_000_000))

    async def blinking() -> ScreenPoll:
        return still(40 + next(polled) // 2)

    state = await wait_until_still(blinking, quiet_s=0.12, max_wait_s=5)

    assert state.outcome == "settled"


async def test_another_app_in_front_starts_the_quiet_period_again() -> None:
    switching = polls([ScreenPoll(LONG_AGO, LAUNCHER, "Home", 9), still(9), still(9)])

    state = await wait_until_still(switching, quiet_s=0.05, max_wait_s=5)

    assert state.outcome == "settled"


async def test_an_app_that_keeps_drawing_ends_the_wait_at_its_limit() -> None:
    """An animation draws a frame every 16 ms: several between two polls."""
    frames = iter(range(1, 1_000_000, 5))

    async def always_drawing() -> ScreenPoll:
        return still(next(frames))

    state = await wait_until_still(always_drawing, quiet_s=0.05, max_wait_s=0.2)

    assert (state.outcome, state.pending) == ("capped", ("frames",))
    assert state.waited_ms >= 200


async def test_what_cannot_be_read_ends_the_wait_as_unknown_with_the_reason() -> None:
    async def broken() -> ScreenPoll:
        raise ConnectionError("device offline")

    async def ended() -> ScreenPoll:
        raise EventsEnded("the service's event stream ended: the channel was closed")

    failed = await wait_until_still(broken, quiet_s=0.05, max_wait_s=5)
    no_events = await wait_until_still(ended, quiet_s=0.05, max_wait_s=5)

    assert (failed.outcome, failed.detail) == ("unknown", "ConnectionError: device offline")
    assert (no_events.outcome, no_events.detail) == (
        "unknown",
        "EventsEnded: the service's event stream ended: the channel was closed",
    )


async def test_the_screen_is_not_still_before_the_minimum_after_an_action() -> None:
    """AndroidWorld's M3A sleeps 2 s after every action before it looks again."""
    clock = asyncio.get_running_loop().time
    acted = clock()

    held = await wait_until_still(
        polls([still()]), quiet_s=0.05, max_wait_s=5, not_before=acted + 0.3
    )
    ended = clock()
    capped = await wait_until_still(
        polls([still()]), quiet_s=0.05, max_wait_s=0.1, not_before=clock() + 5
    )

    assert held.outcome == "settled"
    assert ended - acted >= 0.3
    assert (capped.outcome, capped.pending) == ("capped", ("after_action",))


async def test_a_wait_that_goes_on_counts_from_where_it_began() -> None:
    """A capture that showed no app waits again within the limit of its first wait."""
    clock = asyncio.get_running_loop().time
    began = clock() - 0.4

    within = await wait_until_still(polls([still()]), quiet_s=0.05, max_wait_s=5, started=began)
    past = await wait_until_still(
        polls([still(since_event_ms=10)]), quiet_s=0.05, max_wait_s=0.2, started=began
    )

    assert within.outcome == "settled" and within.waited_ms >= 450
    assert (past.outcome, past.pending) == ("capped", ("events", "focus", "frames"))


def _window(window_type: str, *nodes: dict[str, object]) -> dict[str, object]:
    return {"window_type": window_type, "tree": {"nodes": list(nodes)}}


def _forest(*windows: dict[str, object]) -> AndroidAccessibilityForest:
    return AndroidAccessibilityForest.model_validate({"windows": list(windows)})


#: The status bar: a system window that says the time.
STATUS_BAR = _window("TYPE_SYSTEM", {"class_name": "android.widget.TextView", "text": "12:09"})
#: An app window whose content has not come yet: its root and a layout container.
EMPTY_APP = _window(
    "TYPE_APPLICATION",
    {"class_name": "android.widget.FrameLayout", "child_ids": [1], "is_important": True},
    {"unique_id": 1, "class_name": "android.widget.LinearLayout", "depth": 1},
)


def test_a_tree_shows_an_app_once_an_app_window_has_content() -> None:
    def app(**node: object) -> dict[str, object]:
        return _window("TYPE_APPLICATION", {"class_name": "android.view.View", **node})

    assert not shows_an_app(_forest())
    assert not shows_an_app(_forest(STATUS_BAR)), "the status bar alone: a transition"
    assert not shows_an_app(_forest(EMPTY_APP, STATUS_BAR)), "the window's empty root"
    for content in (
        {"text": "Display brightness"},  # the brightness slider, a screen of one element
        {"content_description": "Video player. Tap to show controls."},
        {"is_clickable": True},  # a button nobody labelled
        {"is_long_clickable": True},
        {"is_editable": True},
        {"is_scrollable": True},
        {"is_checkable": True},
    ):
        assert shows_an_app(_forest(app(**content), STATUS_BAR)), content
    assert not shows_an_app(_forest(app(text="   "))), "white space says nothing"
    assert shows_an_app(_forest(EMPTY_APP, app(text="Open with"))), "one window with content"


def test_a_wait_that_saw_no_app_and_waits_no_more_says_so() -> None:
    settled = SettleState(outcome="settled", waited_ms=900, loaded=None)
    capped = SettleState(outcome="capped", waited_ms=2000, loaded=None, pending=("frames",))
    unknown = SettleState(outcome="unknown", waited_ms=3, loaded=None, detail="TimeoutError")

    assert without_an_app(settled) == SettleState(
        outcome="capped", waited_ms=900, loaded=None, pending=("app_window",)
    )
    assert without_an_app(capped).pending == ("frames", "app_window")
    assert without_an_app(unknown) == unknown, "a wait that could not read the screen stays so"
