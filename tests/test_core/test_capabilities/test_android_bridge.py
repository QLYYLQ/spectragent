"""The bridge on adb's built-in commands: what it sends, and how it reads what the device answers.

The reports are shaped like the emulator's (API 33, 2026-11-01); the phone
behind the fake ADB server is ``tests/_helpers/android_device.py``.
"""

from __future__ import annotations

import pytest

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.bridge import (
    AdbBuiltins,
    AppLauncher,
    BridgeError,
    DeviceFacts,
    ForegroundApp,
    ForegroundApps,
    InputInjection,
    ScreenCapture,
    TextFocus,
    parse_foreground,
    parse_text_focus,
)
from tests._helpers.android_device import (
    FINGERPRINT,
    INPUT_METHOD,
    SERIAL,
    ScriptedPhone,
    png,
)
from tests._helpers.fake_adb import FakeAdbServer, ShellResult, scripted


def test_one_implementation_plays_every_role() -> None:
    builtins = AdbBuiltins(AdbClient("127.0.0.1", 1), SERIAL)

    for role in (ScreenCapture, InputInjection, ForegroundApps, AppLauncher, DeviceFacts):
        assert isinstance(builtins, role)
    assert builtins.producer == "screencap"


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


def _windows(focus: str, target: str | None) -> str:
    """The two lines of ``dumpsys window`` that say which window has focus and which the input method serves."""
    served = f"  imeInputTarget in display# 0 Window{{{target}}}\n" if target else ""
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
        "a window the input method has never served",
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
        focus = await AdbBuiltins(AdbClient(adb.host, adb.port), SERIAL).text_focus()

    assert focus.text_field is True
    assert [request.service for request in adb.requests] == [
        "shell,v2:dumpsys window",
        "shell,v2:dumpsys input_method",
    ]


async def test_the_device_facts_are_read_as_the_device_reports_them() -> None:
    async with FakeAdbServer({SERIAL: ScriptedPhone(width=720, height=1600)}) as adb:
        builtins = AdbBuiltins(AdbClient(adb.host, adb.port), SERIAL)

        assert await builtins.screen_size() == (720, 1600)
        assert await builtins.fingerprint() == FINGERPRINT
        assert await builtins.input_method() == INPUT_METHOD
        assert await builtins.has_feature("android.hardware.faketouch") is True
        assert await builtins.capture() == png(720, 1600)


async def test_an_override_size_wins_over_the_physical_one() -> None:
    device = scripted(
        {"wm size": ShellResult(stdout=b"Physical size: 1080x2400\nOverride size: 720x1600\n")}
    )
    async with FakeAdbServer({SERIAL: device}) as adb:
        assert await AdbBuiltins(AdbClient(adb.host, adb.port), SERIAL).screen_size() == (720, 1600)


async def test_an_unset_input_method_is_unknown_not_a_failure() -> None:
    device = scripted({"settings get secure default_input_method": ShellResult(stdout=b"null\n")})
    async with FakeAdbServer({SERIAL: device}) as adb:
        assert await AdbBuiltins(AdbClient(adb.host, adb.port), SERIAL).input_method() is None


async def test_text_is_quoted_for_the_devices_shell() -> None:
    phone = ScriptedPhone()
    async with FakeAdbServer({SERIAL: phone}) as adb:
        await AdbBuiltins(AdbClient(adb.host, adb.port), SERIAL).type_text("it's $HOME; ls")

    assert phone.inputs == ["input text 'it'\"'\"'s $HOME; ls'"]


async def test_text_off_the_virtual_keyboard_fails_saying_what_input_text_can_type() -> None:
    async with FakeAdbServer({SERIAL: ScriptedPhone()}) as adb:
        with pytest.raises(BridgeError, match="only characters on Android's virtual keyboard map"):
            await AdbBuiltins(AdbClient(adb.host, adb.port), SERIAL).type_text("你好")


async def test_a_screen_grab_that_is_not_a_png_is_refused() -> None:
    device = scripted({"screencap -p": ShellResult(stdout=b"WARNING: linker\n\x89PNG...")})
    async with FakeAdbServer({SERIAL: device}) as adb:
        with pytest.raises(BridgeError, match="not a PNG"):
            await AdbBuiltins(AdbClient(adb.host, adb.port), SERIAL).capture()


@pytest.mark.parametrize("name", ["x; reboot", "com.example/.Main", "settings", ""])
async def test_only_a_package_name_reaches_a_command_line(name: str) -> None:
    async with FakeAdbServer({SERIAL: ScriptedPhone()}) as adb:
        builtins = AdbBuiltins(AdbClient(adb.host, adb.port), SERIAL)
        with pytest.raises(ValueError, match="not an Android package name"):
            await builtins.is_installed(name)
        with pytest.raises(ValueError, match="not an Android package name"):
            await builtins.frames_drawn(name)

    assert adb.requests == []


async def test_a_failed_command_says_which_and_why() -> None:
    async with FakeAdbServer({SERIAL: scripted({})}) as adb:
        with pytest.raises(BridgeError, match="input tap failed .exit 127.: /system/bin/sh: input"):
            await AdbBuiltins(AdbClient(adb.host, adb.port), SERIAL).tap(1, 2)
