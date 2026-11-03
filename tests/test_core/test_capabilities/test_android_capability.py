"""The Android capability over the fake ADB server: setup, its fragments, its captures, its environment.

The observation-cycle contract runs it through
``tests/_helpers/capability_contract.py`` (``HARNESS_FACTORIES``); this module
pins what is the capability's own: the device side it starts and stops, which
fragments a capture builds and from which sources, what an action does to the
capture before it, setup and teardown through the backend, the service kept
bound, and that UI-TARS seats against it. The phone, adb's side and the device
side both, is ``tests/_helpers/android_device.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import JsonValue, ValidationError

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.android.actions import AndroidOpenAppAction
from spectragent.core.capabilities.android.backends.base import (
    AndroidProviderSession,
    DeviceNeeds,
    UnmetDeviceNeeds,
)
from spectragent.core.capabilities.android.backends.local_emulator import LocalEmulatorProvider
from spectragent.core.capabilities.android.benchmark_setup import OracleTask
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.device.apk import (
    TEST_KEY_CERTIFICATE_SHA256,
    ServiceApkError,
)
from spectragent.core.capabilities.android.device.channel import (
    PROTOCOL_VERSION,
    JsonObject,
    RemoteError,
)
from spectragent.core.capabilities.android.device.lifecycle import DeviceSideError
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.input._registrar import TypingBackendUnsupported
from spectragent.core.capabilities.android.input.tap import AccessibilityGestureInjector
from spectragent.core.capabilities.android.input.text import CommitTextBackend, KeyEventsBackend
from spectragent.core.capabilities.android.observations import (
    CAPTURE_STATE,
    FOREGROUND_APP,
    PLATFORM,
    SCREENSHOT,
    UI_TREE,
    AndroidCaptureStateObs,
    AndroidForegroundAppObs,
    AndroidPlatformObs,
    AndroidScreenshotObs,
    AndroidUITreeObs,
)
from spectragent.core.capabilities.android.session import CaptureError
from spectragent.core.capabilities.android.settle import ScreenPoll
from spectragent.core.capabilities.android.ui_tree.cleaning import AndroidWorldCleaner
from spectragent.core.capabilities.android.ui_tree.serializers import M3ASerializer, T3ASerializer
from spectragent.core.capabilities.android.ui_tree.source import AccessibilityServiceTreeSource
from spectragent.core.capabilities.base import EpisodeEnd, SetupHost, TaskTextFromSetup
from spectragent.core.composer import ConfigComposer
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action
from spectragent.schemas.observation.geometry import ScreenshotGeometry
from spectragent.schemas.task import SetupTaskText
from spectragent.schemas.verdict import Judgment, VerifierIdentity
from tests._helpers.android_device import (
    APK_BYTES,
    FINGERPRINT,
    FOREST,
    INPUT_METHOD,
    SERIAL,
    FakeServerProvider,
    ScriptedPhone,
    android_capability,
    configured,
    fake_service_apk,
    phone_server,
    png,
    settings_forest,
    unset_capability,
)
from tests._helpers.assembly import AGENT_ID, NoModel, make_env, make_run
from tests._helpers.fake_adb import FakeAdbServer, ShellResult
from tests._helpers.fake_device_side import Answer

SERVICE = "org.spectragent.device/.DeviceService"


def _services(adb: FakeAdbServer) -> list[str]:
    return [request.service.removeprefix("shell,v2:") for request in adb.requests]


async def test_setup_starts_the_device_side_and_teardown_stops_it_and_releases_the_device(
    tmp_path: Path,
) -> None:
    phone = ScriptedPhone()
    async with phone_server(phone) as adb:
        capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
        provider = configured(capability, adb)
        await capability.setup()
        assert capability.is_alive and capability.screen_size == (1080, 2400)
        assert phone.side.installed == APK_BYTES and phone.side.bound
        assert phone.side.enabled == SERVICE and phone.side.helper is not None

        await capability.teardown()
        await capability.teardown()  # a second teardown does nothing
        async with asyncio.timeout(5):  # a helper that never exits fails, not hangs
            while phone.side.helper_exits == 0:  # it exits once its stdin ended
                await asyncio.sleep(0.01)

    assert provider.closed == ["fake-session"]
    assert not capability.is_alive
    assert phone.side.enabled is None and not phone.side.bound, "the setting is as it was"
    assert not any("force-stop" in service for service in _services(adb))


async def test_one_capture_builds_every_fragment_from_one_read_of_each_source() -> None:
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with phone_server(phone) as adb, android_capability(adb) as capability:
        before, shots = len(adb.requests), phone.helper_calls("screenshot")
        await capability.begin_observation(1)
        fragments = {
            key: await capability.resolve_fragment(key)
            for key in (SCREENSHOT, FOREGROUND_APP, PLATFORM, CAPTURE_STATE)
        }

        assert sorted(_services(adb)[before:]) == [
            "dumpsys window",
            "settings get secure default_input_method",
        ]
        assert phone.helper_calls("screenshot") == shots + 1

    screenshot = fragments[SCREENSHOT]
    assert isinstance(screenshot, AndroidScreenshotObs) and isinstance(
        screenshot, ScreenshotGeometry
    )
    assert screenshot.image_bytes == png(1080, 2400)
    assert (screenshot.image_width, screenshot.image_height) == (1080, 2400)
    assert (screenshot.viewport_width, screenshot.viewport_height) == (1080, 2400)
    assert (screenshot.producer, screenshot.capture_ms, screenshot.encode_ms) == (
        "shell_helper",
        17,
        85,
    )
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
        platform.sdk_int,
        platform.input_method,
        platform.faketouch,
        platform.gestures,
    ) == ("android", FINGERPRINT, 33, INPUT_METHOD, True, True)
    assert (
        platform.service_apk_sha256,
        platform.service_apk_version,
        platform.accessibility_tool,
        platform.service_apk_signing_certificate_sha256,
    ) == (hashlib.sha256(APK_BYTES).hexdigest(), "1 (1)", False, TEST_KEY_CERTIFICATE_SHA256)
    state = fragments[CAPTURE_STATE]
    assert isinstance(state, AndroidCaptureStateObs)
    assert (state.pointer.status, state.failed_visible_resources.status) == (
        "unavailable",
        "unavailable",
    )
    assert state.settle_state is None and state.foreground_change is None


async def test_one_capture_reads_the_tree_once_and_cleans_and_prints_it() -> None:
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with phone_server(phone) as adb, android_capability(adb) as capability:
        await capability.begin_observation(1)
        tree = await capability.resolve_fragment(UI_TREE)
        await capability.resolve_fragment(SCREENSHOT)
        reads = list(phone.tree_reads)

    assert reads == [5000], "one read per capture, past which off-screen nodes are left out"
    assert isinstance(tree, AndroidUITreeObs)
    assert (tree.producer, tree.node_count, tree.omitted_offscreen, tree.capture_ms) == (
        "accessibility_service",
        3,
        0,
        12,
    )
    assert [(e.index, e.unique_id) for e in tree.elements] == [(0, 1), (1, 2)]
    assert [node.unique_id for node in tree.forest.windows[0].tree.nodes] == [1, 2]
    assert tree.serialized == M3ASerializer().serialize(tree.tree())
    assert tree.serialized.startswith('UI element 0: {"index": 0, "text": "Network & internet"')


async def test_a_run_whose_agent_reads_no_tree_never_asks_for_one() -> None:
    phone = ScriptedPhone()
    async with (
        phone_server(phone) as adb,
        android_capability(adb, fragments={SCREENSHOT, PLATFORM, CAPTURE_STATE}) as capability,
    ):
        await capability.begin_observation(1)
        await capability.resolve_fragment(SCREENSHOT)

    assert phone.service_calls("tree") == 0


async def test_the_tree_source_and_serializer_a_run_configures_are_the_ones_used() -> None:
    phone = ScriptedPhone()
    async with phone_server(phone) as adb, android_capability(adb) as capability:
        capability.ui_tree_source = AccessibilityServiceTreeSource(max_nodes=300)
        capability.ui_tree_serializer = T3ASerializer()
        await capability.begin_observation(1)
        tree = await capability.resolve_fragment(UI_TREE)

    assert phone.tree_reads == [300]
    assert isinstance(tree, AndroidUITreeObs) and tree.serialized.startswith(
        "UI element 0: UIElement(text='Network & internet'"
    )
    assert capability.fragment_formats() == {UI_TREE: T3ASerializer().describe_format()}


async def test_the_tree_has_a_time_limit_of_its_own() -> None:
    """The first walk of a long web page took 8.4 s: past query_timeout_s, the capture holds."""
    phone = ScriptedPhone(tree_delay_s=0.4)
    async with (
        phone_server(phone) as adb,
        android_capability(adb, fragments={UI_TREE}, query_timeout_s=0.2) as capability,
    ):
        await capability.begin_observation(1)
        tree = await capability.resolve_fragment(UI_TREE)
        capability.ui_tree_timeout_s = 0.2
        await capability.begin_observation(2)
        with pytest.raises(CaptureError, match=r"android\.ui_tree: .*no answer within 0\.2 s"):
            await capability.resolve_fragment(UI_TREE)

    assert isinstance(tree, AndroidUITreeObs) and len(tree.elements) == 2


async def test_the_tree_is_cleaned_against_the_screen_its_capture_measured() -> None:
    """Turned sideways after setup, the screenshot is 2400 wide; an element right of 1080 is on it."""
    phone = ScriptedPhone()
    phone.forest = settings_forest(("Beside the old edge", (1200, 300, 1500, 450)))
    async with phone_server(phone) as adb, android_capability(adb) as capability:
        phone.width, phone.height = 2400, 1080
        await capability.begin_observation(1)
        tree = await capability.resolve_fragment(UI_TREE)
        await capability.resolve_fragment(SCREENSHOT)

    assert isinstance(tree, AndroidUITreeObs) and len(tree.elements) == 1
    assert capability.screen_size == (2400, 1080)


def test_the_ui_tree_format_is_described_by_the_configured_serializer(tmp_path: Path) -> None:
    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    assert capability.fragment_formats() == {}, "no serializer configured: nothing described"

    capability.ui_tree_serializer = M3ASerializer()

    assert capability.fragment_formats() == {UI_TREE: M3ASerializer().describe_format()}


def test_the_platform_names_a_closed_set_of_operating_systems() -> None:
    """``prepare()`` reads which systems a platform fragment may name with ``typing.get_args``."""
    annotation = typing.get_type_hints(AndroidPlatformObs)["operating_system"]

    assert typing.get_args(annotation) == ("android",)


async def test_a_capture_reads_only_what_the_run_negotiated() -> None:
    phone = ScriptedPhone()
    async with (
        phone_server(phone) as adb,
        android_capability(adb, fragments={SCREENSHOT}) as capability,
    ):
        before, shots = len(adb.requests), phone.helper_calls("screenshot")
        await capability.begin_observation(1)
        await capability.resolve_fragment(SCREENSHOT)

        assert _services(adb)[before:] == []
        assert phone.helper_calls("screenshot") == shots + 1


async def test_the_service_takes_the_screenshots_where_the_helper_cannot() -> None:
    """The helper's screenshot is written for API 31 to 33; elsewhere it answers unsupported."""
    phone = ScriptedPhone(helper_screenshots=False)
    async with (
        phone_server(phone) as adb,
        android_capability(adb, fragments={SCREENSHOT}) as capability,
    ):
        await capability.begin_observation(1)
        screenshot = await capability.resolve_fragment(SCREENSHOT)

    assert isinstance(screenshot, AndroidScreenshotObs)
    assert (screenshot.producer, screenshot.waited_ms) == ("accessibility_service", 40)
    assert phone.service_calls("screenshot") == 1


