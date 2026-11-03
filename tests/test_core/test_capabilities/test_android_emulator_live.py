"""The Android capability on a real emulator, booted once for the whole module.

Opt-in (marker ``requires_android_emulator``): set ``SPECTRAGENT_TEST_AVD`` to
the name of an AVD and ``SPECTRAGENT_TEST_SERVICE_APK`` to an APK built by
``uv run python -m spectragent.core.capabilities.android.device.apk build``.
The capability is composed from YAML, as a run composes it, with the
``local_emulator`` provider, the accessibility service's gestures as its tap
injector and ``set_text`` as its typing backend, which every API level from 30
runs; ``SPECTRAGENT_TEST_TYPING_BACKEND`` names another by its ``typing_id``.
With ``key_events`` on API 33 every test passed (2026-11-04, device protocol
8; ``click`` by index timed out once at a load of about 50 and passed on its
own). The one boot covers, in file order:

- the key hygiene of ``docs/architecture_design.md`` §11.2 ("两把钥匙"): from
  API 31 the emulator hands the guest the run's own key (its log's "Sending
  adb public key" carries the run's ``adbkey.pub``), on the API 30 image it
  hands over none, and the user's ``~/.android/adbkey*`` stay as they were;
- the emulator's ``HOME`` is the run's: its console auth token is there, and a
  console command sent through adb with that ``HOME`` reaches the emulator;
- the run's own ``netsimd``: its ``netsim.ini`` is in the run's ``TMPDIR``, the
  user's ``$TMPDIR/netsim.ini`` stays as it was, and the guest joins its Wi-Fi;
- the device side is up: the platform fragment reports the APK the device
  runs, the faketouch check and the test tap; the screenshot comes from the
  helper where its screenshot works (API 31-33), else from the service, with
  the screen's geometry;
- ``open_app`` with the foreground app checked, and the back and home keys;
- ``type_coordinate`` without a point: refused on Settings' main page, where
  no text field has focus, and typed into the search field a tap opens;
- ``type_coordinate`` at a point (user rulings of 2026-11-02): refused after a
  tap on nothing editable; typed into the search field its tap on the search
  bar opens, which takes focus away from the point 2-3 s after the tap
  (measured); refused after a tap on the status bar, another window drawn
  over the search screen, the field's text untouched; typed into the Add
  network dialog's "Network name" field after a tap on that field's own
  label, which leaves focus in it: the label is inside the field as drawn;
  with WPA/WPA2-Personal chosen, the dialog holds a password field too, and a
  tap on the "Password" label, outside the "Network name" field as drawn, is
  refused with the name untouched; text at the cursor of the empty password
  field goes in, though the field reports no cursor, and once the field holds
  text the next is refused (``set_text`` cannot read it), saying how much the
  field holds, and the field keeps it; after keys
  with characters from the shell helper, which leave touch mode and let the
  keyboard give one of its own views focus, the node with input focus is still
  the dialog's: a tap on the "Password" label is refused naming the name field,
  and a tap on the password field types there;
- a scroll down that shows what is further down, measured on the screenshots,
  its effect naming the injector that delivered it;
- the UI tree of Settings' main page, cleaned and printed by the configured
  cleaner and serializer, with our service as its producer; ``click`` by an
  element's index opens the row it names, and ``scroll`` by the list's index
  moves the list, both found by their text, not their place; on the search
  screen, ``type`` by the field's index types into it; on the home screen,
  ``long_press`` by an app icon's index opens the icon's menu;
- a capture waits for a still screen: after a cold start of Settings it waits
  for the window manager, the frames and the events, and reports how;
- a clean teardown: no process of the run is left, nothing listens on its
  private adb port, its directory is gone, nothing listens on adb's default port
  5037 that did not before, every other emulator and ``netsimd`` (another
  run's or another tool's) that ran when the teardown began still runs, and
  the run added nothing to the top level of the user's home and left the
  user's ``~/.emulator_console_auth_token`` as it was.

The capability is composed with AndroidWorld's cleaner and M3A's serializer.
The points on Settings' pages were read off a Pixel 6 profile (1080 x 2400,
420 dpi) with the API 33 Google APIs image; on another screen or image the tests
that tap them skip.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import socket
import subprocess
import tempfile
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from PIL import Image, ImageChops

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.backends.base import AndroidProviderSession
from spectragent.core.capabilities.android.backends.local_emulator import (
    LocalEmulatorProvider,
    find_sdk,
)
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.device.apk import ServiceApk
from spectragent.core.capabilities.android.device.forest import (
    AndroidAccessibilityNodeInfo as Node,
)
from spectragent.core.capabilities.android.effects import AndroidTouchEffect
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
from spectragent.core.capabilities.android.ui_tree.serializers import M3ASerializer
from spectragent.core.composer import ConfigComposer
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.image import image_size

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.asyncio(loop_scope="module"),
]

#: The AVD to boot; the marker skips this module when it is unset (``tests/conftest.py``).
AVD_VARIABLE = "SPECTRAGENT_TEST_AVD"
APK_VARIABLE = "SPECTRAGENT_TEST_SERVICE_APK"
#: The typing backend to compose, by its ``typing_id``; ``set_text`` when unset.
TYPING_VARIABLE = "SPECTRAGENT_TEST_TYPING_BACKEND"
TYPING_BACKEND = os.environ.get(TYPING_VARIABLE, "set_text")
SETTINGS = "com.android.settings"
#: Settings' search: Google's on the API 31 and 33 images, AOSP's on API 30's.
SETTINGS_SEARCH = ("com.google.android.settings.intelligence", "com.android.settings.intelligence")
#: Where Settings' search bar is on a 1080 x 2400 screen.
SEARCH_BAR = (540, 660)
#: A point of Settings' main page on no control: the toolbar above its title.
NOTHING_THERE = (540, 300)
#: A point on the status bar, between the clock and the icons: a tap there takes no focus.
STATUS_BAR = (540, 40)
#: Where the search screen lists the results of a query, between its field and the keyboard.
RESULTS = (0, 300, 1080, 1400)
USER_KEYS = (Path.home() / ".android" / "adbkey", Path.home() / ".android" / "adbkey.pub")
#: The console auth token an emulator started with the user's HOME keeps there.
USER_TOKEN = Path.home() / ".emulator_console_auth_token"
#: Where every emulator started with the user's TMPDIR finds the netsimd it shares.
USER_NETSIM = Path(tempfile.gettempdir()) / "netsim.ini"


@dataclass
class Booted:
    """The booted device, and what the run and the user had before it."""

    capability: AndroidCapability
    session: AndroidProviderSession
    run_directory: Path
    emulator_pid: int
    emulator_group: int
    adb_server_pid: int
    user_keys: list[tuple[str, float] | None]
    user_netsim: tuple[str, float] | None
    #: The names at the top level of the user's home before the boot.
    user_home: set[str]
    user_token: tuple[str, float] | None
    listening_on_5037: bool
    cycle: int = 0


def _file_state(path: Path) -> tuple[str, float] | None:
    """*path*'s digest and modification time, or ``None`` when there is no such file."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime


