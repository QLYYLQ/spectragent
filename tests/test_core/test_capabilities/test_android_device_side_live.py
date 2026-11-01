"""The device-side APK on a real emulator, booted once for the whole module.

Opt-in: the marker ``requires_android_emulator`` needs ``SPECTRAGENT_TEST_AVD``
(an AVD to boot), and ``SPECTRAGENT_TEST_SERVICE_APK`` names an APK built by
``uv run python -m spectragent.core.capabilities.android.device.apk build``.
The ``local_emulator`` provider boots the AVD read-only, so the APK is
installed afresh and the AVD stays as it was. The boot covers, in file order:

- the install, the helper and the service, confirmed by a ping; the hello
  reports the installed build's SHA-256;
- the window forest: complete (views not important for accessibility, view
  ids), consistent (ids, children, depths), and visible nodes kept past the
  node limit;
- screenshots from the service and the helper, each identical to
  ``screencap``'s between the status bar and the navigation handle (the
  helper's from API 31);
- the service's tap, long press and swipe, and the helper's; a timed path
  whose speed changes keeps its timing on both;
- the global actions back, home and recents, and the helper's keys;
- text committed through the service's input method at the cursor, Chinese
  included (API 33), and text set on the focused field;
- events, in order and numbered without gaps;
- the service refuses a wrong token, closes a connection from an app's uid
  before reading it, closes a hello over 64 KB without an answer, and closes
  at once a connection past its limit;
- a provider error leaves ``content`` with exit code 0 and the error on
  stderr, which is how the host tells a failed token write;
- the helper's disable and enable keep another service's entry in the setting;
- a force-stop of the package (what a task may do) takes the service out of
  the setting, and ``ensure_bound`` brings it back;
- ``stop`` takes the service out of the setting and the socket goes; the
  package stays installed.

What the tests touch is found in the tree and touched at the middle of its
bounds, once it is there: a point read off one screen lands elsewhere on
another, or on a screen still drawing. A screen counts as still when two tree
captures in a row read the same. Events reach the service from the system on
their own schedule, so a test waits for the ones it expects.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import struct
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image, ImageChops

from spectragent.core.capabilities.android.adb import AdbClient, AdbError
from spectragent.core.capabilities.android.backends.local_emulator import LocalEmulatorProvider
from spectragent.core.capabilities.android.bridge import ForegroundApp, parse_foreground
from spectragent.core.capabilities.android.device.apk import (
    BUILD_TOOLS,
    PACKAGE,
    SERVICE_COMPONENT,
    ServiceApk,
    android_sdk,
)
from spectragent.core.capabilities.android.device.channel import (
    PROTOCOL_VERSION,
    Channel,
    HandshakeRefused,
    encode_frame,
)
from spectragent.core.capabilities.android.device.clients import (
    ServiceEvent,
    ServiceEvents,
    Stroke,
    TreeCapture,
)
from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityNodeInfo
from spectragent.core.capabilities.android.device.lifecycle import PROVIDER_URI, DeviceSide

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.asyncio(loop_scope="module"),
]

AVD_VARIABLE = "SPECTRAGENT_TEST_AVD"
APK_VARIABLE = "SPECTRAGENT_TEST_SERVICE_APK"
SETTINGS = "com.android.settings"
#: Settings' search: Google's on the API 31 and 33 images, AOSP's on API 30's.
SETTINGS_SEARCH = ("com.google.android.settings.intelligence", "com.android.settings.intelligence")
#: How long the UI may take to show what an action did.
_SHOWN_S = 15.0
#: The connections the service keeps open at once (``Server.MAX_CONNECTIONS``).
MAX_CONNECTIONS = 8
#: Another service's entry in the setting, which neither disable nor enable may touch.
OTHER_SERVICE = "com.example.kept/com.example.kept.KeptService"
_POLL_S = 0.3
#: The uids ``su`` runs the probe as, on the emulator's userdebug image: the shell user, as which
#: adbd connects for the host, and an app's.
SHELL_UID, APP_UID = 2000, 10000
PROBE_PATH = "/data/local/tmp/spectragent-probe.dex"
#: A client of an abstract socket, to connect as a peer the host never is: once connected it prints
#: ``connected``, sends its stdin, and prints what comes back. The image has no such tool (toybox
#: nc 0.8.6 knows only socket files), so the test builds it with the JDK and the SDK that build the
#: APK.
PROBE_SOURCE = """
import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;