async def test_a_helper_screenshot_that_fails_at_setup_fails_setup(tmp_path: Path) -> None:
    """Only the helper's ``unsupported`` moves screenshots to the service; a failure is the device's."""
    phone = ScriptedPhone(screenshot_fails=True)
    async with phone_server(phone) as adb:
        capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
        provider = configured(capability, adb)
        with pytest.raises(RemoteError, match="screenshot_failed"):
            await capability.setup()

    assert provider.closed == ["fake-session"] and not capability.is_alive
    assert phone.service_calls("screenshot") == 0, "the service was never asked instead"


async def test_an_action_drops_the_capture_it_came_after() -> None:
    phone = ScriptedPhone()
    async with phone_server(phone) as adb, android_capability(adb) as capability:
        shots = phone.helper_calls("screenshot")
        await capability.begin_observation(1)
        first = await capability.resolve_fragment(SCREENSHOT)
        await capability.execute_action(
            Action(name="android.click_coordinate", params={"coordinate_x": 1, "coordinate_y": 2})
        )
        again = await capability.resolve_fragment(SCREENSHOT)

        assert again is not first
        assert phone.helper_calls("screenshot") == shots + 2


async def test_with_the_wait_on_a_capture_waits_for_the_screen_to_stop_changing() -> None:
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with (
        phone_server(phone) as adb,
        android_capability(adb, settle_max_wait_s=5, settle_quiet_ms=50) as capability,
    ):
        await capability.begin_observation(1)
        state = await capability.resolve_fragment(CAPTURE_STATE)

    assert isinstance(state, AndroidCaptureStateObs) and state.settle is not None
    assert (state.settle.outcome, state.settle.loaded) == ("settled", None)
    assert state.settle.waited_ms >= 50


