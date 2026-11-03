"""``android.ui_tree_raw`` on a real emulator: real trees archived, decoded and cleaned again.

Opt-in, like ``test_android_device_side_live.py``: the marker
``requires_android_emulator`` needs ``SPECTRAGENT_TEST_AVD`` (the module was
written against an API 33 AVD), and ``SPECTRAGENT_TEST_SERVICE_APK`` names the
device-side APK. One read-only boot, the archiver bound before setup:

- on Settings' main page and on its third-party licence page (one WebView, far
  past the source's node limit), each capture's archived tree, decoded, cleans
  offline into the fragment the capture served;
- the licence page's tree is partial and says so (``omitted_offscreen``), and
  collecting it, the largest tree the source returns, fits the per-step
  archive budget (``ARCHIVE_MAX_P50_OVERHEAD_MS``; a busy machine may raise it
  with ``SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS``, as the browser's benchmark
  reads it). The module prints the tree's size and that time, for the design
  doc;
- negotiated to the screenshot alone, as an agent that reads only the screen
  is (user ruling 2026-11-03), each capture still archives one tree, which the
  agent cannot read; the module prints what that costs a capture, measured
  against the same captures with the archiver unbound, on Settings' main page
  and on the licence page.
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from pydantic import JsonValue

from spectragent.core.archive import ARCHIVE_MAX_P50_OVERHEAD_MS
from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.archive.ui_tree_raw import RawUITree, RawUITreeArchiver
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.observations import (
    SCREENSHOT,
    UI_TREE,
    AndroidUITreeObs,
)
from spectragent.core.capabilities.android.session import CaptureError
from spectragent.core.composer import ConfigComposer
from spectragent.schemas.action import Action

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.asyncio(loop_scope="module"),
]

APK_VARIABLE = "SPECTRAGENT_TEST_SERVICE_APK"
#: How long the licence page may take to load and show its WebView's nodes: it
#: loaded within 90 s at a 1-minute load of 4, and not at 15 (2026-11-03).
_LOADED_S = 240.0
#: How long one walk of the tree may take here. The capability's default is 10 s;
#: the first walk of the loaded licence page took 8.4 s at a load of 4 to 9, and
#: kept passing 10 s at 15 (2026-11-03). This module waits for a page, not for
#: the default limit.
_TREE_TIMEOUT_S = 30.0


@dataclass
class Up:
    capability: AndroidCapability
    cycle: int = 0


@dataclass
class Archived:
    """One capture: the fragment it served, its archived tree, the file's size and its collection's time."""

    fragment: AndroidUITreeObs
    raw: RawUITree
    size: int
    collect_s: float


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def up() -> AsyncIterator[Up]:
    apk = os.environ.get(APK_VARIABLE)
    if not apk:
        pytest.skip(f"set {APK_VARIABLE} to the device-side APK")
    raw: dict[str, JsonValue] = {
        "capability": [
            {
                "capability_id": "android",
                "backend": {
                    "provider_id": "local_emulator",
                    "avd": os.environ["SPECTRAGENT_TEST_AVD"],
                },
                "service_apk": apk,
                "tap_injector": {"injector_id": "shell_injection"},
                "typing_backend": {"typing_id": "set_text"},
                "ui_tree_cleaner": {"cleaner_id": "androidworld"},
                "ui_tree_serializer": {"serializer_id": "m3a"},
                "ui_tree_timeout_s": _TREE_TIMEOUT_S,
            }
        ]
    }
    capability = ConfigComposer.for_config(raw).compose(raw).single("capability")
    assert isinstance(capability, AndroidCapability)
    capability.bind_archivers([RawUITreeArchiver()])
    await capability.setup(negotiation=frozenset({UI_TREE, SCREENSHOT}))
    try:
        yield Up(capability)
    finally:
        await capability.teardown()


async def _observe(up: Up) -> Archived:
    """One observation, then one collection: the capture's fragment and its archived tree."""
    up.cycle += 1
    await up.capability.begin_observation(up.cycle)
    fragment = await up.capability.resolve_fragment(UI_TREE)
    assert isinstance(fragment, AndroidUITreeObs)
    started = time.perf_counter()
    batch = await up.capability.collect_archive()
    collect_s = time.perf_counter() - started
    (file,) = batch.files
    return Archived(fragment, RawUITree.decode(file.data), len(file.data), collect_s)


def _assert_cleans_back(up: Up, archived: Archived) -> None:
    capability, raw = up.capability, archived.raw
    offline = capability.ui_tree_cleaner.clean(raw.forest, raw.screen)
    assert offline == archived.fragment.tree()
    assert capability.ui_tree_serializer.serialize(offline) == archived.fragment.serialized
    assert raw.screen == capability.screen_size and raw.producer == "accessibility_service"
    nodes = sum(len(window.tree.nodes) for window in raw.forest.windows)
    assert nodes == archived.fragment.node_count
    assert raw.omitted_offscreen == archived.fragment.omitted_offscreen


async def test_settings_main_page_cleans_back_from_its_archived_tree(up: Up) -> None:
    opened = await up.capability.execute_action(
        Action(name="android.open_app", params={"app_name": "Settings"})
    )
    assert opened.success, opened.error
    archived = await _observe(up)

    _assert_cleans_back(up, archived)
    assert archived.fragment.elements, "the page has elements to compare"