def _key_state() -> list[tuple[str, float] | None]:
    """Each of the user's adb key files: its digest and modification time, or ``None``."""
    return [_file_state(key) for key in USER_KEYS]


def _processes() -> list[tuple[int, int, str]]:
    """Every process on the machine: its PID, its process group and its command."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid=,command="], capture_output=True, text=True, check=True
    ).stdout
    rows = [line.split(maxsplit=2) for line in listing.splitlines()]
    return [(int(pid), int(group), command) for pid, group, command in rows]


def _emulators(*, besides: int) -> set[int]:
    """The PIDs of every emulator and netsimd running now outside the process group *besides*."""
    return {
        pid
        for pid, group, command in _processes()
        if group != besides and ("qemu-system" in command or command.split()[0].endswith("netsimd"))
    }


def _netsimds_using(directory: Path) -> set[int]:
    """The netsimds with a file open under *directory*: a netsimd keeps its logs in its TMPDIR."""
    holders = subprocess.run(
        ["lsof", "-t", "+D", str(directory)], capture_output=True, text=True
    ).stdout
    using = {int(pid) for pid in holders.split()}
    return {
        pid
        for pid, _, command in _processes()
        if pid in using and command.split()[0].endswith("netsimd")
    }


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def booted() -> AsyncIterator[Booted]:
    apk = os.environ.get(APK_VARIABLE)
    if not apk:
        pytest.skip(
            f"set {APK_VARIABLE} to an APK built by `uv run python -m {ServiceApk.__module__} build`"
        )
    user_keys, user_netsim, listening = _key_state(), _file_state(USER_NETSIM), _listening(5037)
    user_home, user_token = set(os.listdir(Path.home())), _file_state(USER_TOKEN)
    raw = {
        "capability": [
            {
                "capability_id": "android",
                "backend": {
                    "provider_id": "local_emulator",
                    "avd": os.environ[AVD_VARIABLE],
                },
                "service_apk": apk,
                "tap_injector": {"injector_id": "accessibility_gesture"},
                "typing_backend": {"typing_id": TYPING_BACKEND},
                "ui_tree_cleaner": {"cleaner_id": "androidworld"},
                "ui_tree_serializer": {"serializer_id": "m3a"},
            }
        ]
    }
    capability = ConfigComposer.for_config(raw).compose(raw).single("capability")
    assert isinstance(capability, AndroidCapability)
    await capability.setup()
    try:
        provider, session = capability.backend, capability._session
        assert isinstance(provider, LocalEmulatorProvider) and session is not None
        run = provider._runs[session.session_id]  # what the run owns, for the checks below
        assert run.emulator is not None and run.emulator_group is not None
        assert run.server is not None
        yield Booted(
            capability=capability,
            session=session,
            run_directory=run.directory,
            emulator_pid=run.emulator.pid,
            emulator_group=run.emulator_group.id,
            adb_server_pid=run.server.process.pid,
            user_keys=user_keys,
            user_netsim=user_netsim,
            user_home=user_home,
            user_token=user_token,
            listening_on_5037=listening,
        )
    finally:
        await capability.teardown()


async def _act(booted: Booted, name: str, **params: Any) -> ActionResult:
    return await booted.capability.execute_action(Action(name=f"android.{name}", params=params))


async def _observe(booted: Booted, key: str) -> FragmentBase:
    booted.cycle += 1
    await booted.capability.begin_observation(booted.cycle)
    return await booted.capability.resolve_fragment(key)


async def _foreground(booted: Booted) -> AndroidForegroundAppObs:
    front = await _observe(booted, FOREGROUND_APP)
    assert isinstance(front, AndroidForegroundAppObs)
    return front


async def _screenshot(booted: Booted) -> Image.Image:
    shot = await _observe(booted, SCREENSHOT)
    assert isinstance(shot, AndroidScreenshotObs) and shot.image_bytes is not None
    return Image.open(io.BytesIO(shot.image_bytes)).convert("L")


async def _settings_from_scratch(booted: Booted) -> None:
    """Settings, freshly started, in front: no page left open by an earlier test."""
    client = AdbClient(booted.session.endpoint.host, booted.session.endpoint.port)
    for package in (SETTINGS, *SETTINGS_SEARCH):
        await client.shell(booted.session.serial, f"am force-stop {package}")
    assert (await _act(booted, "open_app", app_name="Settings")).success
    assert (await _foreground(booted)).package == SETTINGS


async def test_the_emulator_got_the_runs_own_key_and_the_users_keys_are_untouched(
    booted: Booted,
) -> None:
    """Emulator 37.2.13 hands the key over only through the boot property
    ``androidboot.qemu.adb.pubkey``, and logs it; the API 30 image takes none (2026-11-02)."""
    platform = await _observe(booted, PLATFORM)
    assert isinstance(platform, AndroidPlatformObs) and platform.sdk_int is not None
    log = (booted.run_directory / "emulator.log").read_text(errors="replace")
    sent = re.search(r"Sending adb public key \[(\S+)", log)
    run_key = (booted.run_directory / ".android" / "adbkey.pub").read_text().split()[0]

    if platform.sdk_int >= 31:
        assert sent is not None, "the emulator's log names the key it hands the guest"
        assert sent[1] == run_key
    else:
        assert sent is None, "the API 30 image is handed no key"
    assert _key_state() == booted.user_keys


async def test_a_console_command_sent_through_adb_reaches_the_emulator(booted: Booted) -> None:
    """The emulator keeps its console auth token in the run's HOME, where adb looks for it when it
    runs with that HOME; with two different homes the command fails without a word
    (``docs/architecture_design.md`` §11.2)."""
    assert (booted.run_directory / ".emulator_console_auth_token").is_file()
    provider = booted.capability.backend
    assert isinstance(provider, LocalEmulatorProvider)
    adb = find_sdk(provider.sdk_root, os.environ) / "platform-tools" / "adb"
    answer = subprocess.run(
        [str(adb), "-P", str(booted.session.endpoint.port), "-s", booted.session.serial]
        + ["emu", "avd", "name"],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "HOME": str(booted.run_directory)},
    )
    assert answer.stdout.split() == [provider.avd, "OK"], answer.stderr


async def test_the_run_has_a_netsimd_of_its_own_and_the_guest_is_online(booted: Booted) -> None:
    """netsimd carries the guest's network; one shared through the user's TMPDIR would die with
    whichever emulator's group started it (``docs/architecture_design.md`` §11.2)."""
    assert (booted.run_directory / "tmp" / "netsim.ini").is_file()
    assert _file_state(USER_NETSIM) == booted.user_netsim

    client = AdbClient(booted.session.endpoint.host, booted.session.endpoint.port)
    status = ""
    for _ in range(60):  # Wi-Fi joined about 5 s after the boot completed; at most 30 s
        status = (await client.shell(booted.session.serial, "cmd wifi status")).stdout.decode()
        if "Wifi is connected to" in status:
            break
        await asyncio.sleep(0.5)
    assert "Wifi is connected to" in status, status