public final class Probe {
    public static void main(String[] args) throws Exception {
        ByteArrayOutputStream sent = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        for (int read; (read = System.in.read(buffer)) != -1; ) {
            sent.write(buffer, 0, read);
        }
        LocalSocket socket = new LocalSocket();
        socket.connect(new LocalSocketAddress(args[0]));
        System.out.println("connected");
        try {
            socket.getOutputStream().write(sent.toByteArray());
            InputStream in = socket.getInputStream();
            for (int read; (read = in.read(buffer)) != -1; ) {
                System.out.write(buffer, 0, read);
            }
        } catch (IOException closed) {
            // closed from the other end
        } finally {
            System.out.flush();
            socket.close();
        }
    }
}
"""

Node = AndroidAccessibilityNodeInfo


@dataclass
class Up:
    adb: AdbClient
    serial: str
    apk: ServiceApk
    side: DeviceSide
    screen: tuple[int, int]
    #: The service's events from the start of the module.
    events: ServiceEvents


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def up() -> AsyncIterator[Up]:
    path = os.environ.get(APK_VARIABLE)
    if not path:
        pytest.skip(
            f"set {APK_VARIABLE} to an APK built by `uv run python -m {ServiceApk.__module__} build`"
        )
    apk = ServiceApk.load(path)
    provider = LocalEmulatorProvider(avd=os.environ[AVD_VARIABLE])
    session = await provider.open()
    try:
        adb = AdbClient(session.endpoint.host, session.endpoint.port)
        side = await DeviceSide.start(adb, session.serial, apk, timeout_s=120)
        try:
            shot = await side.service.screenshot()  # the helper's needs API 31
            events = await side.service.events()
            yield Up(adb, session.serial, apk, side, (shot.width, shot.height), events)
        finally:
            await side.stop()
    finally:
        await provider.close()


async def _shell(up: Up, command: str) -> str:
    async with asyncio.timeout(30):
        return (await up.adb.shell(up.serial, command)).stdout.decode(errors="replace")


async def _until(what: str, check: Callable[[], Awaitable[bool]]) -> None:
    """Poll *check* until it holds, for at most :data:`_SHOWN_S`."""
    for _ in range(int(_SHOWN_S / _POLL_S)):
        if await check():
            return
        await asyncio.sleep(_POLL_S)
    raise AssertionError(f"within {_SHOWN_S:g} s: {what}")


def _nodes(capture: TreeCapture) -> list[Node]:
    return [node for window in capture.forest.windows for node in window.tree.nodes]


def _visible(capture: TreeCapture) -> dict[str, Node]:
    """Visible nodes with a text or a description, by it."""
    return {
        node.text or node.content_description: node
        for node in _nodes(capture)
        if node.is_visible_to_user and (node.text or node.content_description)
    }


async def _node(up: Up, what: str, matches: Callable[[Node], bool]) -> Node:
    """The first visible node that *matches*, once the tree has one."""
    found: list[Node] = []

    async def shown() -> bool:
        capture = await up.side.service.tree()
        found[:] = [node for node in _nodes(capture) if node.is_visible_to_user and matches(node)]
        return bool(found)

    await _until(what, shown)
    return found[0]


async def _still(up: Up) -> TreeCapture:
    """A capture once two in a row read the same: the texts and places of the visible nodes."""
    last = [await up.side.service.tree()]

    def reading(capture: TreeCapture) -> set[tuple[str, int, int]]:
        return {
            (text, node.bounds_in_screen.top, node.bounds_in_screen.left)
            for text, node in _visible(capture).items()
        }

    async def still() -> bool:
        capture = await up.side.service.tree()
        same = reading(capture) == reading(last[0])
        last[0] = capture
        return same

    await _until("a still screen", still)
    return last[0]


def _center(node: Node) -> tuple[float, float]:
    box = node.bounds_in_screen
    return (box.left + box.right) / 2, (box.top + box.bottom) / 2


async def _foreground(up: Up) -> ForegroundApp:
    return parse_foreground(await _shell(up, "dumpsys window"))


async def _front_is(up: Up, packages: tuple[str, ...]) -> None:
    async def check() -> bool:
        return (await _foreground(up)).package in packages

    await _until(f"one of {packages} in front", check)


async def _until_launcher(up: Up) -> None:
    async def check() -> bool:
        front = await _foreground(up)
        return front.package is not None and "launcher" in front.package

    await _until("the launcher in front", check)


async def _home(up: Up) -> None:
    await up.side.service.global_action("home")
    await _until_launcher(up)
    await _still(up)


async def _search_bar(up: Up) -> Node:
    """Settings' search bar, on Settings' first page: nothing left open by an earlier test.

    Settings' task is cleared and started again in its running process rather than
    force-stopped: a cold start on a loaded machine took long enough to fail tests.
    """
    for search in SETTINGS_SEARCH:
        await _shell(up, f"am force-stop {search}")
    # Not `am start -W`: it once waited for a launch it never saw complete, for good (measured).
    said = await _shell(up, f"am start --activity-clear-task -n {SETTINGS}/.Settings")
    try:
        return await _node(
            up,
            "Settings' search bar",
            lambda node: node.package_name == SETTINGS and node.text == "Search settings",
        )
    except AssertionError as missing:
        windows = [
            (window.window_type, window.title, len(window.tree.nodes))
            for window in (await up.side.service.tree()).forest.windows
        ]
        missing.add_note(f"am start said: {said.strip()}")
        missing.add_note(f"in front: {await _foreground(up)}; the tree's windows: {windows}")
        raise


async def _settings_list(up: Up) -> TreeCapture:
    """Settings, started afresh, its list drawn and still."""
    await _search_bar(up)
    return await _still(up)


def _scrolled(before: TreeCapture, after: TreeCapture) -> bool:
    """Whether the list moved up: an item seen before is higher now, or gone, and a new one shows."""
    old, new = _visible(before), _visible(after)
    moved_or_gone = any(
        text not in new or new[text].bounds_in_screen.top < node.bounds_in_screen.top - 100
        for text, node in old.items()
        if node.package_name == SETTINGS
    )
    return moved_or_gone and bool(set(new) - set(old))


# ── up ────────────────────────────────────────────────────────────────


async def test_the_service_is_up_on_the_build_it_was_given(up: Up) -> None:
    hello = up.side.service.hello

    assert up.side.installed_now, "a read-only boot starts without the APK"
    assert up.apk.signed_with_test_key, "the default build signs with the committed key"
    assert hello.apk_sha256 == up.apk.info.sha256 == up.side.helper.hello.apk_sha256
    # The system reads isAccessibilityTool from API 31; below, the hello says null.
    expected = up.apk.info.accessibility_tool if hello.sdk_int >= 31 else None
    assert hello.accessibility_tool == expected
    assert (await up.side.service.ping()).uptime_ms > 0
    assert (up.side.faketouch, up.side.gestures) == (True, True)
    assert SERVICE_COMPONENT in await _shell(
        up, "settings get secure enabled_accessibility_services"
    )


# ── the tree ──────────────────────────────────────────────────────────


async def test_the_forest_is_complete_and_consistent(up: Up) -> None:
    capture = await _settings_list(up)

    nodes = _nodes(capture)
    assert capture.node_count == len(nodes) and capture.omitted_offscreen == 0
    assert any(not node.is_important for node in nodes), "views not important for accessibility"
    assert any(node.view_id_resource_name.startswith(f"{SETTINGS}:id/") for node in nodes)
    assert any(window.window_type == "TYPE_APPLICATION" for window in capture.forest.windows)
    for window in capture.forest.windows:
        tree = {node.unique_id: node for node in window.tree.nodes}
        if not tree:
            continue
        assert sorted(tree) == list(range(len(tree))) and tree[0].depth == 0
        for node in tree.values():
            for child in node.child_ids:
                assert tree[child].depth == node.depth + 1


async def test_past_the_node_limit_visible_nodes_are_kept_and_offscreen_ones_counted(
    up: Up,
) -> None:
    full = await _settings_list(up)
    capped = await up.side.service.tree(max_nodes=1)

    kept = [node for window in capped.forest.windows for node in window.tree.nodes[1:]]
    assert all(node.is_visible_to_user for node in kept), "past the limit, only visible nodes"
    offscreen = [
        node
        for window in full.forest.windows
        for node in window.tree.nodes[1:]
        if not node.is_visible_to_user
    ]
    assert capped.node_count + capped.omitted_offscreen <= full.node_count
    assert (capped.omitted_offscreen > 0) == bool(offscreen)


# ── screenshots ───────────────────────────────────────────────────────


@pytest.mark.parametrize("producer", ["service", "helper"])
async def test_a_screenshot_is_screencaps_pixels(up: Up, producer: str) -> None:
    if producer == "helper" and up.side.helper.hello.sdk_int < 31:
        pytest.skip(
            "the helper's screenshot is written for API 31 to 33; below, it answers unsupported"
        )
    await _home(up)
    reference = Image.open(io.BytesIO((await up.adb.shell(up.serial, "screencap -p")).stdout))
    # Between the status bar, whose clock ticks, and the navigation handle, whose colour
    # follows what is behind it and was still changing after going home (measured).
    between_the_bars = (0, 150, up.screen[0], up.screen[1] - 150)

    shot = await (up.side.service if producer == "service" else up.side.helper).screenshot()

    image = Image.open(io.BytesIO(shot.png))
    assert image.size == up.screen == (shot.width, shot.height)
    difference = ImageChops.difference(
        reference.convert("RGB").crop(between_the_bars),
        image.convert("RGB").crop(between_the_bars),
    )
    assert difference.getbbox() is None, f"{shot.producer} differs from screencap"


async def test_back_to_back_service_screenshots_wait_instead_of_failing(up: Up) -> None:
    """The system refuses a screenshot within 333 ms of the last (1000 ms on API 30); the service
    waits instead.

    Sent together, the second request reaches the service while it still handles the first, so
    nothing but the first's own capture and encoding lies between the two."""
    interval_ms = 1000 if up.side.service.hello.sdk_int == 30 else 333

    first, second = await asyncio.gather(up.side.service.screenshot(), up.side.service.screenshot())

    assert first.png and second.png
    assert second.waited_ms > 0 or first.capture_ms + first.encode_ms >= interval_ms