async def test_the_wait_counts_the_services_events_by_their_time_on_the_device() -> None:
    """Events from long before the capture do not hold it; an event during it does."""
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with (
        phone_server(phone) as adb,
        android_capability(adb, settle_max_wait_s=0.6, settle_quiet_ms=300) as capability,
    ):
        for _ in range(50):  # while the model was thinking
            phone.emit()
        await asyncio.sleep(0.4)
        await capability.begin_observation(1)
        old_events = await capability.resolve_fragment(CAPTURE_STATE)

        async def keeps_changing() -> None:
            while True:
                phone.emit()
                await asyncio.sleep(0.05)

        changing = asyncio.ensure_future(keeps_changing())
        try:
            await capability.begin_observation(2)
            new_events = await capability.resolve_fragment(CAPTURE_STATE)
        finally:
            changing.cancel()

    assert isinstance(old_events, AndroidCaptureStateObs) and old_events.settle is not None
    assert old_events.settle.outcome == "settled"
    assert isinstance(new_events, AndroidCaptureStateObs) and new_events.settle is not None
    assert (new_events.settle.outcome, new_events.settle.pending) == ("capped", ("events",))


async def test_a_poll_of_the_wait_that_gets_no_answer_is_recorded_with_its_limit() -> None:
    phone = ScriptedPhone(focused="com.android.settings/.Settings")

    async def window_hangs(command: str) -> ShellResult:
        if command == "dumpsys window":
            await asyncio.Event().wait()
        return await phone(command)

    async with (
        FakeAdbServer(
            {SERIAL: window_hangs},
            streams={SERIAL: phone.side.streams},
            sockets={SERIAL: phone.side.sockets},
        ) as adb,
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
        AndroidCapability(service_apk="a.apk", settle_quiet_ms=3000, settle_max_wait_s=2)
    AndroidCapability(service_apk="a.apk", settle_quiet_ms=3000, settle_max_wait_s=0)  # wait off


#: The system's "Open with" chooser in front: its package, ``android``, has no dot.
CHOOSER = "android/com.android.internal.app.ResolverActivity"


async def test_with_the_open_with_chooser_in_front_every_capture_still_waits() -> None:
    """Its frame count once raised before the poll noted the app in front: every later poll
    failed, and every later capture of the run came 0.07-0.5 s after its action (BrowserDraw and
    BrowserMaze in AndroidWorld's seed-42 sweep).

    The device reports no frame count for ``android`` (``No process found for: android`` on the
    emulator), so a capture with the chooser in front waits to the limit; the next one settles.
    """
    phone = ScriptedPhone(focused="com.google.android.documentsui/.files.FilesActivity")
    settles = []
    async with (
        phone_server(phone) as adb,
        android_capability(
            adb, fragments={SCREENSHOT}, settle_max_wait_s=0.5, settle_quiet_ms=50
        ) as capability,
    ):
        for cycle, (focused, frames) in enumerate(
            (
                (phone.focused, 36),
                (CHOOSER, None),
                ("com.android.chrome/com.google.android.apps.chrome.Main", 36),
            ),
            start=1,
        ):
            phone.focused, phone.frames = focused, frames
            await capability.begin_observation(cycle)
            screenshot = await capability.resolve_fragment(SCREENSHOT)
            assert isinstance(screenshot, AndroidScreenshotObs)
            settles.append(screenshot.settle)

    files, chooser, chrome = settles
    assert files is not None and files.outcome == "settled"
    assert chooser is not None and (chooser.outcome, chooser.pending) == ("capped", ("frames",))
    assert chrome is not None and chrome.outcome == "settled"
    assert "dumpsys gfxinfo android" in _services(adb)


async def test_a_frame_count_that_fails_is_no_count_and_the_wait_goes_on() -> None:
    """No count is "not drawn yet" (``settle.py``): the wait lasts to its limit, every time."""
    phone = ScriptedPhone(focused="com.android.settings/.Settings")

    async def counts_fail(command: str) -> ShellResult:
        if command.startswith("dumpsys gfxinfo "):
            return ShellResult(stderr=b"Can't find service: gfxinfo\n", exit_code=1)
        return await phone(command)

    settles = []
    async with (
        FakeAdbServer(
            {SERIAL: counts_fail},
            streams={SERIAL: phone.side.streams},
            sockets={SERIAL: phone.side.sockets},
        ) as adb,
        android_capability(
            adb, fragments={SCREENSHOT}, settle_max_wait_s=0.3, settle_quiet_ms=50
        ) as capability,
    ):
        for cycle in (1, 2):
            await capability.begin_observation(cycle)
            screenshot = await capability.resolve_fragment(SCREENSHOT)
            assert isinstance(screenshot, AndroidScreenshotObs)
            settles.append(screenshot.settle)

    for settle in settles:
        assert settle is not None and (settle.outcome, settle.pending) == ("capped", ("frames",))
        assert settle.waited_ms >= 300


@pytest.mark.parametrize(
    ("action", "succeeds"),
    [
        (
            Action(name="android.click_coordinate", params={"coordinate_x": 1, "coordinate_y": 2}),
            True,
        ),
        (Action(name="android.open_app", params={"app_name": "nothing"}), False),
        (Action(name="android.wait", params={"seconds": 0}), True),
    ],
    ids=["a tap", "a failed action", "a wait"],
)
async def test_after_an_action_the_screen_is_not_still_before_the_minimum(
    action: Action, succeeds: bool
) -> None:
    """A run that runs M3A as AndroidWorld does waits at least its 2 s after every action.

    An action that failed counts, since it may have touched the screen before it failed; so
    does a wait, which touches nothing.
    """
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with (
        phone_server(phone) as adb,
        android_capability(
            adb,
            fragments={SCREENSHOT},
            settle_max_wait_s=3,
            settle_quiet_ms=50,
            settle_min_after_action_ms=1500,
        ) as capability,
    ):
        clock = asyncio.get_running_loop().time
        await capability.begin_observation(1)
        first = await capability.resolve_fragment(SCREENSHOT)
        result = await capability.execute_action(action)
        acted = clock()
        await capability.begin_observation(2)
        after = await capability.resolve_fragment(SCREENSHOT)
        captured = clock()

    assert result.success is succeeds, result.error
    assert isinstance(first, AndroidScreenshotObs) and first.settle is not None
    assert first.settle.outcome == "settled"
    assert first.settle.waited_ms < 1500, "no action came before the first capture"
    assert isinstance(after, AndroidScreenshotObs) and after.settle is not None
    assert after.settle.outcome == "settled"
    assert captured - acted >= 1.49


def test_a_minimum_after_an_action_the_wait_could_never_reach_is_refused() -> None:
    with pytest.raises(
        ValueError,
        match=r"settle_min_after_action_ms \(3000\) is longer than settle_max_wait_s \(2 s\)",
    ):
        AndroidCapability(service_apk="a.apk", settle_min_after_action_ms=3000, settle_max_wait_s=2)
    with pytest.raises(ValueError, match=r"settle_max_wait_s \(0 s\).*0 turns the wait off"):
        AndroidCapability(service_apk="a.apk", settle_min_after_action_ms=2000, settle_max_wait_s=0)
    AndroidCapability(service_apk="a.apk", settle_min_after_action_ms=2000, settle_max_wait_s=2)


@dataclass
class _TreesPhone(ScriptedPhone):
    """A phone whose service answers each ``tree`` with the next of *trees*, then the last for ever."""

    trees: list[JsonObject] = field(default_factory=list)

    async def _tree(self, params: JsonObject) -> Answer:
        self.forest = self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]
        return await super()._tree(params)