async def test_the_device_side_is_up_on_the_configured_build(booted: Booted) -> None:
    platform = await _observe(booted, PLATFORM)
    assert isinstance(platform, AndroidPlatformObs)
    apk = ServiceApk.load(os.environ[APK_VARIABLE])

    assert platform.service_apk_sha256 == apk.info.sha256
    assert platform.service_apk_signing_certificate_sha256 == apk.info.signing_certificate_sha256
    assert (platform.faketouch, platform.gestures) == (True, True)
    assert platform.sdk_int is not None and platform.sdk_int >= 30
    expected_tool = apk.info.accessibility_tool if platform.sdk_int >= 31 else None
    assert platform.accessibility_tool == expected_tool


async def test_a_screenshot_comes_from_the_fastest_program_and_reports_the_screens_geometry(
    booted: Booted,
) -> None:
    platform = await _observe(booted, PLATFORM)
    shot = await _observe(booted, SCREENSHOT)
    assert isinstance(platform, AndroidPlatformObs) and platform.sdk_int is not None
    assert isinstance(shot, AndroidScreenshotObs) and shot.image_bytes is not None
    size = booted.capability.screen_size

    assert image_size(shot.image_bytes) == (shot.image_width, shot.image_height) == size
    assert (shot.viewport_width, shot.viewport_height) == size
    expected = "shell_helper" if 31 <= platform.sdk_int <= 33 else "accessibility_service"
    assert shot.producer == expected
    assert shot.capture_ms is not None and shot.encode_ms is not None