async def test_a_licence_page_past_the_node_limit_cleans_back_and_says_it_is_partial(
    up: Up,
) -> None:
    session = up.capability._session
    assert session is not None
    adb = AdbClient(session.endpoint.host, session.endpoint.port)
    started = await adb.shell(session.serial, "am start -W -a android.settings.LICENSE")
    assert started.exit_code == 0, started.stderr.decode(errors="replace")

    archived: Archived | None = None
    deadline = time.monotonic() + _LOADED_S
    while time.monotonic() < deadline:
        try:
            archived = await _observe(up)
        except CaptureError:  # the first walk of the loaded page can pass the tree's limit
            continue
        if archived.raw.omitted_offscreen:
            break
    assert archived is not None and archived.raw.omitted_offscreen, (
        f"the licence page never passed the node limit within {_LOADED_S:g} s"
    )
    loaded = await _observe(up)  # a walk of the page as it now stands

    _assert_cleans_back(up, loaded)
    nodes = sum(len(window.tree.nodes) for window in loaded.raw.forest.windows)
    json_bytes = len(loaded.raw.model_dump_json(exclude_defaults=True))
    print(
        f"\nlicence page: {nodes} nodes, {loaded.raw.omitted_offscreen} off-screen subtrees "
        f"left out, {len(loaded.fragment.elements)} elements; JSON {json_bytes} B, archived "
        f"{loaded.size} B ({json_bytes / loaded.size:.1f}x); collected in "
        f"{loaded.collect_s * 1000:.0f} ms; the device walked it in {loaded.fragment.capture_ms} ms"
    )
    bound = float(
        os.environ.get("SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS") or ARCHIVE_MAX_P50_OVERHEAD_MS
    )
    assert loaded.collect_s * 1000 <= bound, (
        f"collecting the licence page's tree took {loaded.collect_s * 1000:.0f} ms (bound "
        f"{bound:.0f} ms; override with SPECTRAGENT_ARCHIVE_OVERHEAD_P50_MS)"
    )


async def _capture_s(up: Up) -> float:
    """One screenshot-only observation: how long its capture took, seconds."""
    up.cycle += 1
    await up.capability.begin_observation(up.cycle)
    started = time.perf_counter()
    await up.capability.resolve_fragment(SCREENSHOT)
    return time.perf_counter() - started


async def _costs(up: Up, rounds: int) -> tuple[list[float], list[float], list[RawUITree]]:
    """Captures with the archiver unbound and bound, alternating: their times, and the trees archived."""
    archiver = RawUITreeArchiver()
    without: list[float] = []
    with_tree: list[float] = []
    trees: list[RawUITree] = []
    for _ in range(rounds):
        up.capability.bind_archivers([])
        without.append(await _capture_s(up))
        assert (await up.capability.collect_archive()).is_empty
        up.capability.bind_archivers([archiver])
        with_tree.append(await _capture_s(up))
        batch = await up.capability.collect_archive()
        (file,) = batch.files
        assert not batch.gaps, batch.gaps
        trees.append(RawUITree.decode(file.data))
    return without, with_tree, trees


def _ms(times: list[float]) -> str:
    ordered = sorted(times)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return f"median {statistics.median(times) * 1000:.0f} ms, p90 {p90 * 1000:.0f} ms"


async def test_a_run_that_reads_only_the_screen_archives_a_tree_it_cannot_read(up: Up) -> None:
    session = up.capability._session
    assert session is not None
    adb = AdbClient(session.endpoint.host, session.endpoint.port)
    up.capability.apply_negotiation(frozenset({SCREENSHOT}))
    try:
        with pytest.raises(KeyError, match="does not capture 'android.ui_tree'"):
            await _capture_s(up)  # the screenshot is served...
            await up.capability.resolve_fragment(UI_TREE)  # ...the tree never is
        await up.capability.collect_archive()

        shown = await adb.shell(session.serial, "am start -W -n com.android.settings/.Settings")
        assert shown.exit_code == 0, shown.stderr.decode(errors="replace")
        without, with_tree, trees = await _costs(up, rounds=8)
        assert all(tree.forest.windows for tree in trees)
        main_nodes = max(sum(len(w.tree.nodes) for w in tree.forest.windows) for tree in trees)
        report = [
            f"Settings' main page ({main_nodes} nodes): capture {_ms(without)} without the "
            f"archiver, {_ms(with_tree)} with it"
        ]

        shown = await adb.shell(session.serial, "am start -W -a android.settings.LICENSE")
        assert shown.exit_code == 0, shown.stderr.decode(errors="replace")
        up.capability.bind_archivers([RawUITreeArchiver()])
        deadline = time.monotonic() + _LOADED_S
        loaded = False
        while not loaded and time.monotonic() < deadline:
            await _capture_s(up)
            batch = await up.capability.collect_archive()
            loaded = any(RawUITree.decode(f.data).omitted_offscreen for f in batch.files)
        assert loaded, f"the licence page never passed the node limit within {_LOADED_S:g} s"
        without, with_tree, trees = await _costs(up, rounds=5)
        assert all(tree.omitted_offscreen for tree in trees)
        report.append(
            f"licence page (5000 nodes and more): capture {_ms(without)} without the archiver, "
            f"{_ms(with_tree)} with it"
        )
        print(
            "\nscreenshot-only captures with the tree for the archive alone:\n" + "\n".join(report)
        )
    finally:
        up.capability.apply_negotiation(frozenset({UI_TREE, SCREENSHOT}))
        up.capability.bind_archivers([RawUITreeArchiver()])