#: What the service reported right after a tap opened a page: the status bar alone.
_STATUS_BAR_ALONE: JsonObject = {
    "windows": [
        {
            "bounds_in_screen": {"right": 1080, "bottom": 128},
            "id": 10,
            "window_type": "TYPE_SYSTEM",
            "tree": {
                "nodes": [
                    {
                        "bounds_in_screen": {"left": 50, "right": 148, "bottom": 128},
                        "class_name": "android.widget.TextView",
                        "package_name": "com.android.systemui",
                        "text": "12:09",
                        "window_id": 10,
                        "is_visible_to_user": True,
                        "is_important": True,
                    }
                ]
            },
        }
    ]
}
#: An app's window before its content came: the root alone.
_EMPTY_APP_WINDOW: JsonObject = {
    "windows": [
        {
            "bounds_in_screen": {"right": 1080, "bottom": 2400},
            "id": 16,
            "title": "Joplin",
            "window_type": "TYPE_APPLICATION",
            "tree": {
                "nodes": [
                    {
                        "bounds_in_screen": {"right": 1080, "bottom": 2400},
                        "class_name": "android.widget.FrameLayout",
                        "package_name": "net.cozic.joplin",
                        "window_id": 16,
                        "is_visible_to_user": True,
                        "is_important": True,
                    }
                ]
            },
        }
    ]
}


async def test_a_capture_whose_tree_shows_no_app_waits_again_and_is_taken_again() -> None:
    """The agent saw the status bar alone 2.3 s after a tap that opened a page (SystemWifiTurnOn
    in AndroidWorld's seed-42 sweep): the screen had not finished changing."""
    phone = _TreesPhone(focused="com.android.settings/.Settings", trees=[_STATUS_BAR_ALONE, FOREST])
    async with (
        phone_server(phone) as adb,
        android_capability(
            adb, fragments={SCREENSHOT, UI_TREE}, settle_max_wait_s=3, settle_quiet_ms=50
        ) as capability,
    ):
        shots = phone.helper_calls("screenshot")
        await capability.begin_observation(1)
        tree = await capability.resolve_fragment(UI_TREE)
        screenshot = await capability.resolve_fragment(SCREENSHOT)
        taken = phone.helper_calls("screenshot") - shots

    assert isinstance(tree, AndroidUITreeObs) and "Network & internet" in tree.serialized
    assert (len(phone.tree_reads), taken) == (2, 2), "the whole capture was taken again"
    assert tree.settle is not None and tree.settle.outcome == "settled"
    assert isinstance(screenshot, AndroidScreenshotObs) and screenshot.settle == tree.settle


async def test_a_tree_that_never_shows_an_app_ends_the_wait_at_its_limit_and_says_so() -> None:
    phone = _TreesPhone(focused="net.cozic.joplin/.MainActivity", trees=[_EMPTY_APP_WINDOW])
    async with (
        phone_server(phone) as adb,
        android_capability(
            adb, fragments={UI_TREE}, settle_max_wait_s=0.6, settle_quiet_ms=50
        ) as capability,
    ):
        await capability.begin_observation(1)
        tree = await capability.resolve_fragment(UI_TREE)

    assert isinstance(tree, AndroidUITreeObs) and tree.settle is not None
    assert tree.settle.outcome == "capped" and tree.settle.pending[-1] == "app_window"
    assert len(phone.tree_reads) >= 2, "it waited again and looked again"
    assert tree.settle.waited_ms >= 400, "each wait again counts from the first, within one limit"


async def test_every_fragment_of_a_capture_records_its_wait_without_capture_state() -> None:
    """Recorded in the trace and never shown to the agent: the browser's ``settle`` is the same."""
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with (
        phone_server(phone) as adb,
        android_capability(
            adb,
            fragments={SCREENSHOT, UI_TREE, FOREGROUND_APP},
            settle_max_wait_s=2,
            settle_quiet_ms=50,
        ) as capability,
    ):
        await capability.begin_observation(1)
        fragments = [
            await capability.resolve_fragment(key) for key in (SCREENSHOT, UI_TREE, FOREGROUND_APP)
        ]
    async with phone_server(ScriptedPhone()) as adb, android_capability(adb) as capability:
        await capability.begin_observation(1)
        unwaited = await capability.resolve_fragment(SCREENSHOT)

    screenshot = fragments[0]
    assert isinstance(screenshot, AndroidScreenshotObs) and screenshot.settle is not None
    assert screenshot.settle.outcome == "settled"
    assert [getattr(fragment, "settle", None) for fragment in fragments] == [screenshot.settle] * 3
    binary = screenshot.trace_binary()
    assert binary is not None
    payload = screenshot.model_dump(mode="json", exclude=screenshot.trace_json_exclude())
    assert payload["settle"]["outcome"] == "settled"
    assert AndroidScreenshotObs.from_trace(payload, binary[1]) == screenshot
    assert isinstance(unwaited, AndroidScreenshotObs) and unwaited.settle is None
    assert "settle" not in unwaited.model_dump(exclude=unwaited.trace_json_exclude()), (
        "a capture without a wait writes what it always wrote"
    )