# ── gestures, actions and keys ────────────────────────────────────────


@pytest.mark.parametrize("injector", ["service", "helper"])
async def test_a_tap_opens_settings_search(up: Up, injector: str) -> None:
    await _settings_list(up)
    bar = await _node(up, "the search bar", lambda node: node.text == "Search settings")
    tap = up.side.service.tap if injector == "service" else up.side.helper.tap

    await tap(*_center(bar))

    await _front_is(up, SETTINGS_SEARCH)


async def test_a_long_press_on_an_app_icon_opens_its_shortcuts(up: Up) -> None:
    await _home(up)
    icon = await _node(
        up,
        "an app's icon on the home screen",
        lambda node: (
            node.class_name == "android.widget.TextView"
            and node.is_long_clickable
            and "launcher" in node.package_name
            and node.text in ("Phone", "Messages", "Chrome")
        ),
    )

    await up.side.service.tap(*_center(icon), duration_ms=1000)

    await _node(
        up, "the icon's shortcuts", lambda node: "App info" in (node.text, node.content_description)
    )
    await up.side.service.global_action("back")


@pytest.mark.parametrize("injector", ["service", "helper"])
async def test_a_swipe_up_scrolls_settings(up: Up, injector: str) -> None:
    before = await _settings_list(up)
    width, height = up.screen
    swipe = up.side.service.swipe if injector == "service" else up.side.helper.swipe

    await swipe((width / 2, height * 0.8), (width / 2, height * 0.3), duration_ms=400)

    async def scrolled() -> bool:
        return _scrolled(before, await up.side.service.tree())

    await _until("Settings' list moved up", scrolled)


