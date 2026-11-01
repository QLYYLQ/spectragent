"""The Android actions: the browser's param contract where they share a name, and what each one sends.

A relative name is a param contract (``docs/architecture_design.md`` §4.9):
negotiation binds names, never params, so each action that takes a browser
action's relative name must take exactly its params. The Android-only ones take
the params §11.2 gives them. Every action runs through the capability against
the scripted phone (``tests/_helpers/android_device.py``), which keeps the
``input`` commands it received.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.android_device import SERIAL, ScriptedPhone, android_capability
from tests._helpers.fake_adb import FakeAdbServer

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
        ("type_text", {"text"}, {"text"}, ()),
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
    async with FakeAdbServer({SERIAL: phone}) as adb, android_capability(adb) as capability:
        yield capability


async def act(capability: AndroidCapability, name: str, **params: Any) -> ActionResult:
    return await capability.execute_action(Action(name=f"android.{name}", params=params))


async def test_a_tap_and_a_long_press_go_where_they_are_aimed(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    assert (await act(capability, "click_coordinate", coordinate_x=100, coordinate_y=200)).success
    assert (await act(capability, "long_press", coordinate_x=100, coordinate_y=200)).success
    assert (await act(capability, "drag", start_x=10, start_y=20, end_x=30, end_y=40)).success

    assert phone.inputs == [
        "input tap 100 200",
        "input swipe 100 200 100 200 1000",  # a long press: a swipe that does not move
        "input swipe 10 20 30 40 1000",
    ]


@pytest.mark.parametrize(
    ("direction", "swipe"),
    [
        # down shows what is further down: the finger moves up, the content with it.
        ("down", "input swipe 540 1200 540 200 500"),
        ("up", "input swipe 540 1200 540 2200 500"),
        # right shows what is further right: the finger moves left (held on the screen).
        ("right", "input swipe 540 1200 0 1200 500"),
        ("left", "input swipe 540 1200 1079 1200 500"),
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
    assert (
        await act(capability, "scroll", direction="down", start_x=300, start_y=2000, amount=600)
    ).success

    assert phone.inputs == ["input swipe 300 2000 300 1400 500"]


async def test_typing_at_a_point_taps_clears_and_types(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    capability.actions = [AndroidTypeCoordinateAction(focus_wait_ms=0)]

    assert (
        await act(capability, "type_coordinate", coordinate_x=5, coordinate_y=6, text="hi there")
    ).success
    assert (
        await act(
            capability, "type_coordinate", coordinate_x=5, coordinate_y=6, text="x", clear=False
        )
    ).success

    # No text field has focus on this phone: with a point, nothing checks that
    # one took it (the gap of docs/architecture_design.md §11.2).
    assert phone.inputs == [
        "input tap 5 6",
        "input keycombination KEYCODE_CTRL_LEFT KEYCODE_A && input keyevent KEYCODE_DEL",
        "input text 'hi there'",
        "input tap 5 6",
        "input text x",
    ]


async def test_typing_without_a_point_types_into_the_field_that_has_focus(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    phone.text_field = True

    assert (await act(capability, "type_coordinate", text="hi there")).success
    assert (await act(capability, "type_coordinate", text="x", clear=False)).success

    assert phone.inputs == [
        "input keycombination KEYCODE_CTRL_LEFT KEYCODE_A && input keyevent KEYCODE_DEL",
        "input text 'hi there'",
        "input text x",
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
    phone.text_field, phone.window_over = text_field, window_over

    result = await act(capability, "type_coordinate", text="hi")

    assert (result.success, result.error) == (
        False,
        f"No text field has focus: {where}, so no text was typed.",
    )
    assert phone.inputs == []


async def test_text_the_keyboard_map_lacks_fails_the_action_and_says_why(
    capability: AndroidCapability,
) -> None:
    result = await act(capability, "type_text", text="你好")

    assert not result.success
    assert result.error is not None and "virtual keyboard map" in result.error


async def test_system_keys_press_their_android_key_codes(
    capability: AndroidCapability, phone: ScriptedPhone
) -> None:
    for key in ("back", "home", "enter"):
        assert (await act(capability, "press_key", key=key)).success

    assert phone.inputs == [
        "input keyevent KEYCODE_BACK",
        "input keyevent KEYCODE_HOME",
        "input keyevent KEYCODE_ENTER",
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
