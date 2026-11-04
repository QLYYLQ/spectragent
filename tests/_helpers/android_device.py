"""A scripted phone behind the fake ADB server, and the Android capability set up over it.

:class:`ScriptedPhone` is one phone with both of its sides:

- **adb's own commands**, answered with output shaped like the emulator's (API
  33, 2026-11-01): the focus lines of ``dumpsys window``, the input method's
  latest start of input and its client's served connection from ``dumpsys
  input_method``, a frame count from
  ``dumpsys gfxinfo``, ``wm size``, ``getprop``, ``settings``, ``pm``,
  ``cmd package``, ``am start``, and the wall clock: ``date +%s%3N`` and
  ``cmd alarm set-time``;
- **the device side** (:class:`~tests._helpers.fake_device_side.FakeDeviceSide`):
  the APK's install, the shell helper, the accessibility service, and what
  they do for a run: screenshots, timed strokes, keys, text, the node with
  input focus, the window forest (:data:`FOREST` unless a test gives another)
  and the events the service pushes.

Any other command is answered as ``sh`` does (exit 127), so a capability that
starts sending a new command fails a test until this phone learns it. It keeps
every input its device side received, in order (:attr:`ScriptedPhone.inputs`).

:func:`android_capability` is an ``AndroidCapability`` set up over a
:class:`~tests._helpers.fake_adb.FakeAdbServer` holding such a phone
(:func:`phone_server`): a provider hands it the fake server's endpoint, as
``local_emulator`` hands it the run's private adb server.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import struct
import tempfile
import time
import zlib
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from spectragent.core.capabilities.android.backends.base import (
    AdbEndpoint,
    AndroidProviderSession,
    DeviceNeeds,
)
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.device.apk import (
    TEST_KEY_CERTIFICATE_SHA256,
    build_directory,
)
from spectragent.core.capabilities.android.device.channel import PROTOCOL_VERSION, JsonObject
from spectragent.core.capabilities.android.input._registrar import (
    AndroidTapInjector,
    AndroidTypingBackend,
)
from spectragent.core.capabilities.android.input.tap import ShellInjectionInjector
from spectragent.core.capabilities.android.input.text import SetTextBackend
from spectragent.core.capabilities.android.ui_tree._registrar import (
    AndroidUITreeCleaner,
    AndroidUITreeSerializer,
)
from spectragent.core.capabilities.android.ui_tree.cleaning import AndroidWorldCleaner
from spectragent.core.capabilities.android.ui_tree.serializers import M3ASerializer
from tests._helpers.fake_adb import FakeAdbServer, ShellResult
from tests._helpers.fake_device_side import Answer, Failure, FakeDeviceSide, Method

SERIAL = "emulator-5554"
LAUNCHER = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
FINGERPRINT = "google/sdk_gphone64_arm64/emu64a:13/TE1A.240213.009/12342917:userdebug/dev-keys"
INPUT_METHOD = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
#: The APK the fake phone installs; its sidecar describes it (:func:`fake_service_apk`).
APK_BYTES = b"PK\x03\x04 the device-side APK " + bytes(range(256)) * 64
_LAUNCHABLE = "-a android.intent.action.MAIN -c android.intent.category.LAUNCHER"


def settings_forest(*rows: tuple[str, tuple[int, int, int, int]]) -> JsonObject:
    """A window forest in the wire form (``device/forest.py``): Settings, its root, and *rows*.

    Each row is a clickable text view, laid out (drawing order 1) and
    important, given as its text and its ``(left, top, right, bottom)``.
    """
    nodes: list[JsonValue] = [
        {
            "bounds_in_screen": {"right": 1080, "bottom": 2400},
            "class_name": "android.widget.FrameLayout",
            "package_name": "com.android.settings",
            "window_id": 7,
            "is_visible_to_user": True,
            "child_ids": list(range(1, len(rows) + 1)),
            "is_important": True,
        }
    ]
    for unique_id, (text, (left, top, right, bottom)) in enumerate(rows, start=1):
        nodes.append(
            {
                "unique_id": unique_id,
                "bounds_in_screen": {"left": left, "top": top, "right": right, "bottom": bottom},
                "class_name": "android.widget.TextView",
                "package_name": "com.android.settings",
                "text": text,
                "window_id": 7,
                "is_clickable": True,
                "is_enabled": True,
                "is_visible_to_user": True,
                "depth": 1,
                "drawing_order": 1,
                "is_important": True,
            }
        )
    window: JsonObject = {
        "bounds_in_screen": {"right": 1080, "bottom": 2400},
        "id": 7,
        "title": "Settings",
        "window_type": "TYPE_APPLICATION",
        "is_active": True,
        "is_focused": True,
        "tree": {"nodes": nodes},
    }
    return {"windows": [window]}


#: The window forest the scripted phone's service answers ``tree`` with:
#: Settings with two rows of its list.
FOREST = settings_forest(
    ("Network & internet", (0, 300, 1080, 450)), ("Connected devices", (0, 450, 1080, 600))
)


def png(width: int, height: int) -> bytes:
    """The PNG signature and IHDR chunk: enough for every reader of an image's type and size."""
    ihdr = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr + struct.pack(">I", zlib.crc32(ihdr))