@pytest.mark.parametrize("injector", ["service", "helper"])
async def test_a_timed_path_keeps_its_timing_and_scrolls(up: Up, injector: str) -> None:
    """A path whose speed changes along the way: slow for 200 ms, then fast."""
    before = await _settings_list(up)
    x, height = up.screen[0] / 2, up.screen[1]
    path = Stroke(
        (
            (x, height * 0.8, 0),
            (x, height * 0.76, 200),
            (x, height * 0.5, 300),
            (x, height * 0.3, 350),
        )
    )

    if injector == "service":
        elapsed = await up.side.service.gesture([path])
        # Each segment after the first waits for the system to finish the one before it.
        assert 350 <= elapsed < 1350
    else:
        elapsed = await up.side.helper.gesture(path)
        # Its events go at their planned times: waiting for the app to handle each move held
        # this path to about 750 ms (measured).
        assert 350 <= elapsed < 700

    async def scrolled() -> bool:
        return _scrolled(before, await up.side.service.tree())

    await _until("Settings' list moved up", scrolled)


async def test_global_actions_go_back_home_and_to_recents(up: Up) -> None:
    await _settings_list(up)
    await up.side.service.global_action("back")
    await _until_launcher(up)

    await _settings_list(up)
    await up.side.service.global_action("home")
    await _until_launcher(up)

    await _settings_list(up)
    await up.side.service.global_action("recents")
    # The launcher's own overview: the window manager's focus may not move.
    await _node(
        up,
        "the overview of recent apps",
        lambda node: node.text in ("Clear all", "Screenshot", "Select"),
    )
    await _home(up)


