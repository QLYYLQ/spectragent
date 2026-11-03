"""``android.ui_tree_raw`` on a real emulator: real trees archived, decoded and cleaned again.

Opt-in, like ``test_android_device_side_live.py``: the marker
``requires_android_emulator`` needs ``SPECTRAGENT_TEST_AVD`` (the module was
written against an API 33 AVD), and ``SPECTRAGENT_TEST_SERVICE_APK`` names the
device-side APK. One read-only boot, the archiver bound before setup:

- on Settings' main page and on its third-party licence page (one WebView, far
  past the source's node limit), each capture's archived tree, decoded, cleans
  offline into the fragment the capture served;
- the licence page's tree is partial and says so (``omitted_offscreen``); the
  module prints its size and how long collecting it took, for the design doc.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from pydantic import JsonValue

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
#: How long the licence page may take to load and show its WebView's nodes.
_LOADED_S = 90.0


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
