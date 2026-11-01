"""The Android capability over the fake ADB server: setup, its fragments, its captures, its environment.

The observation-cycle contract runs it through
``tests/_helpers/capability_contract.py`` (``HARNESS_FACTORIES``); this module
pins what is the capability's own: which fragments a capture builds and from
which commands, what an action does to the capture before it, setup and
teardown through the backend, and that UI-TARS seats against it with its
wiring unchanged. The phone is ``tests/_helpers/android_device.py``.
"""

from __future__ import annotations

import asyncio
import typing

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import (
    CAPTURE_STATE,
    FOREGROUND_APP,
    PLATFORM,
    SCREENSHOT,
    AndroidCaptureStateObs,
    AndroidForegroundAppObs,
    AndroidPlatformObs,
    AndroidScreenshotObs,
)
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action
from spectragent.schemas.observation.geometry import ScreenshotGeometry
from tests._helpers.android_device import (
    FINGERPRINT,
    INPUT_METHOD,
    SERIAL,
    FakeServerProvider,
    ScriptedPhone,
    android_capability,
    png,
)
from tests._helpers.assembly import AGENT_ID, NoModel, make_env, make_run
from tests._helpers.fake_adb import FakeAdbServer, ShellResult


def _services(adb: FakeAdbServer) -> list[str]:
    return [request.service.removeprefix("shell,v2:") for request in adb.requests]


async def test_setup_brings_the_device_up_through_the_backend_and_teardown_releases_it() -> None:
    async with FakeAdbServer({SERIAL: ScriptedPhone()}) as adb:
        capability = AndroidCapability()
        provider = FakeServerProvider(adb.host, adb.port)
        capability.backend = provider
        await capability.setup()
        assert capability.is_alive and capability.screen_size == (1080, 2400)

        await capability.teardown()
        await capability.teardown()  # a second teardown does nothing

    assert provider.closed == ["fake-session"]
    assert not capability.is_alive


async def test_one_capture_builds_every_fragment_from_one_read_of_each_source() -> None:
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with FakeAdbServer({SERIAL: phone}) as adb, android_capability(adb) as capability:
        before = len(adb.requests)
        await capability.begin_observation(1)
        fragments = {
            key: await capability.resolve_fragment(key)
            for key in (SCREENSHOT, FOREGROUND_APP, PLATFORM, CAPTURE_STATE)
        }

        assert sorted(_services(adb)[before:]) == [
            "dumpsys window",
            "screencap -p",
            "settings get secure default_input_method",
        ]

    screenshot = fragments[SCREENSHOT]
    assert isinstance(screenshot, AndroidScreenshotObs) and isinstance(
        screenshot, ScreenshotGeometry
    )
    assert screenshot.image_bytes == png(1080, 2400)
    assert (screenshot.image_width, screenshot.image_height) == (1080, 2400)
    assert (screenshot.viewport_width, screenshot.viewport_height) == (1080, 2400)
    assert screenshot.producer == "screencap"
    assert fragments[FOREGROUND_APP] == AndroidForegroundAppObs(
        package="com.android.settings",
        activity="com.android.settings.Settings",
        window="com.android.settings/com.android.settings.Settings",
        captured_at=fragments[FOREGROUND_APP].captured_at,
    )
    platform = fragments[PLATFORM]
    assert isinstance(platform, AndroidPlatformObs)
    assert (
        platform.operating_system,
        platform.fingerprint,
        platform.input_method,
        platform.faketouch,
    ) == (
        "android",
        FINGERPRINT,
        INPUT_METHOD,
        True,
    )
    state = fragments[CAPTURE_STATE]
    assert isinstance(state, AndroidCaptureStateObs)
    assert (state.pointer.status, state.failed_visible_resources.status) == (
        "unavailable",
        "unavailable",
    )
    assert state.settle_state is None and state.foreground_change is None


def test_the_platform_names_a_closed_set_of_operating_systems() -> None:
    """``prepare()`` reads which systems a platform fragment may name with ``typing.get_args``."""
    annotation = typing.get_type_hints(AndroidPlatformObs)["operating_system"]

    assert typing.get_args(annotation) == ("android",)


async def test_a_capture_reads_only_what_the_run_negotiated() -> None:
    async with (
        FakeAdbServer({SERIAL: ScriptedPhone()}) as adb,
        android_capability(adb, fragments={SCREENSHOT}) as capability,
    ):
        before = len(adb.requests)
        await capability.begin_observation(1)
        await capability.resolve_fragment(SCREENSHOT)

        assert _services(adb)[before:] == ["screencap -p"]


async def test_an_action_drops_the_capture_it_came_after() -> None:
    async with (
        FakeAdbServer({SERIAL: ScriptedPhone()}) as adb,
        android_capability(adb) as capability,
    ):
        await capability.begin_observation(1)
        first = await capability.resolve_fragment(SCREENSHOT)
        await capability.execute_action(
            Action(name="android.click_coordinate", params={"coordinate_x": 1, "coordinate_y": 2})
        )
        again = await capability.resolve_fragment(SCREENSHOT)

        assert again is not first
        assert _services(adb).count("screencap -p") == 2


