"""The bridge to the device: what the capability asks of it, as Protocols, and an implementation.

``docs/architecture_design.md`` §11.2: the Android package defines its bridge
contracts as Protocols, and the layers below are their implementations. The
capability reaches a device only through :class:`DeviceBridge`: the screen
(:class:`ScreenCapture`), input (:class:`InputInjection`), the foreground app
(:class:`ForegroundApps`), launching apps (:class:`AppLauncher`) and the
device's measured facts (:class:`DeviceFacts`).

:class:`AdbBuiltins` implements all five with adb's built-in commands, each one
``shell,v2`` round trip over the host transport (``adb.py``): ``screencap``,
``input``, ``dumpsys``, ``pm``, ``cmd package``, ``am``, ``wm``, ``getprop`` and
``settings``. A later stage puts the package's own accessibility service and
shell helper behind the same Protocols. This one never enables an
accessibility service, never runs ``uiautomator dump`` (the UI tree comes
later) and never talks to the emulator's gRPC. Medians measured on the
emulator over the host protocol (API 33, 2026-11-01): ``screencap -p`` about
0.7 s (the emulator's gRPC returns a PNG of the same screen about 7 times
faster), ``input tap`` 14 ms, ``dumpsys window`` 18 ms, ``getprop`` 8 ms,
``dumpsys input_method`` about 0.1 s (550 KB, most of it the keyboard's own
state).

A command that fails raises :class:`BridgeError` with the command's own words.
A fact the device does not report (no focused app, an unset input method) is
``None``; transport failures raise :class:`~spectragent.core.capabilities.android.adb.AdbError`.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from spectragent.core.capabilities.android.adb import AdbClient, ShellResult

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: A package name as Android writes it; nothing else reaches a command line.
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")
_KEYCODE = re.compile(r"KEYCODE_[A-Z0-9_]+")
_FEATURE = re.compile(r"[A-Za-z0-9_.]+")
#: ``mFocusedApp=ActivityRecord{6dbdd1 u0 com.android.settings/.Settings t9}``.
_FOCUSED_APP = re.compile(r"mFocusedApp=ActivityRecord\{\S+ u\d+ (?P<component>[^\s}]+)")
#: ``mCurrentFocus=Window{388b3c9 u0 com.android.settings/com.android.settings.Settings}``:
#: the window (its id, user and title) and its title.
_CURRENT_FOCUS = re.compile(r"mCurrentFocus=Window\{(?P<window>\S+ u\d+ (?P<title>[^}]*))\}")
#: ``imeInputTarget in display# 0 Window{388b3c9 u0 com.android.settings/...}``: the
#: window the input method serves. Android 11 names it ``mInputMethodInputTarget``, as
#: its platform source prints it (not measured here).
_IME_INPUT_TARGET = re.compile(
    r"(?:imeInputTarget|mInputMethodInputTarget) in display# \d+ Window\{(?P<window>[^}]*)\}"
)
#: ``mServedInputConnection=null``, or the input connection the input method client's
#: focused view holds (``RemoteInputConnectionImpl{...}``).
_SERVED_CONNECTION = re.compile(r"^ +mServedInputConnection=(?P<connection>\S+)", re.MULTILINE)
_FRAMES = re.compile(r"Total frames rendered: (?P<frames>\d+)")
_SIZE = re.compile(r"(?P<kind>Physical|Override) size: (?P<width>\d+)x(?P<height>\d+)")


class BridgeError(Exception):
    """A device command failed, or answered in a shape it never does."""


@dataclass(frozen=True)
class ForegroundApp:
    """What the device shows in front, as its window manager reports it.

    Attributes:
        package: The focused app's package; ``None`` when no app has focus.
        activity: That app's focused activity, its class name in full.
        window: The window that has input focus: an app's activity window, or
            a system one (``NotificationShade``, a permission dialog of another
            package); ``None`` when no window has focus.
    """

    package: str | None
    activity: str | None
    window: str | None


@dataclass(frozen=True)
class TextFocus:
    """Whether text typed now has a text field to go into, as the device reports it.

    Attributes:
        window: The window that has input focus, by its title (as
            :attr:`ForegroundApp.window`); ``None`` when no window has focus.
        text_field: Whether a text field of that window has focus: the input
            method serves the window, and the view with focus in it holds an
            input connection. ``False`` when no window has focus; ``None``
            when the device did not say.
    """

    window: str | None
    text_field: bool | None


@runtime_checkable
class ScreenCapture(Protocol):
    """The screen, as an image."""

    @property
    def producer(self) -> str:
        """What made the images, recorded with each one (``screencap``)."""
        ...

    async def capture(self) -> bytes:
        """The whole screen as PNG bytes, at the size taps are given in."""
        ...


@runtime_checkable
class InputInjection(Protocol):
    """Touches, text and keys, in screen pixels."""

    async def tap(self, x: int, y: int) -> None: ...

    async def swipe(
        self, start: tuple[int, int], end: tuple[int, int], *, duration_ms: int
    ) -> None:
        """Touch *start*, move to *end* over *duration_ms*, lift; a long press when they are equal."""
        ...

    async def type_text(self, text: str) -> None:
        """Type *text* into the focused field."""
        ...

    async def clear_focused_field(self) -> None:
        """Select everything in the focused field and delete it."""
        ...

    async def press_key(self, keycode: str) -> None:
        """Press and release one key, named as Android names it (``KEYCODE_BACK``)."""
        ...


@runtime_checkable
class ForegroundApps(Protocol):
    """What is in front, whether it is still drawing, and whether a text field in it has focus."""

    async def foreground(self) -> ForegroundApp: ...

    async def frames_drawn(self, package: str) -> int | None:
        """How many frames *package* has rendered so far; ``None`` when it reports none."""
        ...

    async def text_focus(self) -> TextFocus:
        """Whether text typed now would go into a text field, and which window has focus."""
        ...


@runtime_checkable
class AppLauncher(Protocol):
    """Starting an app by its package."""

    async def is_installed(self, package: str) -> bool: ...

    async def launch(self, package: str) -> None:
        """Start *package*'s launcher activity and wait until it is shown."""
        ...