def fake_service_apk(directory: Path) -> Path:
    """An APK of :data:`APK_BYTES` with the sidecar the build writes beside it, under *directory*.

    In a directory named by its SHA-256, as the build keeps it.
    """
    sha256 = hashlib.sha256(APK_BYTES).hexdigest()
    path = directory / build_directory(sha256) / "spectragent-device.apk"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(APK_BYTES)
    sidecar = {
        "format": "spectragent-device-apk-2",
        "package": "org.spectragent.device",
        "version_code": 1,
        "version_name": "1",
        "protocol": PROTOCOL_VERSION,
        "accessibility_tool": False,
        "sha256": sha256,
        "signing_certificate_sha256": TEST_KEY_CERTIFICATE_SHA256,
    }
    path.with_suffix(".json").write_text(json.dumps(sidecar))
    return path


@dataclass
class ScriptedPhone:
    """A phone's answers; change the attributes to change what it reports next.

    Attributes:
        width: Its screen's width in pixels (a Pixel 6 by default).
        height: Its height.
        sdk_int: Its API level, as its device-side programs report it.
        focused: The activity in front, as ``package/.Activity``.
        frames: How many frames the app in front has rendered; ``None``: its
            process reports none (it has not started).
        launchable: Installed packages with their launcher activities.
        labels: The label the home screen shows for each of those packages;
            one it leaves out is labelled with its package name.
        more_launcher_apps: Further launcher activities the service lists,
            each ``{"label", "package", "activity"}``: a package with two.
        launcher_delay_s: How long the service takes to answer
            ``launcher_apps``: a cold device's package manager can be slow.
        helper_screenshots: Whether the helper takes screenshots (API 31-33);
            otherwise it answers ``unsupported`` and the service takes them.
        screenshot_fails: Every screenshot fails from now on.
        screen: The image a screenshot carries; ``None``: :func:`png`'s header
            alone, which says the type and the size and decodes to nothing.
        text_field: A text field of the activity in front has focus: the input
            method serves its window, and the view with focus holds an input
            connection. ``None``: the input method's client does not answer, so
            its report names no connection.
        input_started: The number of the input method's latest start of input
            (``StartInput #N`` in its report): raise it when focus moves to
            another field, as the input method then starts input again.
        window_over: A window over the activity that has focus instead of it,
            by its title (``NotificationShade``): the input method still serves
            the activity's window, as the emulator reported.
        field_bounds: Where the node with input focus is, as the service
            reports it while :attr:`text_field` is true: ``(left, top, right,
            bottom)``; ``None``: the service names no focused node.
        field_node: That node's hash, as the service reports it: another
            number is another field.
        field_editable: Whether that node takes text; ``False``: a node that
            does not, as a WebView's own node is before its page gives the
            field focus. ``set_text`` then finds no field, as on the device;
            ``commit_text`` types through the input connection all the same.
        text_failure: The error code ``set_text`` and ``commit_text`` answer
            with instead of entering text, whatever has focus; ``None``: none.
        drawn_bounds: Where the field that node belongs to is, as drawn, when
            the service is asked (``field_bounds``): ``None``, the node's own
            bounds, a field with nothing around it.
        drawn_bounds_given: Whether the service gives those bounds when asked;
            ``False``: it gave up walking the tree.
        label_bounds: Where the view that labels that node is, given with the
            field's bounds; ``None``: no view labels it.
        top_window: The window on top at the point a read gives with the
            field's bounds: 7 is the focused node's own window.
        focus_reads: Whether each read of the focus asked for the field's
            bounds, in order.
        on_tap: Run when a stroke arrives, after it is recorded: a tap that
            moves focus, or opens a screen.
        forest: The window forest the service reports for ``tree``, in the
            wire form (:data:`FOREST` unless changed).
        tree_reads: The node limit each ``tree`` request asked for, in order.
        tree_delay_s: How long the service takes to answer ``tree``.
        typing_fails: The error code the helper's ``type_text`` answers with
            from now on (``typed_partly``: a key failed after others went
            in); ``None``: it types.
        unbinds_after_reads: How many reads of the bootstrap provider's health
            the service stays bound after it left the setting
            (:class:`FakeDeviceSide`); 0: at once.
        inputs: Every input the device side received, in order, setup's test
            tap first: ``"<program> stroke (x,y)@t ..."``, ``"helper key
            KEYCODE_..."``, ``"helper type_text '...' replace|at the cursor"``,
            ``"service set_text '...' replace|at the cursor"``, ``"service
            commit_text '...' replace|at the cursor"``.
        clock_ms: The wall clock when the phone booted, in ms since the epoch
            (AndroidWorld's 2023-10-15 15:34 UTC); it runs on from there, and
            ``cmd alarm set-time`` moves it.
        clock_fails: ``date`` fails from now on.
        clock_sets: Every time ``cmd alarm set-time`` was given, in order.
        global_settings: What ``settings put global`` wrote, by name.
    """

    width: int = 1080
    height: int = 2400
    sdk_int: int = 33
    focused: str = LAUNCHER
    frames: int | None = 36
    launchable: dict[str, str] = field(
        default_factory=lambda: {"com.android.settings": "com.android.settings/.Settings"}
    )
    labels: dict[str, str] = field(default_factory=lambda: {"com.android.settings": "Settings"})
    more_launcher_apps: list[JsonValue] = field(default_factory=list)
    launcher_delay_s: float = 0.0
    helper_screenshots: bool = True
    screenshot_fails: bool = False
    screen: bytes | None = None
    text_field: bool | None = False
    input_started: int = 4
    window_over: str | None = None
    field_bounds: tuple[int, int, int, int] | None = (0, 100, 1080, 250)
    field_node: int = 1
    field_editable: bool = True
    text_failure: str | None = None
    drawn_bounds: tuple[int, int, int, int] | None = None
    drawn_bounds_given: bool = True
    label_bounds: tuple[int, int, int, int] | None = None
    top_window: int = 7
    focus_reads: list[bool] = field(default_factory=list)
    on_tap: Callable[[], None] | None = None
    forest: JsonObject = field(default_factory=lambda: dict(FOREST))
    tree_reads: list[JsonValue] = field(default_factory=list)
    tree_delay_s: float = 0.0
    typing_fails: str | None = None
    unbinds_after_reads: int = 0
    inputs: list[str] = field(default_factory=list)
    clock_ms: int = 1_697_384_040_000
    clock_fails: bool = False
    clock_sets: list[int] = field(default_factory=list)
    global_settings: dict[str, str] = field(default_factory=dict)
    side: FakeDeviceSide = field(init=False)
    _booted: float = field(init=False, default_factory=time.monotonic)
    #: When the clock was last set (or the phone booted), on the host's monotonic clock.
    _clock_since: float = field(init=False, default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.side = FakeDeviceSide(
            sdk_int=self.sdk_int,
            unbinds_after_reads=self.unbinds_after_reads,
            service_methods={
                "ping": self._service_ping,
                "screenshot": self._screenshot("accessibility_service"),
                "gesture": self._stroke("service"),
                "focus": self._focus,
                "tree": self._tree,
                "set_text": self._text("set_text"),
                "commit_text": self._text("commit_text"),
                "launcher_apps": self._launcher_apps,
            },
            helper_methods={
                "ping": self._helper_ping,
                "screenshot": self._screenshot("shell_helper"),
                "gesture": self._stroke("helper"),
                "key": self._key,
                "type_text": self._type_text,
            },
            fallback=self._adb,
        )

    @property
    def uptime_ms(self) -> int:
        """The device's uptime: the clock its events and pings are on."""
        return 100_000 + round((time.monotonic() - self._booted) * 1000)

    @property
    def now_ms(self) -> int:
        """The device's wall clock: :attr:`clock_ms`, run on since it was last set."""
        return self.clock_ms + round((time.monotonic() - self._clock_since) * 1000)

    def emit(self, event_type: str = "TYPE_WINDOW_CONTENT_CHANGED") -> int:
        """The service receives an accessibility event now; returns its ``seq``."""
        return self.side.service.emit(
            type=event_type,
            time_ms=self.uptime_ms,
            package_name=self.focused.partition("/")[0],
            class_name="android.widget.FrameLayout",
            window_id=7,
            content_change_types=1,
            window_changes=0,
            text=[],
            content_description=None,
            before_text=None,
            from_index=-1,
            to_index=-1,
            item_count=-1,
            added_count=-1,
            removed_count=-1,
            scroll_delta_x=-1,
            scroll_delta_y=-1,
        )

    def service_calls(self, method: str) -> int:
        """How many times the service was asked *method*."""
        return sum(1 for name, _ in self.side.service.calls if name == method)

    def helper_calls(self, method: str) -> int:
        """How many times a helper was asked *method*."""
        return sum(1 for helper in self.side.helpers for name, _ in helper.calls if name == method)

    # ── the device side ───────────────────────────────────────────────

    async def _service_ping(self, params: JsonObject) -> Answer:
        return Answer({"uptime_ms": self.uptime_ms, "next_seq": 0})

    async def _launcher_apps(self, params: JsonObject) -> Answer:
        await asyncio.sleep(self.launcher_delay_s)
        apps: list[JsonValue] = []
        for package, component in self.launchable.items():
            activity = component.partition("/")[2]
            apps.append(
                {
                    "label": self.labels.get(package, package),
                    "package": package,
                    "activity": f"{package}{activity}" if activity.startswith(".") else activity,
                }
            )
        return Answer({"apps": [*apps, *self.more_launcher_apps]})

    async def _helper_ping(self, params: JsonObject) -> Answer:
        return Answer({"uptime_ms": self.uptime_ms})

    def _screenshot(self, producer: str) -> Method:
        async def screenshot(params: JsonObject) -> Answer:
            if producer == "shell_helper" and not self.helper_screenshots:
                raise Failure("unsupported", "the helper's screenshot is written for API 31 to 33")
            if self.screenshot_fails:
                raise Failure("screenshot_failed", "the capture failed")
            waited = 0 if producer == "shell_helper" else 40
            result: JsonObject = {
                "width": self.width,
                "height": self.height,
                "format": "png",
                "capture_ms": 17,
                "encode_ms": 85,
                "waited_ms": waited,
            }
            return Answer(result, self.screen or png(self.width, self.height))

        return screenshot

    def _stroke(self, program: str) -> Method:
        async def gesture(params: JsonObject) -> Answer:
            if program == "service" and not self.side.faketouch:
                raise Failure("gesture_cancelled", "the system cancelled the gesture")
            strokes = params["strokes"]
            assert isinstance(strokes, list)
            for stroke in strokes:
                assert isinstance(stroke, dict) and isinstance(stroke["points"], list)
                points = " ".join(
                    f"({point[0]},{point[1]})@{point[2]}"
                    for point in stroke["points"]
                    if isinstance(point, list)
                )
                self.inputs.append(f"{program} stroke {points}")
            if self.on_tap is not None:
                self.on_tap()
            return Answer({"elapsed_ms": 55})

        return gesture

    async def _key(self, params: JsonObject) -> Answer:
        code = params["code"]
        self.inputs.append(f"helper key {code}")
        if code == "KEYCODE_HOME":
            self.focused = LAUNCHER
        return Answer()

    async def _type_text(self, params: JsonObject) -> Answer:
        """Key events go wherever focus is. This key map keeps to printable ASCII, Enter and Tab;
        the emulator's also reaches ç, Ç and ß through Alt (2026-11-03)."""
        text = params["text"]
        assert isinstance(text, str)
        keyless = sorted({c for c in text if not (" " <= c <= "~" or c in "\n\t")})
        if keyless:
            named = ", ".join(f"{c!r} (U+{ord(c):04X})" for c in keyless)
            raise Failure("unmappable_text", f"the device's key map has no key for {named}")
        how = "replace" if params.get("replace") else "at the cursor"
        if self.typing_fails is not None:
            self.inputs.append(f"helper type_text {text!r} {how}, failed partway")
            raise Failure(self.typing_fails, "2 key events went in before one failed")
        self.inputs.append(f"helper type_text {text!r} {how}")
        return Answer()

    async def _tree(self, params: JsonObject) -> Answer:
        self.tree_reads.append(params.get("max_nodes"))
        await asyncio.sleep(self.tree_delay_s)
        windows = self.forest.get("windows")
        count = sum(
            len(nodes)
            for window in (windows if isinstance(windows, list) else [])
            if isinstance(window, dict)
            and isinstance(tree := window.get("tree"), dict)
            and isinstance(nodes := tree.get("nodes"), list)
        )
        return Answer(
            {"forest": self.forest, "node_count": count, "omitted_offscreen": 0, "capture_ms": 12}
        )

    async def _focus(self, params: JsonObject) -> Answer:
        asked = params.get("field_bounds") is True
        self.focus_reads.append(asked)
        if not self.text_field or self.field_bounds is None:
            return Answer({"focused": None})
        left, top, right, bottom = self.field_bounds
        drawn: JsonValue = None
        if asked and self.drawn_bounds_given:
            d_left, d_top, d_right, d_bottom = self.drawn_bounds or self.field_bounds
            drawn = {"left": d_left, "top": d_top, "right": d_right, "bottom": d_bottom}
        label: JsonValue = None
        if asked and self.label_bounds is not None:
            l_left, l_top, l_right, l_bottom = self.label_bounds
            label = {"left": l_left, "top": l_top, "right": l_right, "bottom": l_bottom}
        node: dict[str, JsonValue] = {
            "bounds_in_screen": {"left": left, "top": top, "right": right, "bottom": bottom},
            "field_bounds_in_screen": drawn,
            "label_bounds_in_screen": label,
            "window_at_point": self.top_window if asked and "x" in params else None,
            "is_editable": self.field_editable,
            "window_id": 7,
            "node_hash": self.field_node,
            "package_name": self.focused.partition("/")[0],
            "class_name": "android.widget.EditText",
            "view_id_resource_name": "android:id/search_src_text",
        }
        return Answer({"focused": node})

    def _text(self, method: str) -> Method:
        async def enter(params: JsonObject) -> Answer:
            if self.text_failure is not None:
                raise Failure(self.text_failure, "the device failed while entering the text")
            if not self.text_field or (method == "set_text" and not self.field_editable):
                raise Failure("no_focused_field", "no editable field has input focus")
            how = "replace" if params.get("replace") else "at the cursor"
            self.inputs.append(f"service {method} {params['text']!r} {how}")
            package = self.focused.partition("/")[0]
            if method == "commit_text":
                return Answer({"package_name": package})
            return Answer(
                {
                    "package_name": package,
                    "class_name": "android.widget.EditText",
                    "view_id_resource_name": "android:id/search_src_text",
                }
            )

        return enter

    # ── adb's own commands ────────────────────────────────────────────

    async def __call__(self, command: str) -> ShellResult:
        """The phone as a fake ADB server's device: its device side first, then adb's commands."""
        return await self.side.device(command)

    async def _adb(self, command: str) -> ShellResult:
        words = command.split()
        package = self.focused.partition("/")[0]
        if command == "dumpsys window":
            return ShellResult(stdout=_window_report(self.focused, self.window_over))
        if command == "dumpsys input_method":
            return ShellResult(stdout=_input_method_report(self.text_field, self.input_started))
        if words[:2] == ["dumpsys", "gfxinfo"]:
            if words[2] != package or self.frames is None:
                return ShellResult(stdout=f"No process found for: {words[2]}\n".encode())
            return ShellResult(stdout=f"Total frames rendered: {self.frames}\n".encode())
        if command == "wm size":
            return ShellResult(stdout=f"Physical size: {self.width}x{self.height}\n".encode())
        if command == "getprop ro.build.fingerprint":
            return ShellResult(stdout=f"{FINGERPRINT}\n".encode())
        if command == "date +%s%3N":
            if self.clock_fails:
                return ShellResult(stderr=b"date: bad clock\n", exit_code=1)
            return ShellResult(stdout=f"{self.now_ms}\n".encode())
        if words[:3] == ["cmd", "alarm", "set-time"]:
            self.clock_ms, self._clock_since = int(words[3]), time.monotonic()
            self.clock_sets.append(self.clock_ms)
            return ShellResult()
        if words[:3] == ["settings", "put", "global"]:
            self.global_settings[words[3]] = words[4]
            return ShellResult()
        if command == "settings get secure default_input_method":
            return ShellResult(stdout=f"{INPUT_METHOD}\n".encode())
        if command == "pm list packages":
            listed = "".join(f"package:{package}\n" for package in self.launchable)
            return ShellResult(stdout=listed.encode())
        if command.startswith(f"cmd package resolve-activity --brief {_LAUNCHABLE} "):
            component = self.launchable[words[-1]]
            return ShellResult(stdout=f"priority=0 isDefault=true\n{component}\n".encode())
        if words[:4] == ["am", "start", "-W", "-n"]:
            self.focused = shlex.split(command)[4]
            return ShellResult(
                stdout=f"Starting: Intent {{ cmp={self.focused} }}\nStatus: ok\n".encode()
            )
        name = words[0] if words else command
        return ShellResult(
            stderr=f"/system/bin/sh: {name}: inaccessible or not found\n".encode(), exit_code=127
        )


def phone_server(phone: ScriptedPhone) -> FakeAdbServer:
    """A fake ADB server holding *phone*, its device side's streams and sockets included."""
    return FakeAdbServer(
        {SERIAL: phone}, streams={SERIAL: phone.side.streams}, sockets={SERIAL: phone.side.sockets}
    )


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


def _input_method_report(text_field: bool | None, input_started: int) -> bytes:
    """The lines of ``dumpsys input_method`` the bridge reads, in a little of the rest of it.

    The input method's latest start of input, then its client's connection.
    """
    connection = (
        "RemoteInputConnectionImpl{connection=dzl@450171d finished=false "
        "mParentInputMethodManager.isActive()=true mServedView=dzd{805613a VFED..CL. .F...... "
        "0,0-954,126 #7f0a01fa app:id/open_search_view_edit_text aid=1073741824}}"
        if text_field
        else "null"
    )
    client = (
        # What the platform source prints when the client's dump does not come back.
        "Timeout waiting for dump\n"
        if text_field is None
        else "  mActive=true mRestartOnNextWindowFocus=false mBindSequence=6\n"
        f"  mServedInputConnection={connection}\n"
        "  mServedInputConnectionHandler=null\n"
    )
    return (
        "Current Input Method Manager state:\n"
        "  mShowRequested=false mShowExplicitlyRequested=false mShowForced=false "
        "mInputShown=false\n"
        "  mStartInputHistory:\n"
        f"   StartInput #{input_started}:\n"
        "    time=2023-10-15 15:34:09.804 (timestamp=65789) reason=APP_CALLED_RESTART_INPUT_API "
        "restarting=true\n"
        "  mSoftInputShowHideHistory:\n"
        " \n"
        "Input method client state for android.view.inputmethod.InputMethodManager@ac641eb:\n"
        + client
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
    #: The API level it reports before the device is up; ``None``: it cannot say.
    level: int | None = None

    @property
    def provider_id(self) -> str:
        return "fake_server"

    def unmet_needs(self, needs: DeviceNeeds) -> list[str]:
        return []

    def api_level(self) -> int | None:
        return self.level

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


def unset_capability() -> AndroidCapability:
    """An ``AndroidCapability`` that is never set up, for what it declares; its APK path is never read."""
    return AndroidCapability(service_apk="never-read.apk")


def configured(
    capability: AndroidCapability,
    adb: FakeAdbServer,
    *,
    tap_injector: AndroidTapInjector | None = None,
    typing_backend: AndroidTypingBackend | None = None,
    ui_tree_cleaner: AndroidUITreeCleaner | None = None,
    ui_tree_serializer: AndroidUITreeSerializer | None = None,
) -> FakeServerProvider:
    """Give *capability* a provider of *adb*'s phone and the strategies a run names; returns the provider.

    The tap injector is the shell helper's, the typing backend ``set_text``,
    the UI tree's cleaner ``androidworld`` and its serializer ``m3a``, unless given.
    """
    provider = FakeServerProvider(adb.host, adb.port)
    capability.backend = provider
    capability.tap_injector = tap_injector or ShellInjectionInjector()
    capability.typing_backend = typing_backend or SetTextBackend()
    capability.ui_tree_cleaner = ui_tree_cleaner or AndroidWorldCleaner()
    capability.ui_tree_serializer = ui_tree_serializer or M3ASerializer()
    return provider


@asynccontextmanager
async def android_capability(
    adb: FakeAdbServer,
    *,
    fragments: Iterable[str] | None = None,
    settle_max_wait_s: float = 0.0,
    settle_quiet_ms: int = 300,
    settle_min_after_action_ms: int = 0,
    query_timeout_s: float = 5.0,
    tap_injector: AndroidTapInjector | None = None,
    typing_backend: AndroidTypingBackend | None = None,
) -> AsyncIterator[AndroidCapability]:
    """An ``AndroidCapability`` set up over *adb*, negotiated to *fragments* (all when ``None``).

    The APK is :func:`fake_service_apk`; the tap injector is the shell helper's
    and the typing backend ``set_text`` unless given. The wait for a still
    screen is off unless *settle_max_wait_s* says otherwise, so a capture is
    one round trip per source.
    """
    with tempfile.TemporaryDirectory() as directory:
        capability = AndroidCapability(
            service_apk=str(fake_service_apk(Path(directory))),
            settle_max_wait_s=settle_max_wait_s,
            settle_quiet_ms=settle_quiet_ms,
            settle_min_after_action_ms=settle_min_after_action_ms,
            query_timeout_s=query_timeout_s,
        )
        configured(capability, adb, tap_injector=tap_injector, typing_backend=typing_backend)
        await capability.setup(negotiation=None if fragments is None else frozenset(fragments))
        try:
            yield capability
        finally:
            await capability.teardown()