async def test_open_app_brings_settings_to_the_front_and_back_and_home_leave_it(
    booted: Booted,
) -> None:
    await _settings_from_scratch(booted)

    assert (await _act(booted, "press_key", key="back")).success
    after_back = await _foreground(booted)
    await _settings_from_scratch(booted)
    assert (await _act(booted, "press_key", key="home")).success
    after_home = await _foreground(booted)

    for front in (after_back, after_home):
        assert front.package is not None and "launcher" in front.package


async def test_typing_without_a_point_goes_into_the_field_that_has_focus_or_nowhere(
    booted: Booted,
) -> None:
    """Refused where no text field has focus; typed into Settings' search once a tap opens it."""
    await _skip_unless_measured_layout(booted)
    await _settings_from_scratch(booted)

    refused = await _act(booted, "type_coordinate", text="bluetooth")
    assert (refused.success, refused.error) == (
        False,
        "No text field has focus: focus is in com.android.settings/com.android.settings.Settings, "
        "not on a text field, so no text was typed.",
    )
    assert (
        await _act(
            booted, "click_coordinate", coordinate_x=SEARCH_BAR[0], coordinate_y=SEARCH_BAR[1]
        )
    ).success
    assert (await _foreground(booted)).package in SETTINGS_SEARCH
    before = await _screenshot(booted)
    typed = await _act(booted, "type_coordinate", text="bluetooth")
    assert typed.success, typed.error
    after = await _screenshot(booted)

    assert _results_appeared(before, after)


