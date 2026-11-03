"""The Android actions: the browser's param contract where they share a name, and what each one sends.

A relative name is a param contract (``docs/architecture_design.md`` §4.9):
negotiation binds names, never params, so each action that takes a browser
action's relative name must take exactly its params. The Android-only ones take
the params §11.2 gives them. Every action runs through the capability against
the scripted phone (``tests/_helpers/android_device.py``), which keeps every
input its device side received: each touch a timed stroke through the
configured tap injector, keys through the helper, text through the configured
typing backend.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.actions.params import point_params
from spectragent.core.capabilities.android.actions import (
    KEYCODES,
    AndroidOpenAppAction,
    AndroidScrollAction,
    AndroidTypeAction,
    AndroidTypeCoordinateAction,
    SystemKey,
)
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.effects import (
    AndroidTextEffect,
    AndroidTouchEffect,
    ScreenPoint,
)
from spectragent.core.capabilities.android.input.tap import AccessibilityGestureInjector
from spectragent.core.capabilities.android.input.text import CommitTextBackend
from spectragent.core.capabilities.android.observations import UI_TREE, AndroidUITreeObs
from spectragent.core.trace.effects import registered_effect, restore_effects
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.android_device import (
    ScriptedPhone,
    android_capability,
    phone_server,
    settings_forest,
)
from tests._helpers.fake_adb import ShellResult

#: The actions that mean what the browser's of the same relative name mean.
SHARED = ("click", "click_coordinate", "drag", "type", "type_coordinate", "scroll", "wait")


def _schema_without_descriptions(params: type[BaseModel]) -> object:
    def strip(node: object) -> object:
        if isinstance(node, dict):
            # A description is a string; a param named "description" is a schema.
            return {
                key: strip(value)
                for key, value in node.items()
                if not (key == "description" and isinstance(value, str))
            }
        if isinstance(node, list):
            return [strip(value) for value in node]
        return node

    return strip(params.model_json_schema())


@pytest.mark.parametrize("name", SHARED)
def test_a_shared_action_takes_exactly_the_browser_actions_params(name: str) -> None:
    android = ActionRegistrar.get(f"android.{name}")
    browser = ActionRegistrar.get(f"browser.{name}")

    assert _schema_without_descriptions(android.Params) == _schema_without_descriptions(
        browser.Params
    )
    assert point_params(android.Params) == point_params(browser.Params)


@pytest.mark.parametrize(
    ("name", "properties", "required", "points"),
    [
        # A point or an element index, one of the two (the validator's rule).
        (
            "long_press",
            {"coordinate_x", "coordinate_y", "index"},
            set(),
            (("coordinate_x", "coordinate_y"),),
        ),
        ("press_key", {"key"}, {"key"}, ()),
        ("open_app", {"app_name"}, {"app_name"}, ()),
    ],
)
def test_an_android_action_takes_the_params_it_is_designed_with(
    name: str, properties: set[str], required: set[str], points: tuple[tuple[str, str], ...]
) -> None:
    params = ActionRegistrar.get(f"android.{name}").Params
    schema = params.model_json_schema()

    assert set(schema["properties"]) == properties
    assert set(schema.get("required", ())) == required
    assert point_params(params) == list(points)


def test_the_system_keys_include_back_home_and_enter_and_each_has_a_key_code() -> None:
    assert {"back", "home", "enter"} <= set(get_args(SystemKey))
    assert set(KEYCODES) == set(get_args(SystemKey))


def test_scroll_offers_an_element_index_only_when_the_run_has_the_ui_tree() -> None:
    scroll = ActionRegistrar.get("android.scroll")()

    without = scroll.descriptor(fragments=frozenset({"android.screenshot"}))
    with_tree = scroll.descriptor(fragments=frozenset({"android.screenshot", "android.ui_tree"}))

    assert "index" not in without.params and without.element_params == ()
    assert "index" in with_tree.params and with_tree.element_params == ("index",)


@pytest.fixture
async def phone() -> ScriptedPhone:
    return ScriptedPhone()


@pytest.fixture
async def capability(phone: ScriptedPhone) -> AsyncIterator[AndroidCapability]:
    async with phone_server(phone) as adb, android_capability(adb) as capability:
        phone.inputs.clear()  # setup's test tap
        yield capability


async def act(capability: AndroidCapability, name: str, **params: Any) -> ActionResult:
    return await capability.execute_action(Action(name=f"android.{name}", params=params))


async def test_a_tap_a_long_press_and_a_drag_are_timed_strokes_through_the_tap_injector(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    tap = await act(capability, "click_coordinate", coordinate_x=100, coordinate_y=200)
    press = await act(capability, "long_press", coordinate_x=100, coordinate_y=200)
    drag = await act(capability, "drag", start_x=10, start_y=20, end_x=30, end_y=40)

    assert phone.inputs == [
        "helper stroke (100,200)@0 (100,200)@50",
        "helper stroke (100,200)@0 (100,200)@1000",  # held where it went down
        "helper stroke (10,20)@0 (30,40)@1000",
    ]
    lines = []
    for result in (tap, press, drag):
        assert result.success and isinstance(result.effect, AndroidTouchEffect)
        assert (result.effect.touch.delivered_by, result.effect.touch.elapsed_ms) == (
            "shell_injection",
            55,
        )
        lines.append(result.effect.describe())
    assert lines == [
        "Tapped (100, 200).",
        "Pressed (100, 200) for 1000 ms.",
        "Dragged from (10, 20) to (30, 40) over 1000 ms.",
    ]


async def test_the_accessibility_gesture_injector_sends_strokes_through_the_service() -> None:
    phone = ScriptedPhone()
    async with (
        phone_server(phone) as adb,
        android_capability(adb, tap_injector=AccessibilityGestureInjector()) as capability,
    ):
        phone.inputs.clear()
        tapped = await act(capability, "click_coordinate", coordinate_x=1, coordinate_y=2)

    assert phone.inputs == ["service stroke (1,2)@0 (1,2)@50"]
    assert isinstance(tapped.effect, AndroidTouchEffect)
    assert tapped.effect.touch.delivered_by == "accessibility_gesture"


async def test_a_gesture_the_system_cancels_fails_the_action_and_says_so() -> None:
    """Without faketouch the system cancels every accessibility gesture (§11.2)."""
    phone = ScriptedPhone()
    phone.side.faketouch = False
    async with (
        phone_server(phone) as adb,
        android_capability(adb, tap_injector=AccessibilityGestureInjector()) as capability,
    ):
        tapped = await act(capability, "click_coordinate", coordinate_x=1, coordinate_y=2)

    assert not tapped.success
    assert tapped.error is not None and "gesture_cancelled" in tapped.error


def test_a_touch_effect_reads_back_from_the_trace_as_its_own_type() -> None:
    effect = AndroidTouchEffect.model_validate(
        {
            "effect_id": "android.scroll",
            "touch": {
                "gesture": "swipe",
                "start": {"x": 540, "y": 1200},
                "end": {"x": 540, "y": 200},
                "duration_ms": 500,
                "elapsed_ms": 503,
                "delivered_by": "accessibility_gesture",
            },
        }
    )

    result = ActionResult(success=True, effect=effect)
    restored = restore_effects(ActionResult, result.model_dump(mode="json"), registered_effect)
    assert ActionResult.model_validate(restored).effect == effect


@pytest.mark.parametrize(
    ("direction", "swipe"),
    [
        # down shows what is further down: the finger moves up, the content with it.
        ("down", "helper stroke (540,1200)@0 (540,200)@500"),
        ("up", "helper stroke (540,1200)@0 (540,2200)@500"),
        # right shows what is further right: the finger moves left (held on the screen).
        ("right", "helper stroke (540,1200)@0 (0,1200)@500"),
        ("left", "helper stroke (540,1200)@0 (1079,1200)@500"),
    ],
)
async def test_a_scroll_means_what_it_means_on_the_browser(
    capability: AndroidCapability, phone: ScriptedPhone, direction: str, swipe: str
) -> None:
    assert (await act(capability, "scroll", direction=direction)).success

    assert phone.inputs == [swipe]


async def test_a_scroll_starts_at_its_start_point_and_goes_its_amount(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    scrolled = await act(
        capability, "scroll", direction="down", start_x=300, start_y=2000, amount=600
    )

    assert phone.inputs == ["helper stroke (300,2000)@0 (300,1400)@500"]
    assert isinstance(scrolled.effect, AndroidTouchEffect)
    assert scrolled.effect.describe() == "Swiped from (300, 2000) to (300, 1400) over 500 ms."


# ── element indexes: click, and scroll's index ───────────────────────


async def observe_tree(capability: AndroidCapability, cycle: int = 1) -> AndroidUITreeObs:
    await capability.begin_observation(cycle)
    tree = await capability.resolve_fragment(UI_TREE)
    assert isinstance(tree, AndroidUITreeObs)
    return tree


async def test_click_taps_the_centre_of_the_element_its_index_names(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The scripted Settings list: element 1 is "Connected devices", (0, 450)-(1080, 600)."""
    tree = await observe_tree(capability)
    assert [node.text for _, node in tree.tree().element_nodes()] == [
        "Network & internet",
        "Connected devices",
    ]

    tapped = await act(capability, "click", index=1)

    assert tapped.success and phone.inputs == ["helper stroke (540,525)@0 (540,525)@50"]
    assert isinstance(tapped.effect, AndroidTouchEffect)
    assert tapped.effect.describe() == "Tapped (540, 525)."


