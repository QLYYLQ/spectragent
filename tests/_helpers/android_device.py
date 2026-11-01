"""A scripted phone behind the fake ADB server, and the Android capability set up over it.

:class:`ScriptedPhone` answers the commands the capability's bridge sends
(``spectragent/core/capabilities/android/bridge.py``) with output shaped like
the emulator's (API 33, 2026-11-01): a PNG for ``screencap``, the focus lines of
``dumpsys window``, the input method client's served connection from
``dumpsys input_method``, a frame count from ``dumpsys gfxinfo``, ``wm size``,
``getprop``, ``settings``, ``pm``, ``cmd package`` and ``am start``. Any other
command is answered as ``sh`` does (exit 127), so a capability that starts
sending a new command fails a test until this phone learns it. It keeps every
``input`` command it received, in order.

:func:`android_capability` is an ``AndroidCapability`` set up over a
:class:`~tests._helpers.fake_adb.FakeAdbServer` holding such a phone: a
provider hands it the fake server's endpoint, as ``local_emulator`` hands it
the run's private adb server.
"""

from __future__ import annotations

import shlex
import struct
import zlib
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from spectragent.core.capabilities.android.backends.base import (
    AdbEndpoint,
    AndroidProviderSession,
    DeviceNeeds,
)
from spectragent.core.capabilities.android.capability import AndroidCapability
from tests._helpers.fake_adb import FakeAdbServer, ShellResult

SERIAL = "emulator-5554"
LAUNCHER = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
FINGERPRINT = "google/sdk_gphone64_arm64/emu64a:13/TE1A.240213.009/12342917:userdebug/dev-keys"
INPUT_METHOD = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
_LAUNCHABLE = "-a android.intent.action.MAIN -c android.intent.category.LAUNCHER"


def png(width: int, height: int) -> bytes:
    """The PNG signature and IHDR chunk: enough for every reader of an image's type and size."""
    ihdr = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + struct.pack(">I", zlib.crc32(ihdr))


@dataclass
class ScriptedPhone:
    """A phone's answers; change the attributes to change what it reports next.

    Attributes:
        width: Its screen's width in pixels (a Pixel 6 by default).
        height: Its height.
        focused: The activity in front, as ``package/.Activity``.
        frames: How many frames the app in front has rendered.
        launchable: Installed packages with their launcher activities.
        screencap_fails: ``screencap`` fails from now on.
        text_field: A text field of the activity in front has focus: the input
            method serves its window, and the view with focus holds an input
            connection.
        window_over: A window over the activity that has focus instead of it,
            by its title (``NotificationShade``): the input method still serves
            the activity's window, as the emulator reported.
        inputs: Every ``input`` command received, in order.
    """

    width: int = 1080
    height: int = 2400
    focused: str = LAUNCHER
    frames: int = 36
    launchable: dict[str, str] = field(
        default_factory=lambda: {"com.android.settings": "com.android.settings/.Settings"}
    )
    screencap_fails: bool = False
    text_field: bool = False
    window_over: str | None = None
    inputs: list[str] = field(default_factory=list)

    async def __call__(self, command: str) -> ShellResult:
        words = command.split()
        if words[:1] == ["input"]:
            self.inputs.append(command)
            return self._input(command)
        package = self.focused.partition("/")[0]
        if command == "screencap -p":
            if self.screencap_fails:
                return ShellResult(stderr=b"screencap: capture failed\n", exit_code=1)
            return ShellResult(stdout=png(self.width, self.height))
        if command == "dumpsys window":
            return ShellResult(stdout=_window_report(self.focused, self.window_over))
        if command == "dumpsys input_method":
            return ShellResult(stdout=_input_method_report(self.text_field))
        if words[:2] == ["dumpsys", "gfxinfo"]:
            if words[2] != package:
                return ShellResult(stdout=f"No process found for: {words[2]}\n".encode())
            return ShellResult(stdout=f"Total frames rendered: {self.frames}\n".encode())
        if command == "wm size":
            return ShellResult(stdout=f"Physical size: {self.width}x{self.height}\n".encode())
        if command == "getprop ro.build.fingerprint":
            return ShellResult(stdout=f"{FINGERPRINT}\n".encode())
        if command == "settings get secure default_input_method":
            return ShellResult(stdout=f"{INPUT_METHOD}\n".encode())
        if command == "pm has-feature android.hardware.faketouch":
            return ShellResult(stdout=b"true\n")
        if words[:2] == ["pm", "path"]:
            if words[2] in self.launchable:
                return ShellResult(stdout=f"package:/system/app/{words[2]}.apk\n".encode())
            return ShellResult(exit_code=1)
        if command.startswith(f"cmd package resolve-activity --brief {_LAUNCHABLE} "):
            component = self.launchable[words[-1]]
            return ShellResult(stdout=f"priority=0 isDefault=true\n{component}\n".encode())
        if words[:4] == ["am", "start", "-W", "-n"]:
            self.focused = words[4]
            return ShellResult(
                stdout=f"Starting: Intent {{ cmp={words[4]} }}\nStatus: ok\n".encode()
            )
        name = words[0] if words else command
        return ShellResult(
            stderr=f"/system/bin/sh: {name}: inaccessible or not found\n".encode(), exit_code=127
        )

    def _input(self, command: str) -> ShellResult:
        words = shlex.split(command)
        if words[1] == "text" and not words[2].isascii():
            # What API 33 answered for a character outside the virtual keyboard map.
            return ShellResult(
                stderr=b"\nException occurred while executing 'text':\n"
                b"java.lang.NullPointerException: Attempt to get length of null array\n",
                exit_code=255,
            )
        if words[1:] == ["keyevent", "KEYCODE_HOME"]:
            self.focused = LAUNCHER
        return ShellResult()


