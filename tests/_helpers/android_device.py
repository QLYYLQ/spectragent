"""A scripted phone behind the fake ADB server, and the Android capability set up over it.

:class:`ScriptedPhone` answers the commands the capability's bridge sends
(``spectragent/core/capabilities/android/bridge.py``) with output shaped like
the emulator's (API 33, 2026-11-01): a PNG for ``screencap``, the focus lines of
``dumpsys window``, a frame count from ``dumpsys gfxinfo``, ``wm size``,
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
            return ShellResult(stdout=_window_report(self.focused))
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


def _window_report(focused: str) -> bytes:
    """The two lines of ``dumpsys window`` the bridge reads, in a little of the rest of it."""
    package, _, activity = focused.partition("/")
    window = f"{package}/{package}{activity}" if activity.startswith(".") else focused
    return (
        "WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n"
        f"  mCurrentFocus=Window{{388b3c9 u0 {window}}}\n"
        f"  mFocusedApp=ActivityRecord{{6dbdd1 u0 {focused} t9}}\n"
        "  mHoldScreenWindow=null\n"
    ).encode()


@dataclass
class FakeServerProvider:
    """A provider whose device is a fake ADB server's: what ``local_emulator`` hands over, without an emulator."""

    host: str
    port: int
    serial: str = SERIAL
    closed: list[str] = field(default_factory=list)

    @property
    def provider_id(self) -> str:
        return "fake_server"

    async def open(self) -> AndroidProviderSession:
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