async def test_typing_at_a_point_types_where_focus_moves_and_refuses_where_it_cannot(
    booted: Booted,
) -> None:
    """The tap on the search bar opens a screen whose field takes focus 2-3 s later, away from
    the point, and that types (user ruling 2026-11-02); a tap on nothing focusable is refused,
    and so is a tap on the status bar, another window drawn over the search screen."""
    await _skip_unless_measured_layout(booted)
    await _settings_from_scratch(booted)

    refused = await _act(
        booted,
        "type_coordinate",
        coordinate_x=NOTHING_THERE[0],
        coordinate_y=NOTHING_THERE[1],
        text="bluetooth",
    )
    assert (refused.success, refused.error) == (
        False,
        "No text field took focus at the tapped point: focus is in "
        "com.android.settings/com.android.settings.Settings, not on a text field, "
        "so no text was typed.",
    )
    assert (
        await _act(
            booted, "click_coordinate", coordinate_x=SEARCH_BAR[0], coordinate_y=SEARCH_BAR[1]
        )
    ).success
    assert (await _foreground(booted)).package in SETTINGS_SEARCH
    empty = await _screenshot(booted)

    await _settings_from_scratch(booted)
    typed = await _act(
        booted,
        "type_coordinate",
        coordinate_x=SEARCH_BAR[0],
        coordinate_y=SEARCH_BAR[1],
        text="bluetooth",
    )
    assert typed.success, typed.error
    assert (await _foreground(booted)).package in SETTINGS_SEARCH
    assert _results_appeared(empty, await _screenshot(booted))

    # The search field is its screen's only text field, so its field is the whole window; the
    # status bar is another window drawn over it there.
    stayed = await _act(
        booted, "type_coordinate", coordinate_x=STATUS_BAR[0], coordinate_y=STATUS_BAR[1], text="x"
    )
    assert not stayed.success
    assert stayed.error is not None and "focus stayed on EditText" in stayed.error
    assert await _field_text(booted) == "bluetooth", "nothing was cleared or typed"


async def test_typing_at_a_point_on_a_fields_own_label_types_into_that_field(
    booted: Booted,
) -> None:
    """The Add network dialog focuses its "Network name" field, with the label above it. A tap
    on the label leaves focus in the field, away from the field's own bounds but inside the
    field as drawn (user ruling 2026-11-02: here the whole dialog, whose only text field it
    is), so the text goes in."""
    await _skip_unless_measured_layout(booted)
    client = AdbClient(booted.session.endpoint.host, booted.session.endpoint.port)
    await client.shell(booted.session.serial, "am force-stop com.android.settings")
    await client.shell(booted.session.serial, "am start -W -a android.settings.WIFI_SETTINGS")
    add = await _text_at(booted, "Add network")
    assert (
        await _act(booted, "click_coordinate", coordinate_x=add[0], coordinate_y=add[1])
    ).success
    side = booted.capability._side
    assert side is not None
    async with asyncio.timeout(10):
        while (focused := await side.service.focus(field_bounds=True)) is None:
            await asyncio.sleep(0.1)
    label = await _text_at(booted, "Network name")
    assert focused.is_editable and focused.view_id_resource_name == "com.android.settings:id/ssid"
    assert not (focused.bounds_in_screen.top <= label[1] <= focused.bounds_in_screen.bottom)
    assert focused.field_holds(*label), "the label is inside the field as drawn"

    typed = await _act(
        booted, "type_coordinate", coordinate_x=label[0], coordinate_y=label[1], text="CafeNet"
    )

    assert typed.success, typed.error
    assert await _field_text(booted) == "CafeNet"


async def test_typing_at_a_point_on_another_fields_label_is_refused(booted: Booted) -> None:
    """With WPA/WPA2-Personal chosen, the Add network dialog holds "Network name" and "Password"
    fields, so the walk from the name stops below their shared layout. A tap on the "Password"
    label (a text view, which takes no focus) is outside the name's field: refused."""
    await _skip_unless_measured_layout(booted)
    await _add_network_with_a_password(booted)
    name = await _text_at(booted, "Enter the SSID")
    named = await _act(
        booted, "type_coordinate", coordinate_x=name[0], coordinate_y=name[1], text="CafeNet"
    )
    assert named.success, named.error
    label = await _text_at(booted, "Password")

    refused = await _act(
        booted, "type_coordinate", coordinate_x=label[0], coordinate_y=label[1], text="x"
    )

    assert not refused.success
    assert refused.error is not None and "focus stayed on EditText" in refused.error
    name_now = await _field_text(booted, "com.android.settings:id/ssid")
    assert name_now == "CafeNet", "nothing was cleared or typed"


