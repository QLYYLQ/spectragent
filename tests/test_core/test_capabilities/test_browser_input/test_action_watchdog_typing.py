"""Locks the capability → ActionWatchdog → HumanKeyboard wiring for typing.

The mirror of ``test_action_watchdog_humanize.py`` on the keyboard axis: a
regression that forgets to thread the ``typing_rhythm`` strategy from the
capability into the watchdog leaves a run silently back on the bulk insert,
which is exactly the thing that has no observable timing at all.

Both type verbs are covered, because they are two entry points into one
focus-relative routine: ``browser.type`` (selector-map index) and
``browser.type_coordinate`` (the vision-only path, which clicks first).
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.browser import BrowserCapability
from spectragent.core.capabilities.browser.actions.type_action import TypeAction
from spectragent.core.capabilities.browser.actions.type_coordinate import (
    TypeCoordinateAction,
)
from spectragent.core.capabilities.browser.dom.models import DOMRect
from spectragent.core.capabilities.browser.input import HumanRhythm, InstantRhythm
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import ActionWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import (
    TypeAtCoordinateEvent,
    TypeTextEvent,
)
from tests.test_core.test_capabilities.test_browser_action_click_robustness import (
    FakeCDP,
    _make_node,
)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "spectragent.core.capabilities.browser.input._schedule.asyncio.sleep",
        fake_sleep,
    )


def _watchdog(cdp: FakeCDP, rhythm: object | None) -> ActionWatchdog:
    node = _make_node(42, DOMRect(x=400.0, y=300.0, width=100.0, height=40.0))
    return ActionWatchdog(
        cdp=cdp,  # type: ignore[arg-type]
        selector_map_getter=lambda: {42: node},
        typing_rhythm=rhythm,  # type: ignore[arg-type]
    )


def _typing_calls(cdp: FakeCDP) -> list[str]:
    return [
        c.method
        for c in cdp.calls
        if c.method in ("Input.insertText", "Input.dispatchKeyEvent")
    ]


class TestTheDefaultIsOneBulkInsert:
    """The canary: no rhythm configured == the pre-rhythm CDP traffic."""

    async def test_type_text_sends_exactly_one_insert_text(self) -> None:
        cdp = FakeCDP()
        result = await _watchdog(cdp, None)._on_type_text(
            TypeTextEvent(backend_node_id=42, text="hello", clear=False)
        )
        assert result == {"typed": True, "length": 5}
        assert _typing_calls(cdp) == ["Input.insertText"]
        insert = next(c for c in cdp.calls if c.method == "Input.insertText")
        assert insert.params == {"text": "hello"}

    async def test_clear_still_sends_its_four_key_events_then_the_insert(self) -> None:
        # The clear is a shortcut, not typing: select-all + Backspace, at
        # once, in both modes. ``clear`` defaults to True on the event, so
        # this is what the default type verb actually emits.
        cdp = FakeCDP()
        await _watchdog(cdp, InstantRhythm())._on_type_text(
            TypeTextEvent(backend_node_id=42, text="hi", clear=True)
        )
        assert _typing_calls(cdp) == ["Input.dispatchKeyEvent"] * 4 + ["Input.insertText"]

    async def test_type_at_coordinate_sends_exactly_one_insert_text(self) -> None:
        cdp = FakeCDP()
        result = await _watchdog(cdp, None)._on_type_at_coordinate(
            TypeAtCoordinateEvent(
                coordinate_x=10, coordinate_y=20, text="hello", clear=False
            )
        )
        assert result == {"typed": True, "length": 5}
        assert _typing_calls(cdp) == ["Input.insertText"]


class TestTheStrategyReachesTheWatchdog:
    async def test_a_human_rhythm_dispatches_key_events_per_character(
        self, no_sleep: None
    ) -> None:
        cdp = FakeCDP()
        await _watchdog(cdp, HumanRhythm(seed=1))._on_type_text(
            TypeTextEvent(backend_node_id=42, text="hello", clear=False)
        )
        key_events = [c for c in cdp.calls if c.method == "Input.dispatchKeyEvent"]
        assert [c.params["type"] for c in key_events] == ["keyDown", "keyUp"] * 5
        assert "Input.insertText" not in _typing_calls(cdp)

    async def test_the_coordinate_path_humanizes_too(self, no_sleep: None) -> None:
        cdp = FakeCDP()
        await _watchdog(cdp, HumanRhythm(seed=1))._on_type_at_coordinate(
            TypeAtCoordinateEvent(
                coordinate_x=10, coordinate_y=20, text="hi", clear=False
            )
        )
        key_events = [c for c in cdp.calls if c.method == "Input.dispatchKeyEvent"]
        assert [c.params["type"] for c in key_events] == ["keyDown", "keyUp"] * 2

    def test_the_watchdog_follows_the_strategy(self) -> None:
        cdp = FakeCDP()
        assert ActionWatchdog(cdp=cdp, typing_rhythm=HumanRhythm())._keyboard.humanized  # type: ignore[arg-type]
        assert not ActionWatchdog(cdp=cdp, typing_rhythm=InstantRhythm())._keyboard.humanized  # type: ignore[arg-type]
        assert not ActionWatchdog(cdp=cdp)._keyboard.humanized  # type: ignore[arg-type]

    def test_the_capability_hands_its_rhythm_to_the_context(self) -> None:
        cap = BrowserCapability()
        cap._cdp = FakeCDP()  # type: ignore[assignment]
        assert isinstance(cap.typing_rhythm, InstantRhythm)
        assert isinstance(cap._watchdog_context("sid").typing_rhythm, InstantRhythm)

        cap.typing_rhythm = HumanRhythm(seed=5)
        watchdog = ActionWatchdog.build(cap._watchdog_context("sid"))
        assert watchdog._keyboard.humanized is True

    def test_the_two_axes_are_independent(self) -> None:
        """A curved cursor with a bulk insert, and the reverse, are both legal.

        They are separate receptors on purpose — one shared "humanization
        level" would make the pair inseparable.
        """
        from spectragent.core.capabilities.browser.input import WindMouseTrajectory

        cdp = FakeCDP()
        watchdog = ActionWatchdog(
            cdp=cdp,  # type: ignore[arg-type]
            mouse_trajectory=WindMouseTrajectory(seed=1),
            typing_rhythm=InstantRhythm(),
        )
        assert watchdog._mouse.humanized is True
        assert watchdog._keyboard.humanized is False


class TestTheActionSeamIsUnchanged:
    """§2.5: what the agent emits is what ``descriptor().params`` declares.

    The rhythm is a capability-side side-effect knob; it must not appear in,
    or change, either type verb's advertised contract.
    """

    def test_type_advertises_index_and_text_only(self) -> None:
        params = TypeAction().descriptor().params
        assert set(params) == {"index", "text", "clear"}

    def test_type_coordinate_advertises_its_point_and_text_only(self) -> None:
        params = TypeCoordinateAction().descriptor().params
        assert set(params) == {"coordinate_x", "coordinate_y", "text", "clear"}

    def test_no_rhythm_knob_leaks_into_either_descriptor(self) -> None:
        for action in (TypeAction(), TypeCoordinateAction()):
            descriptor = action.descriptor()
            assert "rhythm" not in str(descriptor.params_schema)
            assert "typing_rhythm" not in descriptor.params