async def test_a_screenshot_reads_back_from_its_trace_files_as_it_was() -> None:
    async with (
        phone_server(ScriptedPhone()) as adb,
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
    capability = unset_capability()

    not_set_up = await capability.execute_action(Action(name="android.wait", params={}))
    unknown = await capability.execute_action(Action(name="android.navigate", params={}))

    assert not not_set_up.success and not_set_up.error == "the Android device is not set up"
    assert not unknown.success and unknown.error == "Unknown action: android.navigate"


async def test_the_android_environment_routes_android_actions_to_the_capability() -> None:
    phone = ScriptedPhone()
    async with phone_server(phone) as adb, android_capability(adb) as capability:
        phone.inputs.clear()  # setup's test tap
        env = make_env(AndroidEnvironment, {"android#0": capability})
        result = await env.act(
            Action(name="android.click_coordinate", params={"coordinate_x": 7, "coordinate_y": 8})
        )

    assert result.success and phone.inputs == ["helper stroke (7,8)@0 (7,8)@50"]


def test_ui_tars_assembles_against_the_android_capability(tmp_path: Path) -> None:
    """A relative name is a param contract (§4.9), so UI-TARS's demands bind to Android's actions."""
    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    env = make_env(EnvironmentBase, {"android#0": capability})
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


# ── the benchmark's setup component ────────────────────────────────


@dataclass
class _RecordingSetup:
    """A benchmark setup component that records what the capability asks of it, in order."""

    needs: DeviceNeeds = field(default_factory=lambda: DeviceNeeds(root=True, guest_wifi=True))
    fail_start: bool = False
    #: The goal it says it made on the device; ``None``: the task's goal is the listed one.
    goal: str | None = None
    #: Whether it answers whether the task's episode is over, and its answer.
    answers: bool = False
    over: bool = False
    calls: list[str] = field(default_factory=list)
    tasks: list[OracleTask | None] = field(default_factory=list)
    #: The event loop's time when the episode question reached it.
    asked_at: float | None = None

    @property
    def setup_id(self) -> str:
        return "recording"

    @property
    def capability_id(self) -> str:
        return "android"

    @property
    def device_needs(self) -> DeviceNeeds:
        return self.needs

    def check_setup(self, task: OracleTask | None) -> None:
        self.calls.append("check_setup")
        if task == {"refuse": True}:
            raise ValueError("refused by the setup")

    async def start(self, device: AndroidProviderSession, task: OracleTask | None) -> None:
        self.calls.append(f"start {device.serial}")
        self.tasks.append(task)
        if self.fail_start:
            raise RuntimeError("the setup could not start")

    def makes_goal(self, task: OracleTask | None) -> bool:
        self.calls.append("makes_goal")
        self.tasks.append(task)
        return self.goal is not None

    def made_goal(self) -> str | None:
        self.calls.append("made_goal")
        return self.goal

    def answers_episode_over(self) -> bool:
        self.calls.append("answers_episode_over")
        return self.answers

    async def episode_over(self) -> bool:
        self.calls.append("episode_over")
        self.asked_at = asyncio.get_running_loop().time()
        return self.over

    async def stop(self) -> None:
        self.calls.append("stop")


@dataclass
class _ScoringSetup(_RecordingSetup):
    """A setup component that also scores, as AndroidWorld's does until its live verifier lands."""

    def oracle_identity(self) -> VerifierIdentity | None:
        return VerifierIdentity(verifier_id="recording")

    async def read_score(self, *, answer: str | None) -> Judgment:
        self.calls.append(f"read_score {answer}")
        return Judgment(score=0.5)


@dataclass
class _ClosingProvider(FakeServerProvider):
    """Records, in the setup component's own log, when the device is released."""

    log: list[str] = field(default_factory=list)

    async def close(self, session: AndroidProviderSession | None = None) -> None:
        self.log.append("provider closed")
        await super().close(session)


def test_the_capability_hosts_a_setup_component_and_refuses_another_kind() -> None:
    capability = unset_capability()
    assert isinstance(capability, SetupHost)

    capability.bind_setup(_RecordingSetup())
    capability.bind_setup(None)
    with pytest.raises(TypeError, match="no setup component the android capability can host"):
        capability.bind_setup(SimpleNamespace(setup_id="other", capability_id="android"))  # type: ignore[arg-type]


async def test_the_setup_shapes_the_boot_and_is_stopped_before_the_device_goes(
    tmp_path: Path,
) -> None:
    task: dict[str, JsonValue] = {"opaque": ["to", "the", "capability"]}
    async with phone_server(ScriptedPhone()) as adb:
        setup = _ScoringSetup()
        capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
        configured(capability, adb)
        capability.bind_setup(setup)
        provider = _ClosingProvider(adb.host, adb.port, log=setup.calls)
        capability.backend = provider
        await capability.setup(oracle_task=task)
        identity = capability.oracle_identity()
        reading = await capability.read_score(answer="Mall")
        await capability.teardown()

    assert provider.needs == [DeviceNeeds(root=True, guest_wifi=True)]
    assert setup.tasks == [task] and setup.tasks[0] is task, "handed over as it came"
    assert identity == VerifierIdentity(verifier_id="recording")
    assert reading == Judgment(score=0.5)
    assert setup.calls == [
        "check_setup",
        f"start {SERIAL}",
        "read_score Mall",  # the run's answer, handed over as it came
        "stop",
        "provider closed",
    ]


async def test_without_a_setup_the_device_boots_with_no_needs_and_nothing_judges(
    tmp_path: Path,
) -> None:
    async with phone_server(ScriptedPhone()) as adb:
        capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
        configured(capability, adb)
        provider = FakeServerProvider(adb.host, adb.port)
        capability.backend = provider
        await capability.setup()
        try:
            assert capability.oracle_identity() is None, "the environment asks nothing"
        finally:
            await capability.teardown()

    assert provider.needs == [DeviceNeeds()]


async def test_a_setup_that_judges_nothing_leaves_no_verdict() -> None:
    capability = unset_capability()
    capability.bind_setup(_RecordingSetup())
    env = make_env(AndroidEnvironment, {"android": capability})

    assert capability.oracle_identity() is None
    assert await env.verify_live(timeout_s=1.0, answer=None) == []
    with pytest.raises(RuntimeError, match="no bound setup component judges this run"):
        await capability.read_score(answer=None)


async def test_a_setup_that_fails_to_start_is_stopped_and_the_device_released(
    tmp_path: Path,
) -> None:
    async with phone_server(ScriptedPhone()) as adb:
        setup = _RecordingSetup(fail_start=True)
        capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
        configured(capability, adb)
        capability.bind_setup(setup)
        capability.backend = _ClosingProvider(adb.host, adb.port, log=setup.calls)
        with pytest.raises(RuntimeError, match="the setup could not start"):
            await capability.setup(oracle_task={"a": 1})

    assert setup.calls[-2:] == ["stop", "provider closed"]
    assert not capability.is_alive


async def test_a_setup_that_refuses_the_task_stops_setup_before_any_boot(
    tmp_path: Path,
) -> None:
    setup = _RecordingSetup()
    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    capability.bind_setup(setup)
    capability.tap_injector = AccessibilityGestureInjector()
    capability.typing_backend = CommitTextBackend()
    capability.ui_tree_cleaner = AndroidWorldCleaner()
    capability.ui_tree_serializer = M3ASerializer()
    provider = FakeServerProvider("127.0.0.1", 1)
    capability.backend = provider

    with pytest.raises(ValueError, match="refused by the setup"):
        await capability.setup(oracle_task={"refuse": True})
    assert provider.needs == [], "the device was never asked for"


def test_prepare_routes_the_setup_and_the_task_to_it_and_refuses_what_it_refuses(
    tmp_path: Path,
) -> None:
    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    env = make_env(AndroidEnvironment, {"android#0": capability})

    env.check_setup({})  # no task, no setup: nothing to refuse
    with pytest.raises(ValueError, match="has no setup component for the android capability"):
        env.check_setup({"oracle_task": {"family": "android"}})

    setup = _RecordingSetup()
    with pytest.raises(ValueError, match="refused by the setup"):
        env.check_setup({"oracle_task": {"refuse": True}}, setups=[setup])
    assert setup.calls == ["check_setup"], "only a check: nothing started"
    env.check_setup({"oracle_task": {"a": 1}}, setups=[setup])
    assert capability._setup is None, "checking binds nothing"

    env.bind_setups([setup])
    assert capability._setup is setup


def test_a_setup_hosted_by_a_capability_the_environment_lacks_is_refused(
    tmp_path: Path,
) -> None:
    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    env = make_env(AndroidEnvironment, {"android#0": capability})
    elsewhere = SimpleNamespace(setup_id="kernelbench", capability_id="docker")

    with pytest.raises(ValueError, match="hosted by the 'docker' capability, and this environment"):
        env.check_setup({}, setups=[elsewhere])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="both name the 'android' capability"):
        env.bind_setups([_RecordingSetup(), _RecordingSetup()])
    assert capability._setup is None, "nothing bound"