def _window_report(focused: str, window_over: str | None) -> bytes:
    """The lines of ``dumpsys window`` the bridge reads, in a little of the rest of it."""
    package, _, activity = focused.partition("/")
    title = f"{package}/{package}{activity}" if activity.startswith(".") else focused
    window = f"388b3c9 u0 {title}"
    focus = f"31ca038 u0 {window_over}" if window_over is not None else window
    return (
        "WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n"
        f"  mCurrentFocus=Window{{{focus}}}\n"
        f"  mFocusedApp=ActivityRecord{{6dbdd1 u0 {focused} t9}}\n"
        "  mHoldScreenWindow=null\n"
        f"  imeInputTarget in display# 0 Window{{{window}}}\n"
    ).encode()


def _input_method_report(text_field: bool) -> bytes:
    """The input method client's lines of ``dumpsys input_method``, around the one the bridge reads."""
    connection = (
        "RemoteInputConnectionImpl{connection=dzl@450171d finished=false "
        "mParentInputMethodManager.isActive()=true mServedView=dzd{805613a VFED..CL. .F...... "
        "0,0-954,126 #7f0a01fa app:id/open_search_view_edit_text aid=1073741824}}"
        if text_field
        else "null"
    )
    return (
        "Current Input Method Manager state:\n"
        "  mShowRequested=false mShowExplicitlyRequested=false mShowForced=false "
        "mInputShown=false\n"
        " \n"
        "Input method client state for android.view.inputmethod.InputMethodManager@ac641eb:\n"
        "  mActive=true mRestartOnNextWindowFocus=false mBindSequence=6\n"
        f"  mServedInputConnection={connection}\n"
        "  mServedInputConnectionHandler=null\n"
    ).encode()


@dataclass
class FakeServerProvider:
    """A provider whose device is a fake ADB server's: what ``local_emulator`` hands over, without an emulator."""

    host: str
    port: int
    serial: str = SERIAL
    closed: list[str] = field(default_factory=list)
    #: What each ``open()`` was asked for.
    needs: list[DeviceNeeds] = field(default_factory=list)

    @property
    def provider_id(self) -> str:
        return "fake_server"

    def unmet_needs(self, needs: DeviceNeeds) -> list[str]:
        return []

    async def open(self, needs: DeviceNeeds | None = None) -> AndroidProviderSession:
        self.needs.append(needs or DeviceNeeds())
        return AndroidProviderSession(
            session_id="fake-session",
            provider_id=self.provider_id,
            endpoint=AdbEndpoint(
                host=self.host,
                port=self.port,
                kind="server",
                encrypted=False,
                needs_adb_key=False,
            ),
            serial=self.serial,
        )

    async def close(self, session: AndroidProviderSession | None = None) -> None:
        self.closed.append(session.session_id if session is not None else "all")


@asynccontextmanager
async def android_capability(
    adb: FakeAdbServer,
    *,
    fragments: Iterable[str] | None = None,
    settle_max_wait_s: float = 0.0,
    settle_quiet_ms: int = 300,
    query_timeout_s: float = 5.0,
) -> AsyncIterator[AndroidCapability]:
    """An ``AndroidCapability`` set up over *adb*, negotiated to *fragments* (all when ``None``).

    The wait for a still screen is off unless *settle_max_wait_s* says
    otherwise, so a capture is one round trip per source.
    """
    capability = AndroidCapability(
        settle_max_wait_s=settle_max_wait_s,
        settle_quiet_ms=settle_quiet_ms,
        query_timeout_s=query_timeout_s,
    )
    capability.backend = FakeServerProvider(adb.host, adb.port)
    await capability.setup(negotiation=None if fragments is None else frozenset(fragments))
    try:
        yield capability
    finally:
        await capability.teardown()
