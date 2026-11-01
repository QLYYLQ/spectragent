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
  ``screencap``'s between the status bar and the navigation handle;
- the service's tap, long press and swipe, and the helper's; a timed path
  whose speed changes keeps its timing on both;
- the global actions back, home and recents, and the helper's keys;
- text committed through the service's input method at the cursor, Chinese
  included, and text set on the focused field;
- events, in order and numbered without gaps;
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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest
import pytest_asyncio
from PIL import Image, ImageChops

from spectragent.core.capabilities.android.adb import AdbClient, AdbError
from spectragent.core.capabilities.android.backends.local_emulator import LocalEmulatorProvider
from spectragent.core.capabilities.android.bridge import ForegroundApp, parse_foreground
from spectragent.core.capabilities.android.device.apk import PACKAGE, SERVICE_COMPONENT, ServiceApk
from spectragent.core.capabilities.android.device.clients import (
    ServiceEvent,
    ServiceEvents,
    Stroke,
    TreeCapture,
)
from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityNodeInfo
from spectragent.core.capabilities.android.device.lifecycle import DeviceSide

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.asyncio(loop_scope="module"),
]

AVD_VARIABLE = "SPECTRAGENT_TEST_AVD"
APK_VARIABLE = "SPECTRAGENT_TEST_SERVICE_APK"
SETTINGS = "com.android.settings"
SETTINGS_SEARCH = "com.google.android.settings.intelligence"
#: How long the UI may take to show what an action did.
_SHOWN_S = 15.0
_POLL_S = 0.3

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
            shot = await side.helper.screenshot()
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


async def _front_is(up: Up, package: str) -> None:
    async def check() -> bool:
        return (await _foreground(up)).package == package

    await _until(f"{package} in front", check)


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
    await _shell(up, f"am force-stop {SETTINGS_SEARCH}")
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
    assert hello.apk_sha256 == up.apk.info.sha256 == up.side.helper.hello.apk_sha256
    assert hello.accessibility_tool == up.apk.info.accessibility_tool
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


async def test_both_screenshots_are_screencaps_pixels(up: Up) -> None:
    await _home(up)
    reference = Image.open(io.BytesIO((await up.adb.shell(up.serial, "screencap -p")).stdout))
    # Between the status bar, whose clock ticks, and the navigation handle, whose colour
    # follows what is behind it and was still changing after going home (measured).
    between_the_bars = (0, 150, up.screen[0], up.screen[1] - 150)

    for shot in (await up.side.service.screenshot(), await up.side.helper.screenshot()):
        image = Image.open(io.BytesIO(shot.png))
        assert image.size == up.screen == (shot.width, shot.height)
        difference = ImageChops.difference(
            reference.convert("RGB").crop(between_the_bars),
            image.convert("RGB").crop(between_the_bars),
        )
        assert difference.getbbox() is None, f"{shot.producer} differs from screencap"


async def test_back_to_back_service_screenshots_wait_instead_of_failing(up: Up) -> None:
    """The system refuses a screenshot within 333 ms of the last; the service waits instead.

    Sent together, the second request reaches the service while it still handles the first, so
    nothing but the first's own capture and encoding lies between the two."""
    first, second = await asyncio.gather(up.side.service.screenshot(), up.side.service.screenshot())

    assert first.png and second.png
    assert second.waited_ms > 0 or first.capture_ms + first.encode_ms >= 333


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
    return node.is_editable and node.package_name == SETTINGS_SEARCH


async def test_text_is_committed_at_the_cursor_and_set_on_the_focused_field(up: Up) -> None:
    """Committed first: right after ACTION_SET_TEXT replaced a field's text, the field restarts
    input, and a commit sent before that restart is done is lost without an error (measured)."""
    await _settings_list(up)
    bar = await _node(up, "the search bar", lambda node: node.text == "Search settings")
    await up.side.service.tap(*_center(bar))
    await _node(
        up,
        "the search field with input focus",
        lambda node: _search_field(node) and node.is_focused,
    )

    committed = await up.side.service.commit_text("蓝牙")
    assert committed.package_name == SETTINGS_SEARCH
    await _node(
        up, "the field reading '蓝牙'", lambda node: _search_field(node) and node.text == "蓝牙"
    )

    target = await up.side.service.set_text("bluetooth")
    assert (target.package_name, target.class_name) == (SETTINGS_SEARCH, "android.widget.EditText")
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