async def test_the_setup_completes_the_task_text(tmp_path: Path) -> None:
    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    assert isinstance(capability, TaskTextFromSetup) and isinstance(capability, EpisodeEnd)
    env = make_env(AndroidEnvironment, {"android#0": capability})

    # No setup: no goal is made on the device, and no episode ends: nothing to wait for.
    assert env.task_text_setter({}) is None
    assert await env.task_text_from_setup() is None
    assert await env.episode_over() is False

    goal = "Follow the instructions shown on the top of the screen: Click on the Submit button."
    setup = _RecordingSetup(goal=goal)
    capability.bind_setup(setup)
    task: dict[str, JsonValue] = {"opaque": ["to", "the", "capability"]}

    assert env.task_text_setter({"oracle_task": task}) == "android"
    assert setup.tasks == [task] and setup.tasks[0] is task, "handed over as it came"
    assert await env.task_text_from_setup() == SetupTaskText(capability_id="android", text=goal)
    assert setup.calls == ["makes_goal", "made_goal"]


def _polls_counted(capability: AndroidCapability, log: list[str]) -> None:
    """Note each poll of the wait for a still screen in *log*."""
    poll = capability._poll_screen

    async def counted() -> ScreenPoll:
        log.append("poll")
        return await poll()

    capability._poll_screen = counted  # type: ignore[method-assign]


async def test_the_episode_question_is_asked_only_once_the_screen_is_still() -> None:
    """Asked at once, a MiniWoB++ page may not have stored its reward yet (§11.2)."""
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with (
        phone_server(phone) as adb,
        android_capability(adb, settle_max_wait_s=5, settle_quiet_ms=100) as capability,
    ):
        setup = _RecordingSetup(answers=True, over=True)
        capability.bind_setup(setup)
        _polls_counted(capability, setup.calls)
        clock = asyncio.get_running_loop().time
        emitted: list[float] = []

        async def the_page_reacts() -> None:  # to the step's last action
            for _ in range(5):
                phone.emit()
                emitted.append(clock())
                await asyncio.sleep(0.05)

        reacting = asyncio.ensure_future(the_page_reacts())
        try:
            assert await capability.episode_over() is True
        finally:
            await reacting

    asked = setup.calls[: setup.calls.index("stop")]
    assert asked[0] == "answers_episode_over" and asked[-1] == "episode_over"
    assert set(asked[1:-1]) == {"poll"} and len(asked) > 3, "the wait polled, then asked"
    asked_at = setup.asked_at
    assert asked_at is not None
    last = max(at for at in emitted if at <= asked_at)
    assert asked_at - last >= 0.09, "a quiet period after the page's last event before it"


async def test_a_task_whose_setup_does_not_answer_the_question_makes_no_wait() -> None:
    """AndroidWorld answers it for MiniWoB++ alone; every other task pays nothing."""
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with (
        phone_server(phone) as adb,
        android_capability(adb, settle_max_wait_s=5, settle_quiet_ms=300) as capability,
    ):
        setup = _RecordingSetup(answers=False, over=True)
        capability.bind_setup(setup)
        _polls_counted(capability, setup.calls)
        clock = asyncio.get_running_loop().time
        started = clock()
        assert await capability.episode_over() is False
        took = clock() - started

    assert setup.calls[: setup.calls.index("stop")] == ["answers_episode_over"]
    assert took < 0.3, "no quiet period was waited for"