async def test_the_helper_presses_any_key(up: Up) -> None:
    await _settings_list(up)

    await up.side.helper.key("KEYCODE_HOME")

    await _until_launcher(up)


# ── text ──────────────────────────────────────────────────────────────


def _search_field(node: Node) -> bool:
    return node.is_editable and node.package_name in SETTINGS_SEARCH


async def _focused_search_field(up: Up) -> None:
    await _settings_list(up)
    bar = await _node(up, "the search bar", lambda node: node.text == "Search settings")
    await up.side.service.tap(*_center(bar))
    await _node(
        up,
        "the search field with input focus",
        lambda node: _search_field(node) and node.is_focused,
    )


async def test_text_is_committed_at_the_cursor_through_the_services_input_method(up: Up) -> None:
    if up.side.service.hello.sdk_int < 33:
        pytest.skip("an accessibility service has an input method of its own from API 33")
    await _focused_search_field(up)

    committed = await up.side.service.commit_text("蓝牙")

    assert committed.package_name in SETTINGS_SEARCH
    await _node(
        up, "the field reading '蓝牙'", lambda node: _search_field(node) and node.text == "蓝牙"
    )


async def test_text_is_set_on_the_focused_field(up: Up) -> None:
    await _focused_search_field(up)

    target = await up.side.service.set_text("bluetooth")

    assert target.package_name in SETTINGS_SEARCH
    assert target.class_name == "android.widget.EditText"
    await _node(
        up,
        "the field reading 'bluetooth'",
        lambda node: _search_field(node) and node.text == "bluetooth",
    )


# ── events ────────────────────────────────────────────────────────────