async def test_with_the_wait_on_a_capture_waits_for_the_app_to_stop_drawing() -> None:
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with (
        FakeAdbServer({SERIAL: phone}) as adb,
        android_capability(adb, settle_max_wait_s=5, settle_quiet_ms=50) as capability,
    ):
        await capability.begin_observation(1)
        state = await capability.resolve_fragment(CAPTURE_STATE)

    assert isinstance(state, AndroidCaptureStateObs) and state.settle is not None
    assert (state.settle.outcome, state.settle.loaded) == ("settled", None)
    assert state.settle.waited_ms >= 50


async def test_a_poll_of_the_wait_that_gets_no_answer_is_recorded_with_its_limit() -> None:
    phone = ScriptedPhone(focused="com.android.settings/.Settings")

    async def window_hangs(command: str) -> ShellResult:
        if command == "dumpsys window":
            await asyncio.Event().wait()
        return await phone(command)

    async with (
        FakeAdbServer({SERIAL: window_hangs}) as adb,
        android_capability(
            adb,
            fragments={SCREENSHOT, CAPTURE_STATE},
            settle_max_wait_s=5,
            settle_quiet_ms=50,
            query_timeout_s=0.2,
        ) as capability,
    ):
        await capability.begin_observation(1)
        state = await capability.resolve_fragment(CAPTURE_STATE)

    assert isinstance(state, AndroidCaptureStateObs) and state.settle is not None
    assert (state.settle.outcome, state.settle.detail) == (
        "unknown",
        "TimeoutError: no answer within 0.2 s",
    )


def test_a_quiet_period_the_wait_could_never_reach_is_refused() -> None:
    with pytest.raises(
        ValueError, match="settle_quiet_ms .3000. is longer than settle_max_wait_s .2 s."
    ):
        AndroidCapability(settle_quiet_ms=3000, settle_max_wait_s=2)
    AndroidCapability(settle_quiet_ms=3000, settle_max_wait_s=0)  # the wait is off


async def test_a_screenshot_reads_back_from_its_trace_files_as_it_was() -> None:
    async with (
        FakeAdbServer({SERIAL: ScriptedPhone()}) as adb,
        android_capability(adb) as capability,
    ):
        await capability.begin_observation(1)
        screenshot = await capability.resolve_fragment(SCREENSHOT)

    binary = screenshot.trace_binary()
    assert binary is not None and binary[0] == ".png"
    payload = screenshot.model_dump(mode="json", exclude=screenshot.trace_json_exclude())
    assert "image_bytes" not in payload
    assert AndroidScreenshotObs.from_trace(payload, binary[1]) == screenshot


async def test_an_action_before_setup_or_of_another_capability_fails_without_raising() -> None:
    capability = AndroidCapability()

    not_set_up = await capability.execute_action(Action(name="android.wait", params={}))
    unknown = await capability.execute_action(Action(name="android.navigate", params={}))

    assert not not_set_up.success and not_set_up.error == "the Android device is not set up"
    assert not unknown.success and unknown.error == "Unknown action: android.navigate"


async def test_the_android_environment_routes_android_actions_to_the_capability() -> None:
    phone = ScriptedPhone()
    async with FakeAdbServer({SERIAL: phone}) as adb, android_capability(adb) as capability:
        env = make_env(AndroidEnvironment, {"android#0": capability})
        result = await env.act(
            Action(name="android.click_coordinate", params={"coordinate_x": 7, "coordinate_y": 8})
        )

    assert result.success and phone.inputs == ["input tap 7 8"]


def test_ui_tars_assembles_against_the_android_capability_with_its_wiring_unchanged() -> None:
    """A relative name is a param contract (§4.9), so UI-TARS's demands bind to Android's actions."""
    env = make_env(EnvironmentBase, {"android#0": AndroidCapability()})
    run = make_run(
        agent=UITarsAgent(), loop=ReactLoop(), env=env, llm=NoModel(), memory=SimpleMemory()
    )
    runtime = AgentRuntime.for_agent(run, AGENT_ID)

    runtime.prepare(task_id="assembly")

    actions, observation = runtime.negotiated_actions, runtime.negotiated_observation
    assert actions is not None and observation is not None
    # Every action UI-TARS wires but send_keys: Android offers no key chords.
    assert actions.action_bindings == {
        "click_coordinate": "android.click_coordinate",
        "type_coordinate": "android.type_coordinate",
        "scroll": "android.scroll",
        "drag": "android.drag",
        "wait": "android.wait",
        "long_press": "android.long_press",
        "open_app": "android.open_app",
        "press_key": "android.press_key",
        "done": "loop.done",
    }
    fragments = observation.fragment_bindings
    assert {name: fragments[name] for name in ("screenshot", "capture_state", "platform")} == {
        "screenshot": "android.screenshot",
        "capture_state": "android.capture_state",
        "platform": "android.platform",
    }
    scroll = next(d for d in runtime.action_space.descriptors if d.name == "android.scroll")
    assert "index" not in scroll.params, "no android.ui_tree in this run, so no element to name"
    assert runtime.seed_messages("open the settings app")