def test_prepare_refuses_a_device_the_provider_cannot_give_naming_setup_needs_and_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _RecordingSetup(needs=DeviceNeeds(emulator_grpc=True, fresh=True))
    capability = unset_capability()
    capability.backend = LocalEmulatorProvider(avd="AndroidWorldAvd", read_only=False)
    env = make_env(AndroidEnvironment, {"android#0": capability})
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(UnmetDeviceNeeds) as refused:
        env.check_setup({"oracle_task": {"a": 1}}, setups=[setup])

    message = str(refused.value)
    assert message.startswith(
        "the benchmark setup 'recording' needs emulator_grpc, fresh of the device, and the "
        "'local_emulator' provider cannot meet: emulator_grpc: "
    )
    assert "measured on macOS only" in message and "fresh: read_only is false" in message
    assert setup.calls == [], "the device comes first: the setup's own check never ran"


# ── the device side and its strategies ────────────────────────────────


def _android_block(**written: JsonValue) -> dict[str, JsonValue]:
    block: dict[str, JsonValue] = {
        "capability_id": "android",
        "backend": {"provider_id": "local_emulator", "avd": "AndroidWorldAvd"},
        "service_apk": "spectragent-device.apk",
        "tap_injector": {"injector_id": "accessibility_gesture"},
        "typing_backend": {"typing_id": "commit_text"},
        "ui_tree_cleaner": {"cleaner_id": "androidworld"},
        "ui_tree_serializer": {"serializer_id": "t3a"},
    }
    block.update(written)
    return block


def test_a_run_names_its_apk_its_input_strategies_and_its_tree_cleaner_and_serializer() -> None:
    """Which program delivers a touch, and which enters text, change what the device receives;
    which nodes become elements, and how they are printed, change what the agent sees. The tree's
    source has one value, so a block may leave it out."""
    required = ("service_apk", "tap_injector", "typing_backend")
    for left_out in (*required, "ui_tree_cleaner", "ui_tree_serializer"):
        block = _android_block()
        del block[left_out]
        raw: dict[str, JsonValue] = {"capability": [block]}
        with pytest.raises(ValidationError) as refused:
            ConfigComposer.for_config(raw).validate(raw)
        assert {error["type"] for error in refused.value.errors()} == {"missing"}, left_out

    raw = {"capability": [_android_block()]}
    capability = ConfigComposer.for_config(raw).compose(raw).single("capability")
    assert isinstance(capability, AndroidCapability)
    assert isinstance(capability.tap_injector, AccessibilityGestureInjector)
    assert isinstance(capability.typing_backend, CommitTextBackend)
    assert isinstance(capability.ui_tree_source, AccessibilityServiceTreeSource)
    assert isinstance(capability.ui_tree_cleaner, AndroidWorldCleaner)
    assert isinstance(capability.ui_tree_serializer, T3ASerializer)
    assert capability.service_apk == "spectragent-device.apk"


def test_a_run_sets_the_minimum_after_an_action_and_raises_the_wait_s_limit() -> None:
    """AndroidWorld's M3A sleeps 2 s after every action; its runner waits up to 6 s for the
    element list to stop changing."""
    raw: dict[str, JsonValue] = {
        "capability": [_android_block(settle_min_after_action_ms=2000, settle_max_wait_s=6)]
    }
    capability = ConfigComposer.for_config(raw).compose(raw).single("capability")
    assert isinstance(capability, AndroidCapability)
    assert (capability.settle_min_after_action_ms, capability.settle_max_wait_s) == (2000, 6)

    left_out: dict[str, JsonValue] = {"capability": [_android_block()]}
    default = ConfigComposer.for_config(left_out).compose(left_out).single("capability")
    assert isinstance(default, AndroidCapability)
    assert (default.settle_min_after_action_ms, default.settle_max_wait_s) == (0, 2.0)


def test_open_apps_table_takes_packages_and_components_and_refuses_anything_else() -> None:
    """A component (``package/activity``) is how AndroidWorld's table names what it opens."""

    def configured(apps: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            "capability": [
                _android_block(actions=[{"action_id": "android.open_app", "apps": apps}])
            ]
        }

    apps: dict[str, JsonValue] = {
        "Settings": "com.android.settings",
        "VLC": "org.videolan.vlc/.gui.MainActivity",
        "Discord": "com.discord/com.discord.app.AppActivity$Main",
    }
    raw = configured(apps)
    capability = ConfigComposer.for_config(raw).compose(raw).single("capability")
    assert isinstance(capability, AndroidCapability)
    (open_app,) = capability.actions
    assert isinstance(open_app, AndroidOpenAppAction) and open_app.apps == apps
    for written in ("VLC", "org.videolan.vlc/", "org.videolan.vlc/.gui Main"):
        raw = configured({"VLC": written})
        with pytest.raises(ValidationError, match="string_pattern_mismatch"):
            ConfigComposer.for_config(raw).validate(raw)


def _sidecar(path: Path) -> dict[str, JsonValue]:
    sidecar = json.loads(path.with_suffix(".json").read_text())
    assert isinstance(sidecar, dict)
    return sidecar


@pytest.mark.parametrize("problem", ["missing", "no sidecar", "other bytes", "other protocol"])
def test_prepare_refuses_an_apk_the_run_cannot_use(tmp_path: Path, problem: str) -> None:
    apk = fake_service_apk(tmp_path)
    if problem == "missing":
        apk.unlink()
    elif problem == "no sidecar":
        apk.with_suffix(".json").unlink()
    elif problem == "other bytes":
        apk.write_bytes(b"another build")
    else:
        sidecar = _sidecar(apk)
        sidecar["protocol"] = PROTOCOL_VERSION + 1
        apk.with_suffix(".json").write_text(json.dumps(sidecar))
    capability = AndroidCapability(service_apk=str(apk))
    env = make_env(AndroidEnvironment, {"android#0": capability})

    with pytest.raises(ServiceApkError):
        env.check_setup({})


async def test_setup_refuses_a_device_whose_service_runs_another_build(tmp_path: Path) -> None:
    phone = ScriptedPhone()
    phone.side.installed = APK_BYTES  # this build is on the device, so it is not installed again
    phone.side.service.apk_sha256 = "1" * 64  # and yet the service runs another
    async with phone_server(phone) as adb:
        capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
        provider = configured(capability, adb)
        with pytest.raises(DeviceSideError, match="runs the APK with SHA-256 1111"):
            await capability.setup()
        async with asyncio.timeout(5):  # a helper that never exits fails, not hangs
            while phone.side.helper_exits == 0:
                await asyncio.sleep(0.01)

    assert provider.closed == ["fake-session"] and not capability.is_alive
    assert phone.side.enabled is None, "the device side was stopped"