async def test_an_element_partly_off_the_screen_is_tapped_where_it_shows(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    phone.forest = settings_forest(
        ("Network & internet", (0, 300, 1080, 450)), ("Low on the list", (0, 2300, 1080, 2600))
    )
    await observe_tree(capability)

    assert (await act(capability, "click", index=1)).success
    assert phone.inputs == ["helper stroke (540,2350)@0 (540,2350)@50"]


async def test_an_index_from_the_tree_before_the_last_action_is_refused_and_touches_nothing(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """After an action the screen may have changed, and its numbers with it."""
    await observe_tree(capability)
    assert (await act(capability, "click", index=0)).success
    phone.inputs.clear()

    for refused in (
        await act(capability, "click", index=1),
        await act(capability, "scroll", direction="down", index=1),
    ):
        assert not refused.success and refused.effect is None
        assert refused.error == (
            "Element 1 is from the UI tree captured before the last action, and the screen may "
            "have changed since; the next observation numbers the elements again."
        )
    assert phone.inputs == []
    await observe_tree(capability, cycle=2)
    assert (await act(capability, "click", index=1)).success, "the next tree names it again"


async def test_a_wait_ends_the_trees_numbers_too(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    await observe_tree(capability)
    assert (await act(capability, "wait", seconds=0)).success

    refused = await act(capability, "click", index=0)

    assert not refused.success and "before the last action" in (refused.error or "")
    assert phone.inputs == []


async def test_an_index_the_tree_does_not_have_is_refused_naming_the_ones_it_has(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    await observe_tree(capability)

    refused = await act(capability, "click", index=7)

    assert refused.error == "The screen's UI tree has no element 7; its elements are 0-1."
    assert phone.inputs == []


async def test_the_numbers_a_refusal_names_show_the_ones_left_unused(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The cleaner leaves an element below the screen out and keeps its number unused."""
    phone.forest = settings_forest(
        ("Network & internet", (0, 300, 1080, 450)),
        ("Below the screen", (0, 2500, 1080, 2650)),
        ("Connected devices", (0, 450, 1080, 600)),
    )
    await observe_tree(capability)

    refused = await act(capability, "click", index=1)

    assert refused.error == "The screen's UI tree has no element 1; its elements are 0, 2."
    assert phone.inputs == []


async def test_an_index_before_any_tree_was_captured_is_refused(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    refused = await act(capability, "click", index=0)

    assert refused.error == "Element 0 names nothing: no UI tree was captured yet in this run."
    assert phone.inputs == []


async def test_an_index_in_a_run_that_captures_no_tree_names_nothing() -> None:
    phone = ScriptedPhone()
    async with (
        phone_server(phone) as adb,
        android_capability(adb, fragments={"android.screenshot"}) as capability,
    ):
        phone.inputs.clear()
        refused = await act(capability, "scroll", direction="down", index=0)

    assert refused.error == "Element 0 names nothing: this run does not capture the UI tree."
    assert phone.inputs == []


async def test_a_scroll_with_an_index_swipes_from_that_elements_centre(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    await observe_tree(capability)

    scrolled = await act(capability, "scroll", direction="down", index=0, amount=200)

    assert scrolled.success and phone.inputs == ["helper stroke (540,375)@0 (540,175)@500"]


def _swiping_to_the_edge(capability: AndroidCapability) -> None:
    """The scroll a run for M3A configures: to its target's edge, in ``input swipe``'s 300 ms."""
    capability.actions = [
        AndroidScrollAction(swipe_to="target_edge", duration_ms=300)
        if action.action_id == "android.scroll"
        else action
        for action in capability.actions
    ]


@pytest.mark.parametrize(
    ("direction", "swipe"),
    [
        # AndroidWorld's scroll: from the centre to the edge the finger moves towards.
        ("down", "helper stroke (540,1200)@0 (540,0)@300"),
        ("up", "helper stroke (540,1200)@0 (540,2399)@300"),  # AndroidWorld's ends on 2400
        ("right", "helper stroke (540,1200)@0 (0,1200)@300"),
        ("left", "helper stroke (540,1200)@0 (1079,1200)@300"),
    ],
)
async def test_a_scroll_to_the_edge_swipes_from_the_middle_of_the_screen_to_its_edge(
    capability: AndroidCapability, phone: ScriptedPhone, direction: str, swipe: str
) -> None:
    _swiping_to_the_edge(capability)

    assert (await act(capability, "scroll", direction=direction)).success

    assert phone.inputs == [swipe]


async def test_a_scroll_to_the_edge_stops_at_its_targets_edge_unless_given_an_amount(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """Element 0 is (0, 300)-(1080, 450); a start point's target is the screen."""
    _swiping_to_the_edge(capability)
    descriptor = next(d for d in capability.get_action_descriptors() if d.name == "android.scroll")
    assert "to the edge of the element, or of the screen" in descriptor.description

    await observe_tree(capability)
    assert (await act(capability, "scroll", direction="down", index=0)).success
    await observe_tree(capability, cycle=2)
    assert (await act(capability, "scroll", direction="up", index=0, amount=200)).success
    assert (await act(capability, "scroll", direction="down", start_x=300, start_y=2000)).success

    assert phone.inputs == [
        "helper stroke (540,375)@0 (540,300)@300",
        "helper stroke (540,375)@0 (540,575)@300",
        "helper stroke (300,2000)@0 (300,0)@300",
    ]


async def test_a_long_press_by_index_holds_the_centre_of_that_element(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    await observe_tree(capability)

    pressed = await act(capability, "long_press", index=1)

    assert pressed.success and phone.inputs == ["helper stroke (540,525)@0 (540,525)@1000"]
    assert isinstance(pressed.effect, AndroidTouchEffect)
    assert pressed.effect.describe() == "Pressed (540, 525) for 1000 ms."


async def test_a_long_press_by_an_index_the_tree_does_not_have_touches_nothing(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    await observe_tree(capability)

    refused = await act(capability, "long_press", index=7)

    assert refused.error == "The screen's UI tree has no element 7; its elements are 0-1."
    assert phone.inputs == []


# ── typing by index: type ─────────────────────────────────────────────


async def test_typing_by_index_taps_the_elements_centre_and_types_into_the_field_there(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The browser's ``type``: focus the element (here a tap at its centre), then type."""
    await observe_tree(capability)
    # The field with focus is the element's: (0, 450)-(1080, 600), centre (540, 525).
    phone.text_field, phone.field_bounds = True, (0, 450, 1080, 600)

    typed = await act(capability, "type", index=1, text="hi", clear=False)

    assert phone.inputs == [
        "helper stroke (540,525)@0 (540,525)@50",
        "service set_text 'hi' at the cursor",
    ]
    assert isinstance(typed.effect, AndroidTextEffect) and typed.effect.text_entered
    assert typed.effect.describe() == (
        "Tapped (540, 525). Entered the text into EditText android:id/search_src_text of "
        "com.google.android.apps.nexuslauncher, at its cursor."
    )


async def test_typing_by_index_clears_the_field_unless_told_not_to(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    await observe_tree(capability)
    phone.text_field, phone.field_bounds = True, (0, 450, 1080, 600)

    assert (await act(capability, "type", index=1, text="hi")).success

    assert phone.inputs[-1] == "service set_text 'hi' replace", "clear_by_default is true"


async def test_typing_by_index_where_focus_stays_on_another_field_is_refused_naming_the_element(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The focus rules after the tap are type_coordinate's: the element was not the field."""
    capability.actions = [AndroidTypeAction(focus_wait_ms=200)]
    await observe_tree(capability)
    phone.text_field = True  # a field at (0, 100)-(1080, 250) has focus, and keeps it

    refused = await act(capability, "type", index=1, text="hi")

    assert refused.error == (
        "No text field took focus at element 1: focus stayed on EditText "
        "android:id/search_src_text of com.google.android.apps.nexuslauncher, away from "
        "element 1, so no text was typed."
    )
    assert phone.inputs == ["helper stroke (540,525)@0 (540,525)@50"], "the tap, nothing typed"
    assert isinstance(refused.effect, AndroidTextEffect) and not refused.effect.text_entered


async def test_typing_by_an_index_from_an_older_tree_touches_nothing(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    await observe_tree(capability)
    assert (await act(capability, "click", index=0)).success
    phone.inputs.clear()

    refused = await act(capability, "type", index=1, text="hi")

    assert not refused.success and "before the last action" in (refused.error or "")
    assert refused.effect is None and phone.inputs == []


@dataclass
class _FieldTakingFocus(ScriptedPhone):
    """A phone whose text field takes focus once the input method has been asked *late* times."""

    late: int = 2
    #: How many times the input method was asked.
    asked: int = 0

    async def __call__(self, command: str) -> ShellResult:
        if command == "dumpsys input_method":
            self.asked += 1
            self.text_field = self.asked > self.late
        return await super().__call__(command)


async def test_typing_at_a_point_taps_and_enters_the_text_over_the_fields_or_at_its_cursor(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    phone.text_field = True

    replaced = await act(
        capability, "type_coordinate", coordinate_x=5, coordinate_y=160, text="hi there"
    )
    inserted = await act(
        capability, "type_coordinate", coordinate_x=5, coordinate_y=160, text="x", clear=False
    )

    assert phone.inputs == [
        "helper stroke (5,160)@0 (5,160)@50",
        "service set_text 'hi there' replace",
        "helper stroke (5,160)@0 (5,160)@50",
        "service set_text 'x' at the cursor",
    ]
    assert isinstance(replaced.effect, AndroidTextEffect) and replaced.effect.text_entered
    assert replaced.effect.entered_by == "set_text" and replaced.effect.replaced
    assert replaced.effect.tap is not None and replaced.effect.tap.delivered_by == "shell_injection"
    assert replaced.effect.describe() == (
        "Tapped (5, 160). Entered the text into EditText android:id/search_src_text of "
        "com.google.android.apps.nexuslauncher, replacing its text."
    )
    assert isinstance(inserted.effect, AndroidTextEffect) and not inserted.effect.replaced
    assert "hi there" not in replaced.effect.model_dump_json(), "the text is never recorded"


async def test_the_commit_text_backend_types_through_the_services_input_method() -> None:
    phone = ScriptedPhone(text_field=True)
    async with (
        phone_server(phone) as adb,
        android_capability(adb, typing_backend=CommitTextBackend()) as capability,
    ):
        typed = await act(capability, "type_coordinate", text="蓝牙")
        appended = await act(capability, "type_coordinate", text="!", clear=False)

    assert phone.inputs[-2:] == [
        "service commit_text '蓝牙' replace",
        "service commit_text '!' at the cursor",
    ]
    assert typed.success and appended.success
    assert isinstance(typed.effect, AndroidTextEffect) and typed.effect.entered_by == "commit_text"
    assert typed.effect.field_class is None, "an input method knows only the field's package"


async def test_typing_at_a_point_where_focus_stays_on_a_field_away_from_it_is_refused(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The tap landed on the caption of another field, and the field that had focus keeps it
    (user rulings of 2026-11-02): nothing is cleared or typed, and the tap is reported."""
    capability.actions = [AndroidTypeCoordinateAction(focus_wait_ms=300)]
    # A field has focus at (0, 100)-(1080, 250); drawn with its own label, it reaches up to 50.
    phone.text_field, phone.drawn_bounds = True, (0, 50, 1080, 250)

    started = time.monotonic()
    result = await act(capability, "type_coordinate", coordinate_x=540, coordinate_y=900, text="hi")

    assert (result.success, result.error) == (
        False,
        "No text field took focus at the tapped point: focus stayed on EditText "
        "android:id/search_src_text of com.google.android.apps.nexuslauncher, away from the "
        "tapped point, so no text was typed.",
    )
    assert time.monotonic() - started >= 0.3, "focus had its time to leave the field"
    assert phone.inputs == ["helper stroke (540,900)@0 (540,900)@50"], "nothing entered"
    effect = result.effect
    assert isinstance(effect, AndroidTextEffect) and not effect.text_entered
    assert effect.tap is not None and effect.tap.start == ScreenPoint(x=540, y=900)
    assert effect.describe() == "Tapped (540, 900). No text was entered."
    assert phone.focus_reads[0] and not any(phone.focus_reads[1:]), (
        "the field's bounds are asked for once, before the tap"
    )


async def test_typing_at_a_point_on_the_label_of_the_focused_field_types(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The label above the field keeps focus in it: it is inside the field as drawn, the
    layout the field shares with its label (user ruling 2026-11-02)."""
    phone.text_field = True
    phone.field_bounds, phone.drawn_bounds = (0, 300, 1080, 400), (0, 200, 1080, 400)

    started = time.monotonic()
    result = await act(capability, "type_coordinate", coordinate_x=540, coordinate_y=250, text="hi")

    assert result.success, result.error
    assert phone.inputs[-1] == "service set_text 'hi' replace"
    assert time.monotonic() - started < 1.0, "no wait for focus to leave"


async def test_typing_at_a_point_on_the_focused_fields_own_label_beside_it_types(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """A label beside its field, sharing only a layout with other fields: the view that labels
    the focused field still names it (user ruling 2026-11-02)."""
    phone.text_field = True
    phone.field_bounds, phone.label_bounds = (300, 300, 1080, 400), (0, 300, 280, 400)

    result = await act(capability, "type_coordinate", coordinate_x=140, coordinate_y=350, text="hi")

    assert result.success, result.error
    assert phone.inputs[-1] == "service set_text 'hi' replace"


async def test_typing_at_a_point_on_another_window_over_the_field_is_refused(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The screen's only text field makes the whole window its field, and the status bar is
    another window drawn over it: a tap there leaves focus in the field and is refused."""
    capability.actions = [AndroidTypeCoordinateAction(focus_wait_ms=200)]
    phone.text_field, phone.drawn_bounds, phone.top_window = True, (0, 0, 1080, 2400), 9

    result = await act(capability, "type_coordinate", coordinate_x=540, coordinate_y=40, text="hi")

    assert not result.success
    assert result.error is not None and "focus stayed on EditText" in result.error
    assert phone.inputs == ["helper stroke (540,40)@0 (540,40)@50"], "nothing entered"


async def test_typing_at_a_point_types_as_asked_when_the_service_cannot_give_the_field(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The service gave up walking a tree too large: where the field is cannot be told."""
    phone.text_field, phone.drawn_bounds_given = True, False

    result = await act(capability, "type_coordinate", coordinate_x=540, coordinate_y=900, text="hi")

    assert result.success, result.error
    assert phone.inputs[-1] == "service set_text 'hi' replace"


async def test_typing_at_a_point_types_into_a_field_that_took_focus_wherever_it_is(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The tap opens a screen whose field, away from the point, takes focus: that types."""
    phone.text_field = False  # nothing to type into before the tap

    def opens_a_search_screen() -> None:
        phone.text_field, phone.field_bounds, phone.field_node = True, (0, 100, 1080, 250), 5

    phone.on_tap = opens_a_search_screen
    result = await act(capability, "type_coordinate", coordinate_x=540, coordinate_y=660, text="hi")

    assert result.success, result.error
    assert phone.inputs[-1] == "service set_text 'hi' replace"


async def test_typing_at_a_point_types_when_focus_moved_to_another_field(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """Another field had focus before the tap; the one the tap gave focus to takes the text."""
    phone.text_field, phone.field_bounds, phone.field_node = True, (0, 100, 1080, 250), 1

    def focuses_another_field() -> None:
        phone.field_bounds, phone.field_node = (0, 2000, 1080, 2150), 2  # away from the point

    phone.on_tap = focuses_another_field
    result = await act(capability, "type_coordinate", coordinate_x=540, coordinate_y=900, text="hi")

    assert result.success, result.error


async def test_typing_at_a_point_inside_the_field_that_has_focus_types(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    phone.text_field, phone.field_bounds = True, (0, 100, 1080, 250)

    result = await act(capability, "type_coordinate", coordinate_x=540, coordinate_y=160, text="hi")

    assert result.success, result.error


async def test_typing_at_a_point_in_the_focused_field_types_though_the_keyboard_moves_it(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The point was in the field when tapped; the keyboard the tap opens pushes it up."""
    phone.text_field, phone.field_bounds = True, (0, 1900, 1080, 2050)

    def keyboard_opens() -> None:
        phone.field_bounds = (0, 900, 1080, 1050)  # the same field, away from the point now

    phone.on_tap = keyboard_opens
    result = await act(
        capability, "type_coordinate", coordinate_x=540, coordinate_y=1950, text="hi"
    )

    assert result.success, result.error
    assert phone.inputs[-1] == "service set_text 'hi' replace"


@pytest.mark.parametrize(
    "how",
    [{"text_entered": True}, {"text_entered": False, "entered_by": "set_text", "replaced": True}],
)
def test_a_text_effect_says_how_text_went_in_exactly_when_it_did(how: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="exactly when text was entered"):
        AndroidTextEffect.model_validate({"effect_id": "android.type_coordinate", **how})


async def test_typing_at_a_point_types_as_asked_when_the_service_names_no_field(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The service's report lags the window manager's; one that never comes cannot tell."""
    capability.actions = [AndroidTypeCoordinateAction(focus_wait_ms=200)]
    phone.text_field = True

    def report_lags() -> None:
        phone.field_bounds = None

    phone.on_tap = report_lags
    result = await act(capability, "type_coordinate", coordinate_x=540, coordinate_y=900, text="hi")

    assert result.success, result.error
    assert phone.inputs[-1] == "service set_text 'hi' replace"


async def test_typing_at_a_point_waits_for_focus_to_leave_the_field_it_stayed_on(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """Focus leaves the old field a moment after the tap, for the field at the point."""
    phone.text_field, phone.field_bounds = True, (0, 100, 1080, 250)

    async def focus_moves() -> None:
        await asyncio.sleep(0.3)
        phone.field_bounds, phone.field_node = (0, 800, 1080, 1000), 2

    moving = asyncio.ensure_future(focus_moves())
    result = await act(capability, "type_coordinate", coordinate_x=540, coordinate_y=900, text="hi")
    await moving

    assert result.success, result.error
    assert phone.inputs[-1] == "service set_text 'hi' replace"


@pytest.mark.parametrize("point", [{"coordinate_x": 5, "coordinate_y": 6}, {}], ids=["tap", "none"])
async def test_typing_waits_for_a_field_that_takes_focus_a_moment_later(
    point: dict[str, int],
) -> None:
    """After the tap that opens it, or after an earlier action that did."""
    phone = _FieldTakingFocus(field_bounds=(0, 0, 1080, 300))
    async with phone_server(phone) as adb, android_capability(adb) as capability:
        result = await act(capability, "type_coordinate", text="hi", **point)

    assert (result.success, result.error) == (True, None)
    assert phone.asked == 3
    assert phone.inputs[-1] == "service set_text 'hi' replace"


async def test_a_tap_that_gives_no_text_field_focus_types_nothing(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    capability.actions = [AndroidTypeCoordinateAction(focus_wait_ms=300)]

    started = time.monotonic()
    result = await act(capability, "type_coordinate", coordinate_x=5, coordinate_y=6, text="hi")

    assert (result.success, result.error) == (
        False,
        "No text field took focus at the tapped point: focus is in "
        "com.google.android.apps.nexuslauncher/"
        "com.google.android.apps.nexuslauncher.NexusLauncherActivity, not on a text field, "
        "so no text was typed.",
    )
    assert time.monotonic() - started >= 0.3, "the wait was spent before refusing"
    assert phone.inputs == ["helper stroke (5,6)@0 (5,6)@50"]
    assert isinstance(result.effect, AndroidTextEffect)
    assert (result.effect.text_entered, result.effect.tap is not None) == (False, True)


async def test_typing_without_a_point_types_into_the_field_that_has_focus(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    phone.text_field = True

    assert (await act(capability, "type_coordinate", text="hi there")).success
    assert (await act(capability, "type_coordinate", text="x", clear=False)).success

    assert phone.inputs == [
        "service set_text 'hi there' replace",
        "service set_text 'x' at the cursor",
    ]


@pytest.mark.parametrize(
    ("text_field", "window_over", "where"),
    [
        (
            False,
            None,
            "focus is in com.google.android.apps.nexuslauncher/"
            "com.google.android.apps.nexuslauncher.NexusLauncherActivity, not on a text field",
        ),
        # The field keeps its input connection under the shade, and the text would go to the shade.
        (True, "NotificationShade", "focus is in NotificationShade, not on a text field"),
    ],
    ids=["no text field", "the notification shade over a text field"],
)
async def test_typing_without_a_point_sends_nothing_when_no_text_field_has_focus(
    capability: AndroidCapability,
    phone: ScriptedPhone,
    text_field: bool,
    window_over: str | None,
    where: str,
) -> None:
    capability.actions = [AndroidTypeCoordinateAction(focus_wait_ms=0)]
    phone.text_field, phone.window_over = text_field, window_over

    result = await act(capability, "type_coordinate", text="hi")

    assert (result.success, result.error) == (
        False,
        f"No text field has focus: {where}, so no text was typed.",
    )
    assert phone.inputs == []


async def test_typing_where_the_device_cannot_tell_types_as_asked(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """The input method's client did not answer: only a measured "no text field" refuses."""
    phone.text_field = None

    result = await act(capability, "type_coordinate", text="hi", clear=False)

    # The service itself then finds no editable field with focus, and says so.
    assert not result.success
    assert result.error is not None and "no_focused_field" in result.error
    assert isinstance(result.effect, AndroidTextEffect) and not result.effect.text_entered
    assert result.effect.tap is None, "typing without a point taps nothing"


async def test_chinese_text_types_as_any_other(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """``input text`` could type only the virtual keyboard's characters; the service takes any."""
    phone.text_field = True

    assert (await act(capability, "type_coordinate", text="你好", clear=False)).success
    assert phone.inputs == ["service set_text '你好' at the cursor"]


async def test_system_keys_press_their_android_key_codes(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    for key in ("back", "home", "enter"):
        assert (await act(capability, "press_key", key=key)).success

    assert phone.inputs == [
        "helper key KEYCODE_BACK",
        "helper key KEYCODE_HOME",
        "helper key KEYCODE_ENTER",
    ]


async def test_an_app_opens_by_its_name_or_its_package(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    assert (await act(capability, "open_app", app_name="  settings ")).success
    assert phone.focused == "com.android.settings/.Settings"

    await act(capability, "press_key", key="home")
    assert (await act(capability, "open_app", app_name="com.android.settings")).success
    assert phone.focused == "com.android.settings/.Settings"


async def test_a_name_the_table_gives_an_activity_starts_that_activity(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    """AndroidWorld opens OpenTracks on its track list, past the introduction its launcher shows."""
    launcher = "de.dennisguse.opentracks/.introduction.IntroductionActivity"
    track_list = "de.dennisguse.opentracks/de.dennisguse.opentracks.TrackListActivity"
    phone.launchable["de.dennisguse.opentracks"] = launcher
    capability.actions = [AndroidOpenAppAction(apps={"OpenTracks": track_list})]

    assert (await act(capability, "open_app", app_name="opentracks")).success
    assert phone.focused == track_list
    # Only the table names an activity: the package opens its launcher activity, and an
    # activity the agent names is refused.
    assert (await act(capability, "open_app", app_name="de.dennisguse.opentracks")).success
    assert phone.focused == launcher
    assert not (await act(capability, "open_app", app_name=track_list)).success


async def test_an_app_that_is_not_there_is_refused_naming_the_apps_that_are_known(
    capability: AndroidCapability,
) -> None:
    for name in ("Notes app", "Chrome"):  # unknown, and known but not installed on this phone
        result = await act(capability, "open_app", app_name=name)
        assert not result.success
        assert result.error is not None and "Settings" in result.error


async def test_a_wait_touches_nothing_and_refuses_a_wait_past_its_maximum(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    assert (await act(capability, "wait", seconds=0)).success
    refused = await act(capability, "wait", seconds=31)

    assert not refused.success and refused.error is not None and "outside 0..30" in refused.error
    assert phone.inputs == []