async def test_typing_at_the_cursor_of_an_empty_android_password_field_enters_the_text(
    booted: Booted,
) -> None:
    """set_text cannot read a password field's text, so text goes at its cursor (``clear:
    false``, as M3A always sends) only into an empty one, where it replaces nothing: no text,
    and no cursor or one at the start. Android's own EditText reports no cursor for a password,
    even an empty one (measured 2026-11-03), and takes the text; once the field holds text,
    text at its cursor is refused, saying how much the field holds, and the field keeps it.
    The refusal is set_text's own: key events and commit_text put the text at the cursor."""
    await _skip_unless_measured_layout(booted)
    await _add_network_with_a_password(booted)
    x, y = await _view_at(booted, PASSWORD)

    typed = await _act(
        booted, "type_coordinate", coordinate_x=x, coordinate_y=y, text="hunter22", clear=False
    )
    assert typed.success, typed.error
    assert len(await _field_text(booted, PASSWORD) or "") == 8
    held = await _act(
        booted, "type_coordinate", coordinate_x=x, coordinate_y=y, text="x", clear=False
    )

    if TYPING_BACKEND != "set_text":
        assert held.success, held.error
        assert len(await _field_text(booted, PASSWORD) or "") == 9
        return
    assert (held.success, held.error) == (
        False,
        "RemoteError: set_text: password_field: a password field hides its text, so text goes "
        "at its cursor only when the field is empty, with no text and no cursor or one at the "
        "start; this one holds 8 characters",
    )
    assert len(await _field_text(booted, PASSWORD) or "") == 8, "nothing was typed"


async def test_after_keys_with_characters_focus_is_the_apps_field_not_the_keyboards_view(
    booted: Booted,
) -> None:
    """Keys with characters, the shell helper's, take the screen out of touch mode, and Gboard
    then gives one of its own views focus; after the next tree capture the service's node cache
    named that view (2026-11-03). Input focus is looked for only in the window that has it, the
    app's: a tap on the "Password" label is refused naming the name field, and a tap on the
    password field types there."""
    await _skip_unless_measured_layout(booted)
    await _add_network_with_a_password(booted)
    x, y = await _text_at(booted, "Enter the SSID")
    assert (await _act(booted, "click_coordinate", coordinate_x=x, coordinate_y=y)).success
    side = booted.capability._side
    assert side is not None
    for code in ("KEYCODE_C", "KEYCODE_A", "KEYCODE_F", "KEYCODE_E"):
        await side.helper.key(code)
    await _observe(booted, UI_TREE)  # the observation after the keys
    label = await _text_at(booted, "Password")

    refused = await _act(
        booted, "type_coordinate", coordinate_x=label[0], coordinate_y=label[1], text="x"
    )
    field = await _view_at(booted, PASSWORD)
    typed = await _act(
        booted, "type_coordinate", coordinate_x=field[0], coordinate_y=field[1], text="hunter22"
    )

    assert refused.error is not None
    assert "focus stayed on EditText com.android.settings:id/ssid" in refused.error
    assert typed.success, typed.error
    assert await _field_text(booted, "com.android.settings:id/ssid") == "cafe"
    assert len(await _field_text(booted, PASSWORD) or "") == 8


#: The Add network dialog's password field, once a security with a password is chosen.
PASSWORD = "com.android.settings:id/password"


async def _add_network_with_a_password(booted: Booted) -> None:
    """Settings' Add network dialog, with WPA/WPA2-Personal chosen: a name and a password field."""
    client = AdbClient(booted.session.endpoint.host, booted.session.endpoint.port)
    await client.shell(booted.session.serial, "am force-stop com.android.settings")
    await client.shell(booted.session.serial, "am start -W -a android.settings.WIFI_SETTINGS")
    for text in ("Add network", "None", "WPA/WPA2-Personal"):
        point = await _text_at(booted, text)
        tapped = await _act(
            booted, "click_coordinate", coordinate_x=point[0], coordinate_y=point[1]
        )
        assert tapped.success, tapped.error


async def _text_at(booted: Booted, text: str) -> tuple[int, int]:
    """The centre of the visible node whose text is *text*, once the service's tree has it."""
    return await _node_at(booted, lambda node: node.text == text)


async def _view_at(booted: Booted, view_id: str) -> tuple[int, int]:
    """The centre of the visible node with the view id *view_id*, once the service's tree has it."""
    return await _node_at(booted, lambda node: node.view_id_resource_name == view_id)


async def _node_at(booted: Booted, wanted: Callable[[Node], bool]) -> tuple[int, int]:
    side = booted.capability._side
    assert side is not None
    async with asyncio.timeout(10):
        while True:
            capture = await side.service.tree()
            for window in capture.forest.windows:
                for node in window.tree.nodes:
                    if wanted(node) and node.is_visible_to_user:
                        box = node.bounds_in_screen
                        return (box.left + box.right) // 2, (box.top + box.bottom) // 2
            await asyncio.sleep(0.2)


async def _field_text(booted: Booted, view_id: str | None = None) -> str | None:
    """The text of the editable field on screen (the one with *view_id*), as the service's tree has it."""
    side = booted.capability._side
    assert side is not None
    capture = await side.service.tree()
    texts = [
        node.text
        for window in capture.forest.windows
        for node in window.tree.nodes
        if node.is_editable
        and node.is_visible_to_user
        and (view_id is None or node.view_id_resource_name == view_id)
    ]
    return texts[0] if texts else None