def test_prepare_refuses_a_typing_backend_the_providers_device_cannot_run(tmp_path: Path) -> None:
    """The provider says the device's API level from configuration, so the refusal comes before
    the trace opens (AGENTS.md §2.3); a provider that cannot say leaves it to setup."""
    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    capability.typing_backend = CommitTextBackend()
    env = make_env(AndroidEnvironment, {"android#0": capability})

    capability.backend = FakeServerProvider("127.0.0.1", 1, level=31)
    with pytest.raises(TypingBackendUnsupported) as refused:
        env.check_setup({})
    assert str(refused.value).startswith(
        "typing_backend 'commit_text' cannot enter text on the 'fake_server' provider's device: "
    )
    assert "this device runs API 31" in str(refused.value)

    for level in (33, None):
        capability.backend = FakeServerProvider("127.0.0.1", 1, level=level)
        env.check_setup({})


def test_a_run_types_with_key_events_on_every_api_level_the_apk_runs_on(tmp_path: Path) -> None:
    """The helper injects keys from API 30, the APK's lowest, as it injects ``key``."""
    for written, folds in (({}, False), ({"fold_accents": True}, True)):
        raw: dict[str, JsonValue] = {
            "capability": [_android_block(typing_backend={"typing_id": "key_events", **written})]
        }
        composed = ConfigComposer.for_config(raw).compose(raw).single("capability")
        assert isinstance(composed, AndroidCapability)
        assert isinstance(composed.typing_backend, KeyEventsBackend)
        assert composed.typing_backend.fold_accents is folds

    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    capability.typing_backend = KeyEventsBackend()
    capability.backend = FakeServerProvider("127.0.0.1", 1, level=30)
    make_env(AndroidEnvironment, {"android#0": capability}).check_setup({})


async def test_setup_refuses_a_typing_backend_the_device_cannot_run_when_only_it_can_tell(
    tmp_path: Path,
) -> None:
    """The backstop for a provider that cannot say the level before the device is up."""
    phone = ScriptedPhone(sdk_int=31)
    async with phone_server(phone) as adb:
        capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
        provider = configured(capability, adb, typing_backend=CommitTextBackend())
        with pytest.raises(
            TypingBackendUnsupported, match="'commit_text' .* on the device: .*API 31"
        ):
            await capability.setup()

    assert provider.needs != [], "it took booting the device to know"


async def test_setup_refuses_a_capability_without_the_strategies_a_run_names(
    tmp_path: Path,
) -> None:
    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    capability.backend = FakeServerProvider("127.0.0.1", 1)
    capability.tap_injector = AccessibilityGestureInjector()
    capability.typing_backend = CommitTextBackend()

    with pytest.raises(
        RuntimeError,
        match="needs a tap_injector .*, a typing_backend .*, a ui_tree_cleaner .* and a "
        "ui_tree_serializer",
    ):
        await capability.setup()


@dataclass
class _ForceStoppingSetup(_RecordingSetup):
    """A setup component whose start unbinds our service, as AndroidWorld's own service does."""

    phone: ScriptedPhone | None = None

    async def start(self, device: AndroidProviderSession, task: OracleTask | None) -> None:
        await super().start(device, task)
        assert self.phone is not None
        self.phone.side.force_stop()


async def test_the_service_is_bound_again_when_something_on_the_device_unbinds_it(
    tmp_path: Path,
) -> None:
    """Once during setup, after the benchmark's setup started, and once between steps."""
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with phone_server(phone) as adb:
        capability = AndroidCapability(
            service_apk=str(fake_service_apk(tmp_path)), settle_max_wait_s=2, settle_quiet_ms=50
        )
        configured(capability, adb, tap_injector=AccessibilityGestureInjector())
        capability.bind_setup(_ForceStoppingSetup(phone=phone))
        await capability.setup(oracle_task={"a": 1})
        phone.inputs.clear()  # setup's test tap
        try:
            assert phone.side.bound, "bound again before the run begins"
            phone.side.force_stop()  # a task force-stops every app
            tapped = await capability.execute_action(
                Action(
                    name="android.click_coordinate", params={"coordinate_x": 3, "coordinate_y": 4}
                )
            )
            await capability.begin_observation(1)
            state = await capability.resolve_fragment(CAPTURE_STATE)
        finally:
            await capability.teardown()

    assert tapped.success, tapped.error
    assert phone.inputs == ["service stroke (3,4)@0 (3,4)@50"]
    assert isinstance(state, AndroidCaptureStateObs) and state.settle is not None
    assert state.settle.outcome == "settled", "the new channel's events are read"


@pytest.mark.parametrize(("settle_max_wait_s", "subscriptions"), [(0.0, 0), (2.0, 1)])
async def test_events_are_subscribed_to_only_when_the_wait_reads_them(
    settle_max_wait_s: float, subscriptions: int
) -> None:
    """Unread, they would pile up to the stream's limit and end it without a word."""
    phone = ScriptedPhone()
    async with (
        phone_server(phone) as adb,
        android_capability(
            adb, settle_max_wait_s=settle_max_wait_s, settle_quiet_ms=50
        ) as capability,
    ):
        for cycle in (1, 2):
            await capability.begin_observation(cycle)
            await capability.resolve_fragment(SCREENSHOT)
            await capability.execute_action(
                Action(name="android.press_key", params={"key": "home"})
            )

        assert phone.service_calls("subscribe") == subscriptions
        assert phone.side.service.calls.count(("ping", {})) >= 4, "each job confirmed the service"
        assert phone.side.tokens_written == 1, "and never had to bind it again"


async def test_an_event_stream_that_ended_is_subscribed_afresh_on_a_new_channel() -> None:
    """A channel subscribes once, so a stream that lost an event ends, and the next job opens another."""
    phone = ScriptedPhone(focused="com.android.settings/.Settings")
    async with (
        phone_server(phone) as adb,
        android_capability(adb, settle_max_wait_s=2, settle_quiet_ms=50) as capability,
    ):
        phone.side.service.lose_event()
        phone.emit()  # numbered past the one lost: the stream ends there
        await capability.begin_observation(1)
        ended = await capability.resolve_fragment(CAPTURE_STATE)
        await capability.begin_observation(2)
        again = await capability.resolve_fragment(CAPTURE_STATE)

    assert isinstance(ended, AndroidCaptureStateObs) and ended.settle is not None
    assert ended.settle.outcome == "unknown"
    assert ended.settle.detail is not None and "event stream ended" in ended.settle.detail
    assert isinstance(again, AndroidCaptureStateObs) and again.settle is not None
    assert again.settle.outcome == "settled"
    assert phone.service_calls("subscribe") == 2