async def test_events_arrive_in_order_numbered_without_gaps(up: Up) -> None:
    """The stream has run since the module began, and would have ended with EventsLost at the
    first gap; here the events of one round trip through Settings, waited for as they come."""
    await _home(up)
    received: list[ServiceEvent] = []
    while (event := up.events.get_nowait()) is not None:
        received.append(event)

    await _search_bar(up)
    await up.side.service.global_action("home")
    states: list[str | None] = []

    def back_home() -> bool:
        if SETTINGS not in states:
            return False
        return any("launcher" in (package or "") for package in states[states.index(SETTINGS) :])

    async with asyncio.timeout(_SHOWN_S):
        while not back_home():
            event = await up.events.get()
            received.append(event)
            if event.type == "TYPE_WINDOW_STATE_CHANGED":
                states.append(event.package_name)

    seqs = [event.seq for event in received]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))


# ── what anything on the device may send ──────────────────────────────


async def test_a_wrong_token_is_refused(up: Up) -> None:
    reader, writer = await up.adb.connect_abstract(up.serial, "spectragent_device")
    with pytest.raises(HandshakeRefused) as refused:
        async with asyncio.timeout(_SHOWN_S):
            await Channel.open(reader, writer, token="not-this-sessions-token", component="service")

    assert refused.value.reason == "token"


def _probe_dex(directory: Path) -> bytes:
    """:data:`PROBE_SOURCE` as a dex, built with javac and the SDK's d8 (skips without them)."""
    sdk = android_sdk(os.environ)
    android_jar = sdk / "platforms" / "android-33" / "android.jar"
    d8 = sdk / "build-tools" / BUILD_TOOLS / "d8"
    java_home = os.environ.get("JAVA_HOME")
    javac = str(Path(java_home) / "bin" / "javac") if java_home else shutil.which("javac")
    if not (android_jar.is_file() and d8.is_file() and javac):
        pytest.skip(f"the probe needs javac, {android_jar} and {d8}")
    source = directory / "Probe.java"
    source.write_text(PROBE_SOURCE)
    for command in (
        [
            javac,
            "--release",
            "8",
            "-classpath",
            str(android_jar),
            "-d",
            str(directory),
            str(source),
        ],
        [
            str(d8),
            "--min-api",
            "30",
            "--lib",
            str(android_jar),
            "--output",
            str(directory),
            str(directory / "Probe.class"),
        ],
    ):
        subprocess.run(command, check=True, capture_output=True, timeout=120)
    return (directory / "classes.dex").read_bytes()


async def test_a_connection_from_an_apps_uid_is_closed_before_it_is_read(
    up: Up, tmp_path: Path
) -> None:
    """The host arrives only through adbd, as the shell user or root; a peer of any other uid is
    closed as soon as it is accepted. A hello with a wrong token shows it: the shell user's is
    read and refused, an app's goes unread.

    Both run under ``su``: SELinux keeps the ``shell`` domain (``adb shell`` itself) from
    connecting to an app's socket at all, adbd excepted (seen on API 33)."""
    if f"uid={APP_UID}" not in await _shell(up, f"su {APP_UID} id"):
        pytest.skip("no su to run the probe as an app's uid")
    dex = await asyncio.to_thread(_probe_dex, tmp_path)
    await up.adb.shell(up.serial, f"cat > {PROBE_PATH} && chmod 644 {PROBE_PATH}", stdin=dex)
    hello = encode_frame(
        {"kind": "hello", "protocol": PROTOCOL_VERSION, "token": "not-this-sessions-token"}
    )
    probe = f"env CLASSPATH={PROBE_PATH} app_process / Probe spectragent_device"
    try:
        async with asyncio.timeout(_SHOWN_S):
            as_shell = await up.adb.shell(up.serial, f"su {SHELL_UID} {probe}", stdin=hello)
            as_app = await up.adb.shell(up.serial, f"su {APP_UID} {probe}", stdin=hello)
    finally:
        await _shell(up, f"rm -f {PROBE_PATH}")

    assert as_shell.stdout.startswith(b"connected\n"), as_shell.stderr.decode(errors="replace")
    assert b'"reason":"token"' in as_shell.stdout
    assert as_app.stdout == b"connected\n", "an app's hello was read"
    assert (await up.side.service.ping()).uptime_ms > 0