async def _skip_unless_measured_layout(booted: Booted) -> None:
    """The points on Settings' pages were read off the API 33 image on a 1080 x 2400 screen.

    On the API 30 and 31 images, Settings' first page is laid out otherwise:
    the search bar's point opened another page there, and the empty point a
    suggestion card (2026-11-02).
    """
    platform = await _observe(booted, PLATFORM)
    assert isinstance(platform, AndroidPlatformObs)
    if booted.capability.screen_size != (1080, 2400) or platform.sdk_int != 33:
        pytest.skip("the points on Settings' pages were measured on the API 33 image, 1080 x 2400")


def _results_appeared(before: Image.Image, after: Image.Image) -> bool:
    """Whether a query's results appeared between the search field and the keyboard."""
    changed = ImageChops.difference(before.crop(RESULTS), after.crop(RESULTS)).histogram()
    return sum(changed[32:]) > 10_000


async def test_a_scroll_down_shows_what_is_further_down(booted: Booted) -> None:
    await _settings_from_scratch(booted)
    before = await _screenshot(booted)

    scrolled = await _act(booted, "scroll", direction="down")
    assert scrolled.success, scrolled.error
    after = await _screenshot(booted)

    assert _content_shift(before, after) > 100, "the content moved up the screen"
    assert isinstance(scrolled.effect, AndroidTouchEffect)
    assert scrolled.effect.touch.delivered_by == "accessibility_gesture"


async def test_a_capture_after_a_cold_start_waits_for_a_still_screen(booted: Booted) -> None:
    """``am start`` returns once Settings is shown; the capture after it waits, and says how."""
    client = AdbClient(booted.session.endpoint.host, booted.session.endpoint.port)
    await client.shell(booted.session.serial, f"am force-stop {SETTINGS}")
    assert (await _act(booted, "press_key", key="home")).success
    assert (await _act(booted, "open_app", app_name="Settings")).success

    state = await _observe(booted, CAPTURE_STATE)

    assert isinstance(state, AndroidCaptureStateObs) and state.settle is not None
    print(f"after a cold start of Settings: {state.settle}")
    assert state.settle.outcome in ("settled", "capped"), state.settle.detail
    again = await _observe(booted, CAPTURE_STATE)
    assert isinstance(again, AndroidCaptureStateObs) and again.settle is not None
    print(f"right after: {again.settle}")
    assert again.settle.outcome == "settled", "nothing changed since"


async def _tree(booted: Booted) -> AndroidUITreeObs:
    tree = await _observe(booted, UI_TREE)
    assert isinstance(tree, AndroidUITreeObs)
    return tree


def _index_of(tree: AndroidUITreeObs, matches: Callable[[Node], bool]) -> int:
    for index, node in tree.tree().element_nodes():
        if matches(node):
            return index
    raise AssertionError(f"no element matches; the tree reads:\n{tree.serialized}")


def _texts(tree: AndroidUITreeObs) -> list[str]:
    return [node.text for _, node in tree.tree().element_nodes() if node.text]


async def test_the_ui_tree_of_settings_is_cleaned_and_printed_by_the_configured_strategies(
    booted: Booted,
) -> None:
    await _settings_from_scratch(booted)

    tree = await _tree(booted)

    assert tree.producer == "accessibility_service"
    assert 0 < len(tree.elements) < tree.node_count and tree.omitted_offscreen == 0
    assert tree.capture_ms is not None and tree.capture_ms > 0
    assert "Network & internet" in _texts(tree)
    assert tree.serialized == M3ASerializer().serialize(tree.tree())
    print(
        f"Settings: {len(tree.elements)} elements of {tree.node_count} nodes, {tree.capture_ms} ms"
    )


async def test_click_by_index_opens_the_row_it_names(booted: Booted) -> None:
    await _settings_from_scratch(booted)
    tree = await _tree(booted)

    tapped = await _act(
        booted, "click", index=_index_of(tree, lambda node: node.text == "Network & internet")
    )

    assert tapped.success, tapped.error
    assert isinstance(tapped.effect, AndroidTouchEffect)
    assert tapped.effect.touch.delivered_by == "accessibility_gesture"
    # The page's window joins the accessibility window list 1-2 s after the
    # window manager shows it, longer under load than the 2 s settle cap.
    async with asyncio.timeout(10):
        while not {"Internet", "Airplane mode"} <= set(_texts(opened := await _tree(booted))):
            print(f"not yet:\n{opened.serialized}")


