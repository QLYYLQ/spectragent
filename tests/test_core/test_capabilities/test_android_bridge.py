"""The bridge's adb side: what it sends, and how it reads what the device answers.

The reports are shaped like the emulator's (API 33, 2026-11-01); the phone
behind the fake ADB server is ``tests/_helpers/android_device.py``. The
device side's roles, the screen and input, run through the capability in
``test_android_capability.py`` and ``test_android_actions.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import spectragent
from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.bridge import (
    AdbQueries,
    AppLauncher,
    BridgeError,
    DeviceFacts,
    ForegroundApp,
    ForegroundApps,
    TextFocus,
    parse_foreground,
    parse_text_focus,
)
from tests._helpers.android_device import FINGERPRINT, INPUT_METHOD, SERIAL, ScriptedPhone
from tests._helpers.fake_adb import FakeAdbServer, ShellResult, scripted

PACKAGE = Path(spectragent.__file__).resolve().parent / "core" / "capabilities" / "android"


def test_adb_plays_the_roles_of_what_the_device_reports_and_starting_apps() -> None:
    queries = AdbQueries(AdbClient("127.0.0.1", 1), SERIAL)

    for role in (ForegroundApps, AppLauncher, DeviceFacts):
        assert isinstance(queries, role)


def test_no_command_of_the_android_package_runs_screencap_or_input() -> None:
    """The device side replaced both (§11.2): a screenshot or a touch over adb is gone for good."""
    for module in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(module.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                words = node.value.split()
                assert words[:1] not in (["screencap"], ["input"]), (module, node.value)


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (
            "  mCurrentFocus=Window{388b3c9 u0 com.android.settings/com.android.settings.Settings}\n"
            "  mFocusedApp=ActivityRecord{6dbdd1 u0 com.android.settings/.Settings} t9}\n",
            ForegroundApp(
                package="com.android.settings",
                activity="com.android.settings.Settings",
                window="com.android.settings/com.android.settings.Settings",
            ),
        ),
        (
            "  mCurrentFocus=Window{aa9f2d4 u0 NotificationShade}\n"
            "  mFocusedApp=ActivityRecord{3e6a580 u0 com.google.android.apps.nexuslauncher/"
            "com.google.android.apps.nexuslauncher.NexusLauncherActivity t6}\n",
            ForegroundApp(
                package="com.google.android.apps.nexuslauncher",
                activity="com.google.android.apps.nexuslauncher.NexusLauncherActivity",
                window="NotificationShade",
            ),
        ),
        (
            "  mCurrentFocus=null\n  mFocusedApp=null\n",
            ForegroundApp(package=None, activity=None, window=None),
        ),
    ],
    ids=["an app", "a system window over an app", "nothing"],
)
def test_the_foreground_is_read_from_the_window_managers_focus_lines(
    report: str, expected: ForegroundApp
) -> None:
    assert parse_foreground(report) == expected


#: Windows as the emulator's ``dumpsys window`` named them (API 33, 2026-11-01).
_SEARCH = (
    "76bfbda u0 com.google.android.settings.intelligence/"
    "com.google.android.settings.intelligence.modules.search.SearchActivity"
)
_SETTINGS = "839e382 u0 com.android.settings/com.android.settings.Settings"
#: A dialog's window has its activity's title and an id of its own.
_ABOUT = "com.android.settings/com.android.settings.Settings$MyDeviceInfoActivity"
#: The input method client's line for a focused search field, and for no text field.
_SERVED = (
    "  mServedInputConnection=RemoteInputConnectionImpl{connection=dzl@95f8b53 finished=false "
    "mParentInputMethodManager.isActive()=true mServedView=dzd{4332b79 VFED..CL. .F...... "
    "0,0-954,126 #7f0a01fa app:id/open_search_view_edit_text aid=1073741824}}\n"
)
_NOT_SERVED = "  mServedInputConnection=null\n"
#: The same lines as API 30 and 31 print them (measured on the emulator, 2026-11-02).
_SERVED_API_30 = (
    "  mServedInputConnectionWrapper=ControlledInputConnectionWrapper{connection="
    "com.android.internal.widget.EditableInputConnection@9c97c14 finished=false}\n"
)
_SERVED_API_31 = (
    "  mServedInputConnectionWrapper=IInputConnectionWrapper{connection="
    "com.android.internal.widget.EditableInputConnection@2247f0c finished=false}\n"
)
_NOT_SERVED_API_31 = "  mServedInputConnectionWrapper=null\n"


def _windows(focus: str, target: str | None, *, line: str = "imeInputTarget") -> str:
    """The two lines of ``dumpsys window`` that say which window has focus and which the input method serves."""
    served = f"  {line} in display# 0 Window{{{target}}}\n" if target else ""
    return f"  mCurrentFocus=Window{{{focus}}}\n  mFocusedApp=null\n{served}"


def _client(served: str) -> str:
    return (
        "Input method client state for android.view.inputmethod.InputMethodManager@41da151:\n"
        "  mActive=true mRestartOnNextWindowFocus=false mBindSequence=10\n" + served
    )


@pytest.mark.parametrize(
    ("windows", "methods", "expected"),
    [
        (
            _windows(_SEARCH, _SEARCH),
            _client(_SERVED),
            TextFocus(window=_SEARCH.split(" u0 ")[1], text_field=True),
        ),
        (
            _windows(_SETTINGS, _SETTINGS),
            _client(_NOT_SERVED),
            TextFocus(
                window="com.android.settings/com.android.settings.Settings", text_field=False
            ),
        ),
        (
            _windows("31ca038 u0 NotificationShade", _SEARCH),
            _client(_SERVED),
            TextFocus(window="NotificationShade", text_field=False),
        ),
        (
            _windows(f"ee0071d u0 {_ABOUT}", f"61645bc u0 {_ABOUT}"),
            _client(_SERVED),
            TextFocus(window=_ABOUT, text_field=False),
        ),
        (
            _windows(_SETTINGS, None),
            _client(_SERVED),
            TextFocus(window="com.android.settings/com.android.settings.Settings", text_field=None),
        ),
        (
            _windows(_SETTINGS, None),
            _client(_NOT_SERVED),
            TextFocus(
                window="com.android.settings/com.android.settings.Settings", text_field=False
            ),
        ),
        (
            _windows(_SEARCH, _SEARCH, line="mInputMethodInputTarget"),
            _client(_SERVED),
            TextFocus(window=_SEARCH.split(" u0 ")[1], text_field=True),
        ),
        (
            _windows(_SEARCH, _SEARCH, line="mInputMethodInputTarget"),
            _client(_SERVED_API_30),
            TextFocus(window=_SEARCH.split(" u0 ")[1], text_field=True),
        ),
        (
            _windows(_SEARCH, _SEARCH),
            _client(_SERVED_API_31),
            TextFocus(window=_SEARCH.split(" u0 ")[1], text_field=True),
        ),
        (
            _windows(_SETTINGS, _SETTINGS),
            _client(_NOT_SERVED_API_31),
            TextFocus(
                window="com.android.settings/com.android.settings.Settings", text_field=False
            ),
        ),
        (
            "  mCurrentFocus=null\n  mFocusedApp=null\n",
            _client(_SERVED),
            TextFocus(window=None, text_field=False),
        ),
        (
            _windows(_SEARCH, _SEARCH),
            "Input method client state for android.view.inputmethod.InputMethodManager@41da151:\n"
            "Timeout waiting for dump\n",
            TextFocus(window=_SEARCH.split(" u0 ")[1], text_field=None),
        ),
    ],
    ids=[
        "a focused search field",
        "a page with no text field",
        "the notification shade over a focused field",
        "a dialog over the window the input method serves",
        "a connection, and no window the input method serves",
        "no connection, and no window the input method serves",
        "the target as Android 11 names it",
        "a focused field as API 30 reports it",
        "a focused field as API 31 reports it",
        "no text field as API 30 and 31 report it",
        "no window",
        "a client that did not answer",
    ],
)
def test_a_text_field_has_focus_where_the_input_method_serves_a_connection(
    windows: str, methods: str, expected: TextFocus
) -> None:
    assert parse_text_focus(windows, methods) == expected


async def test_the_text_focus_is_read_off_the_window_manager_and_the_input_method() -> None:
    async with FakeAdbServer({SERIAL: ScriptedPhone(text_field=True)}) as adb:
        focus = await AdbQueries(AdbClient(adb.host, adb.port), SERIAL).text_focus()

    assert focus.text_field is True
    assert [request.service for request in adb.requests] == [
        "shell,v2:dumpsys window",
        "shell,v2:dumpsys input_method",
    ]


async def test_the_device_facts_are_read_as_the_device_reports_them() -> None:
    async with FakeAdbServer({SERIAL: ScriptedPhone(width=720, height=1600)}) as adb:
        queries = AdbQueries(AdbClient(adb.host, adb.port), SERIAL)

        assert await queries.screen_size() == (720, 1600)
        assert await queries.fingerprint() == FINGERPRINT
        assert await queries.input_method() == INPUT_METHOD


async def test_an_override_size_wins_over_the_physical_one() -> None:
    device = scripted(
        {"wm size": ShellResult(stdout=b"Physical size: 1080x2400\nOverride size: 720x1600\n")}
    )
    async with FakeAdbServer({SERIAL: device}) as adb:
        assert await AdbQueries(AdbClient(adb.host, adb.port), SERIAL).screen_size() == (720, 1600)


async def test_an_unset_input_method_is_unknown_not_a_failure() -> None:
    device = scripted({"settings get secure default_input_method": ShellResult(stdout=b"null\n")})
    async with FakeAdbServer({SERIAL: device}) as adb:
        assert await AdbQueries(AdbClient(adb.host, adb.port), SERIAL).input_method() is None


@pytest.mark.parametrize("name", ["x; reboot", "com.example/.Main", "1android", "com.", ""])
async def test_only_a_package_name_reaches_a_command_line(name: str) -> None:
    async with FakeAdbServer({SERIAL: ScriptedPhone()}) as adb:
        queries = AdbQueries(AdbClient(adb.host, adb.port), SERIAL)
        with pytest.raises(ValueError, match="not an Android package name"):
            await queries.frames_drawn(name)

    assert adb.requests == []


async def test_the_systems_own_package_has_no_dot_and_is_asked_for_its_frames() -> None:
    """``android`` owns the "Open with" chooser; the window manager names it in front."""
    phone = ScriptedPhone(focused="android/com.android.internal.app.ResolverActivity", frames=7)
    async with FakeAdbServer({SERIAL: phone}) as adb:
        assert await AdbQueries(AdbClient(adb.host, adb.port), SERIAL).frames_drawn("android") == 7

    assert [request.service for request in adb.requests] == ["shell,v2:dumpsys gfxinfo android"]


@pytest.mark.parametrize("activity", [".Main; reboot", "$(reboot)", "gui/Main", ""])
async def test_only_an_activity_name_reaches_a_command_line(activity: str) -> None:
    async with FakeAdbServer({SERIAL: ScriptedPhone()}) as adb:
        queries = AdbQueries(AdbClient(adb.host, adb.port), SERIAL)
        with pytest.raises(ValueError, match="not an Android activity name"):
            await queries.launch("com.android.settings", activity)

    assert adb.requests == []


async def test_a_component_reaches_am_quoted_so_its_dollar_stays_a_dollar() -> None:
    async with FakeAdbServer({SERIAL: ScriptedPhone()}) as adb:
        await AdbQueries(AdbClient(adb.host, adb.port), SERIAL).launch(
            "com.discord", "com.discord.app.AppActivity$Main"
        )

    assert adb.requests[-1].service == (
        "shell,v2:am start -W -n 'com.discord/com.discord.app.AppActivity$Main'"
    )


async def test_an_activity_started_without_root_that_is_not_exported_fails_saying_so() -> None:
    """``am`` writes a header before the exception that says why; the error carries the reason."""
    started = "am start -W -n org.videolan.vlc/.gui.MainActivity"
    refused = ShellResult(
        stdout=b"Starting: Intent { cmp=org.videolan.vlc/.gui.MainActivity }\n",
        stderr=(
            b"\nException occurred while executing 'start':\n"
            b"java.lang.SecurityException: Permission Denial: starting Intent { flg=0x10000000 "
            b"cmp=org.videolan.vlc/.gui.MainActivity } from null (pid=6164, uid=2000) not "
            b"exported from uid 10191\n"
            b"\tat com.android.server.wm.ActivityTaskSupervisor.checkStartAnyActivityPermission"
            b"(ActivityTaskSupervisor.java:1116)\n"
        ),
        exit_code=255,
    )
    async with FakeAdbServer({SERIAL: scripted({started: refused})}) as adb:
        queries = AdbQueries(AdbClient(adb.host, adb.port), SERIAL)
        with pytest.raises(
            BridgeError, match=r"exit 255\): java\.lang\.SecurityException: .*not exported"
        ):
            await queries.launch("org.videolan.vlc", ".gui.MainActivity")


async def test_a_failed_command_says_which_and_why() -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        with pytest.raises(BridgeError, match="wm size failed .exit 127.: /system/bin/sh: wm"):
            await AdbQueries(AdbClient(adb.host, adb.port), SERIAL).screen_size()