@runtime_checkable
class DeviceFacts(Protocol):
    """Facts about the device, measured on it, never inferred from the host."""

    async def screen_size(self) -> tuple[int, int]: ...

    async def fingerprint(self) -> str | None: ...

    async def input_method(self) -> str | None: ...

    async def has_feature(self, feature: str) -> bool | None: ...


@dataclass(frozen=True)
class DeviceBridge:
    """Everything the capability asks of one device, one implementation per role."""

    screen: ScreenCapture
    input: InputInjection
    apps: ForegroundApps
    launcher: AppLauncher
    facts: DeviceFacts

    @classmethod
    def over_adb(cls, client: AdbClient, serial: str) -> DeviceBridge:
        """Every role on adb's built-in commands (:class:`AdbBuiltins`)."""
        builtins = AdbBuiltins(client, serial)
        return cls(builtins, builtins, builtins, builtins, builtins)


class AdbBuiltins:
    """Every bridge role on adb's built-in commands, one ``shell,v2`` round trip each."""

    def __init__(self, client: AdbClient, serial: str) -> None:
        self._client = client
        self._serial = serial

    @property
    def producer(self) -> str:
        return "screencap"

    async def capture(self) -> bytes:
        image = await self._check("screencap -p")
        if not image.startswith(_PNG_SIGNATURE):
            raise BridgeError(f"screencap -p answered {len(image)} bytes that are not a PNG")
        return image

    async def tap(self, x: int, y: int) -> None:
        await self._check(f"input tap {int(x)} {int(y)}")

    async def swipe(
        self, start: tuple[int, int], end: tuple[int, int], *, duration_ms: int
    ) -> None:
        (x1, y1), (x2, y2) = start, end
        await self._check(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    async def type_text(self, text: str) -> None:
        """Type *text* with ``input text``, which knows only the virtual keyboard's characters.

        ``input text`` turns each character into the key events of Android's
        virtual keyboard map, so letters, digits, ASCII punctuation and spaces
        type and a character outside the map (Chinese, emoji) fails the whole
        call (it exited 255 with a NullPointerException, measured). It also
        reads ``%s`` as a space, so that pair cannot be typed literally. Other
        typing backends come with the device-side service of a later stage.
        """
        if not text:
            return
        result = await self._run(f"input text {shlex.quote(text)}")
        if result.exit_code != 0:
            raise BridgeError(
                f"input text failed (exit {result.exit_code}): it types only characters on "
                f"Android's virtual keyboard map, such as ASCII letters, digits, punctuation "
                f"and spaces ({_first_line(result)})"
            )

    async def clear_focused_field(self) -> None:
        await self._check(
            "input keycombination KEYCODE_CTRL_LEFT KEYCODE_A && input keyevent KEYCODE_DEL"
        )

    async def press_key(self, keycode: str) -> None:
        if not _KEYCODE.fullmatch(keycode):
            raise ValueError(f"not an Android key code: {keycode!r}")
        await self._check(f"input keyevent {keycode}")

    async def foreground(self) -> ForegroundApp:
        report = (await self._check("dumpsys window")).decode(errors="replace")
        return parse_foreground(report)

    async def frames_drawn(self, package: str) -> int | None:
        report = (await self._check(f"dumpsys gfxinfo {_package(package)}")).decode(
            errors="replace"
        )
        found = _FRAMES.search(report)
        return int(found["frames"]) if found else None

    async def text_focus(self) -> TextFocus:
        windows = (await self._check("dumpsys window")).decode(errors="replace")
        methods = (await self._check("dumpsys input_method")).decode(errors="replace")
        return parse_text_focus(windows, methods)

    async def is_installed(self, package: str) -> bool:
        result = await self._run(f"pm path {_package(package)}")
        if result.exit_code == 0 and result.stdout.startswith(b"package:"):
            return True
        if result.exit_code == 1:
            return False
        raise BridgeError(
            f"pm path {package} failed (exit {result.exit_code}): {_first_line(result)}"
        )

    async def launch(self, package: str) -> None:
        resolved = await self._check(
            "cmd package resolve-activity --brief -a android.intent.action.MAIN "
            f"-c android.intent.category.LAUNCHER {_package(package)}"
        )
        components = [line.strip() for line in resolved.decode().splitlines() if "/" in line]
        if not components:
            raise BridgeError(f"{package} has no launcher activity")
        started = (await self._check(f"am start -W -n {shlex.quote(components[-1])}")).decode()
        refused = any(line.startswith("Error") for line in started.splitlines())
        if refused or "Status: ok" not in started:
            raise BridgeError(f"am start of {components[-1]} did not succeed: {started.strip()}")

    async def screen_size(self) -> tuple[int, int]:
        report = (await self._check("wm size")).decode()
        sizes = {
            found["kind"]: (int(found["width"]), int(found["height"]))
            for found in _SIZE.finditer(report)
        }
        size = sizes.get("Override") or sizes.get("Physical")
        if size is None:
            raise BridgeError(f"wm size reported no size: {report.strip()!r}")
        return size

    async def fingerprint(self) -> str | None:
        return (await self._check("getprop ro.build.fingerprint")).decode().strip() or None

    async def input_method(self) -> str | None:
        result = await self._run("settings get secure default_input_method")
        value = result.stdout.decode().strip()
        return value if result.exit_code == 0 and value not in ("", "null") else None

    async def has_feature(self, feature: str) -> bool | None:
        if not _FEATURE.fullmatch(feature):
            raise ValueError(f"not an Android feature name: {feature!r}")
        answer = (await self._run(f"pm has-feature {feature}")).stdout.decode().strip()
        return {"true": True, "false": False}.get(answer)

    async def _run(self, command: str) -> ShellResult:
        return await self._client.shell(self._serial, command)

    async def _check(self, command: str) -> bytes:
        """Run *command* and return its stdout; a non-zero exit raises :class:`BridgeError`."""
        result = await self._run(command)
        if result.exit_code != 0:
            program = " ".join(command.split()[:2])
            raise BridgeError(f"{program} failed (exit {result.exit_code}): {_first_line(result)}")
        return result.stdout


def parse_foreground(report: str) -> ForegroundApp:
    """The focused app and window in a ``dumpsys window`` report; the first display's when several."""
    app = _FOCUSED_APP.search(report)
    focus = _CURRENT_FOCUS.search(report)
    package = activity = None
    if app is not None:
        package, _, name = app["component"].partition("/")
        activity = f"{package}{name}" if name.startswith(".") else name
    return ForegroundApp(
        package=package, activity=activity or None, window=focus["title"] if focus else None
    )


def parse_text_focus(windows: str, methods: str) -> TextFocus:
    """Whether a text field has focus, from a ``dumpsys window`` and a ``dumpsys input_method`` report.

    A text field has focus when the view with focus in the input method's
    client holds an input connection (its ``mServedInputConnection`` is not
    ``null``), and that client's window is the one with focus: the window that
    has focus (``mCurrentFocus``) is the one the input method serves
    (``imeInputTarget``). Each alone misleads, measured on the emulator (API 33,
    2026-11-01): on Settings' main page and the home screen the input method
    serves the focused window and its view holds no connection; once a tap
    opens Settings' search, its field holds one, keyboard shown or hidden; with
    the notification shade pulled over that field the shade has focus, the
    input method still serves the search window, the field still holds its
    connection, and ``input text`` went nowhere. The first window and client of
    each report count: the default display's, the input method's current
    client.

    ``text_field`` is ``None`` when a report leaves the answer open: the input
    method's names no served connection (its client did not answer), or the
    window manager's names no window the input method serves while there is a
    connection. No connection is an answer without the window: no field of the
    input method's client takes the text.
    """
    focus = _CURRENT_FOCUS.search(windows)
    if focus is None:
        return TextFocus(window=None, text_field=False)
    window = focus["title"]
    served = _SERVED_CONNECTION.search(methods)
    if served is None:
        return TextFocus(window=window, text_field=None)
    if served["connection"] == "null":
        return TextFocus(window=window, text_field=False)
    target = _IME_INPUT_TARGET.search(windows)
    if target is None:
        return TextFocus(window=window, text_field=None)
    return TextFocus(window=window, text_field=target["window"] == focus["window"])


def _package(package: str) -> str:
    if not _PACKAGE.fullmatch(package):
        raise ValueError(f"not an Android package name: {package!r}")
    return package


def _first_line(result: ShellResult) -> str:
    """The first non-empty line the command wrote, stderr first, for an error message."""
    for stream in (result.stderr, result.stdout):
        for line in stream.decode(errors="replace").splitlines():
            if line.strip():
                return line.strip()
    return "no output"
