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
from spectragent.core.trace.effects import registered_effect, restore_effects
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.android_device import ScriptedPhone, android_capability, phone_server
from tests._helpers.fake_adb import ShellResult

#: The actions that mean what the browser's of the same relative name mean.
SHARED = ("click_coordinate", "drag", "type_coordinate", "scroll", "wait")


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
        (
            "long_press",
            {"coordinate_x", "coordinate_y"},
            {"coordinate_x", "coordinate_y"},
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
    assert set(schema["required"]) == required
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