async def test_a_hello_over_64_kb_closes_the_stream_unanswered(up: Up) -> None:
    """A hello's size is capped before the token is read. Without the cap the connection would
    still close, at its 10 s hello timeout, so the test waits only half of that."""
    reader, writer = await up.adb.connect_abstract(up.serial, "spectragent_device")
    try:
        writer.write(struct.pack(">II", 65 * 1024 + 4, 65 * 1024))
        await writer.drain()
        async with asyncio.timeout(5):
            assert await reader.read() == b"", "closed long before its hello would time out"
    finally:
        writer.close()


async def test_a_provider_error_exits_0_with_the_error_on_stderr(up: Up) -> None:
    """What the host relies on to tell a failed token write (``device/lifecycle.py``): the
    ``content`` tool catches the provider's exception, prints it on stderr and still exits 0.
    The provider opens only ``/session``, so writing elsewhere makes it throw."""
    result = await up.adb.shell(
        up.serial, f"content write --uri {PROVIDER_URI}/elsewhere", stdin=b"x"
    )

    assert result.exit_code == 0
    assert result.stderr.startswith(f"Error while accessing provider:{PACKAGE}.bootstrap".encode())


async def test_a_connection_past_the_limit_is_closed_at_once(up: Up) -> None:
    """Each connection holds two threads until its hello times out (10 s), so their number is
    capped; with the device side's own channel, these are one too many."""
    held: list[asyncio.StreamWriter] = []
    try:
        for _ in range(MAX_CONNECTIONS):
            reader, writer = await up.adb.connect_abstract(up.serial, "spectragent_device")
            held.append(writer)
        async with asyncio.timeout(5):
            assert await reader.read() == b"", "closed long before its hello would time out"
    finally:
        for writer in held:
            writer.close()

    assert (await up.side.service.ping()).uptime_ms > 0


async def test_disabling_and_enabling_keep_another_services_entry(up: Up) -> None:
    before = (await _shell(up, "settings get secure enabled_accessibility_services")).strip()
    await _shell(up, f"settings put secure enabled_accessibility_services {OTHER_SERVICE}:{before}")
    try:
        disabled = await up.side.helper.disable_service()
        enabled = await up.side.helper.enable_service()
        assert await up.side.ensure_bound(timeout_s=30) is True
    finally:
        await _shell(up, f"settings put secure enabled_accessibility_services {before}")

    assert (disabled.changed, disabled.current) == (True, OTHER_SERVICE)
    assert (enabled.changed, enabled.current) == (True, f"{OTHER_SERVICE}:{SERVICE_COMPONENT}")


# ── the service unbound, and stopped ──────────────────────────────────


async def test_a_force_stop_unbinds_the_service_and_ensure_bound_brings_it_back(up: Up) -> None:
    """Why §11.2 forbids force-stopping our package on reset, seen on the device: the system takes
    the service out of the setting. Recovery enables it again and confirms it by ping."""
    await _shell(up, f"am force-stop {PACKAGE}")

    async def out_of_the_setting() -> bool:
        return SERVICE_COMPONENT not in await _shell(
            up, "settings get secure enabled_accessibility_services"
        )

    await _until("the service out of the setting", out_of_the_setting)

    assert await up.side.ensure_bound(timeout_s=30) is True
    assert (await up.side.service.ping()).uptime_ms > 0


async def test_stop_takes_the_service_out_and_leaves_the_package(up: Up) -> None:
    await up.side.stop()

    enabled = (await _shell(up, "settings get secure enabled_accessibility_services")).strip()
    assert enabled in ("null", ""), "the setting is back as the read-only boot had it"
    with pytest.raises(AdbError, match="closed"):
        await up.adb.connect_abstract(up.serial, "spectragent_device")
    assert (await _shell(up, f"pm path {PACKAGE}")).startswith("package:")