async def test_scroll_by_index_moves_the_list_it_names(booted: Booted) -> None:
    await _settings_from_scratch(booted)
    tree = await _tree(booted)
    before = _texts(tree)

    scrolled = await _act(
        booted,
        "scroll",
        direction="down",
        index=_index_of(tree, lambda node: node.is_scrollable),
    )

    assert scrolled.success, scrolled.error
    after = _texts(await _tree(booted))
    assert after != before and "Network & internet" not in after, after


async def _search_screen_tree(booted: Booted) -> AndroidUITreeObs:
    """Settings' search screen in front, its field in the tree."""
    await _skip_unless_measured_layout(booted)
    await _settings_from_scratch(booted)
    assert (
        await _act(
            booted, "click_coordinate", coordinate_x=SEARCH_BAR[0], coordinate_y=SEARCH_BAR[1]
        )
    ).success
    assert (await _foreground(booted)).package in SETTINGS_SEARCH
    return await _tree(booted)


async def test_type_by_index_types_into_the_field_it_names(booted: Booted) -> None:
    """The browser's ``type``: a tap at the element's centre puts focus there, then the text."""
    tree = await _search_screen_tree(booted)

    typed = await _act(
        booted,
        "type",
        index=_index_of(tree, lambda node: node.is_editable),
        text="bluetooth",
        clear=False,
    )

    assert typed.success, typed.error
    assert await _field_text(booted) == "bluetooth"


async def test_long_press_by_index_opens_the_menu_of_the_app_icon_it_names(booted: Booted) -> None:
    """On the home screen, a long press on an app's icon opens its menu (App info, shortcuts)."""
    assert (await _act(booted, "press_key", key="home")).success
    tree = await _tree(booted)

    pressed = await _act(
        booted, "long_press", index=_index_of(tree, lambda node: node.text == "Phone")
    )

    assert pressed.success, pressed.error
    assert isinstance(pressed.effect, AndroidTouchEffect)
    assert pressed.effect.touch.gesture == "long_press"
    menu = await _tree(booted)
    assert "App info" in _texts(menu), menu.serialized
    assert (await _act(booted, "press_key", key="back")).success, "the menu closes"


def _content_shift(before: Image.Image, after: Image.Image) -> int:
    """How far the content moved up between the two screens, in pixels (negative: down).

    Each row of pixels with some content in it votes for the offset at which
    the same row appears in the other screen; the most common offset wins.
    """
    width, height = before.size

    def rows(image: Image.Image) -> list[bytes]:
        data = image.crop((60, 0, width - 60, height)).tobytes()
        row = width - 120
        return [data[i * row : (i + 1) * row] for i in range(height)]

    positions: dict[bytes, list[int]] = {}
    for y, row in enumerate(rows(before)):
        if len(set(row)) > 8:
            positions.setdefault(row, []).append(y)
    votes = Counter(
        y_before - y_after
        for y_after, row in enumerate(rows(after))
        for y_before in positions.get(row, ())
    )
    votes.pop(0, None)  # what did not move: the status bar
    return votes.most_common(1)[0][0] if votes else 0


async def test_teardown_stops_what_the_run_started_and_removes_its_directory(
    booted: Booted,
) -> None:
    # Taken just before: another tool may stop its own emulator during the boot.
    own_netsimd = _netsimds_using(booted.run_directory / "tmp")
    assert own_netsimd, "the run's own netsimd runs, with its logs open in the run's tmp"
    others = _emulators(besides=booted.emulator_group) - own_netsimd
    await booted.capability.teardown()

    processes = _processes()
    left = {pid for pid, group, _ in processes if group == booted.emulator_group}
    assert left == set(), "nothing of the emulator's process group is left, netsimd included"
    running = {pid for pid, _, _ in processes}
    assert booted.emulator_pid not in running and booted.adb_server_pid not in running
    assert not own_netsimd & running, "the run's own netsimd is gone, wherever its group was"
    assert others <= running, "every other emulator and netsimd still runs"
    assert not _listening(booted.session.endpoint.port), "the run's adb server is gone"
    assert not booted.run_directory.exists()
    assert _listening(5037) == booted.listening_on_5037
    assert _key_state() == booted.user_keys
    assert _file_state(USER_NETSIM) == booted.user_netsim
    assert set(os.listdir(Path.home())) - booted.user_home == set()
    assert _file_state(USER_TOKEN) == booted.user_token
