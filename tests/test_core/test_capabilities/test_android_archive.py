"""``android.ui_tree_raw``: the whole UI tree of every capture, cleaned again offline (§11.2).

The archiving contract runs the Android capability through
``tests/_helpers/archive_contract.py``; this module pins what is the
archiver's own, over the fake ADB server's scripted phone:

- a run that records it can clean every step's tree again offline, read back
  through the trace's archive boundary, into exactly the fragment that step
  recorded; a run without it writes no ``env/``;
- only a capture that succeeded is archived, whether or not the run
  negotiates the tree, and a screen that did not change is stored once;
- a tree at the source's node limit is kept whole, in a fraction of its JSON,
  without a second request to the device and without encoding inside the
  capture: the encoding runs when the trace hook collects;
- a run whose agent reads no tree archives a tree captured for the archive
  alone, which the agent never sees, and a failure of that capture is a gap of
  the archive, never a failed observation (user ruling 2026-11-03);
- the archiver is chosen only through ``trace_writer.archive``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import pytest
from pydantic import JsonValue

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.android.archive.ui_tree_raw import (
    RawUITree,
    RawUITreeArchiver,
    raw_ui_trees,
)
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.device.channel import JsonObject
from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityForest
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import (
    SCREENSHOT,
    UI_TREE,
    AndroidUITreeObs,
)
from spectragent.core.capabilities.android.session import CaptureError
from spectragent.core.composer import ConfigComposer
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.encoding import ENV_DIR
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.android_archive import AndroidArchiveDriver
from tests._helpers.android_device import ScriptedPhone, configured, fake_service_apk, phone_server
from tests._helpers.android_runs import ScriptedModel
from tests._helpers.assembly import AGENT_ID, make_env, make_run


def _screen(*rows: str) -> JsonObject:
    """Settings' list: a root, a layout not important for accessibility, and *rows*, the last off the screen.

    The cleaner keeps the rows on the screen; the archive keeps all of it.
    """
    nodes: list[JsonValue] = [
        {
            "bounds_in_screen": {"right": 1080, "bottom": 2400},
            "class_name": "android.widget.FrameLayout",
            "package_name": "com.android.settings",
            "window_id": 7,
            "is_visible_to_user": True,
            "child_ids": [1],
            "is_important": True,
        },
        {
            "unique_id": 1,
            "bounds_in_screen": {"right": 1080, "bottom": 2400},
            "class_name": "android.widget.LinearLayout",
            "package_name": "com.android.settings",
            "window_id": 7,
            "is_visible_to_user": True,
            "child_ids": list(range(2, len(rows) + 2)),
            "depth": 1,
            "drawing_order": 1,
        },
    ]
    for number, text in enumerate(rows):
        top = 2600 if number == len(rows) - 1 else 300 + 150 * number
        nodes.append(
            {
                "unique_id": number + 2,
                "bounds_in_screen": {"top": top, "right": 1080, "bottom": top + 150},
                "class_name": "android.widget.TextView",
                "package_name": "com.android.settings",
                "text": text,
                "window_id": 7,
                "is_clickable": True,
                "is_enabled": True,
                "is_visible_to_user": number < len(rows) - 1,
                "depth": 2,
                "drawing_order": number + 1,
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


HOME = _screen("Network & internet", "Connected devices", "Battery")
NETWORK = _screen("Internet", "Calls & SMS", "Airplane mode", "Hotspot & tethering")


class _TreeReader:
    """Reads the UI tree: taps element 0 on its first step, then is done."""

    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset()
    __wiring__: ClassVar[dict[str, Any]] = {
        "action": ("action", ["click", "done"]),
        "observation": ("observation", ["ui_tree"]),
    }

    def __init__(self) -> None:
        self.steps = 0

    async def build_react_prompt(
        self, obs: Any, *, context: ReactPromptContext
    ) -> list[dict[str, str]]:
        return [{"role": "user", "content": obs.ui_tree.serialized}]

    def parse_react_output(self, out: Any) -> list[Action]:
        self.steps += 1
        if self.steps == 1:
            return [Action(name="click", params={"index": 0})]
        return [Action(name="done", params={"extracted_content": None})]

    def get_react_output_model(self) -> None:
        return None


async def _run(
    phone: ScriptedPhone,
    tmp_path: Path,
    *,
    archive: bool,
    agent: Any = None,
    model: ScriptedModel | None = None,
) -> tuple[RunTrace, AndroidCapability]:
    """Steps of *agent* on *phone* and the terminal observation, traced.

    By default the agent reads the UI tree: it taps element 0, then it is done.
    """
    writer = TraceWriter(
        trace_dir=str(tmp_path / "traces"), archive=[RawUITreeArchiver()] if archive else None
    )
    async with phone_server(phone) as adb:
        capability = AndroidCapability(
            service_apk=str(fake_service_apk(tmp_path)), settle_max_wait_s=0
        )
        configured(capability, adb)
        run = make_run(
            agent=agent or _TreeReader(),
            loop=ReactLoop(max_steps=5, terminal_observation=True),
            env=make_env(AndroidEnvironment, {"android#0": capability}),
            llm=model or ScriptedModel("tap the first row", "done"),
            memory=SimpleMemory(),
            hooks=[writer],
        )
        result = await AgentRuntime.for_agent(run, AGENT_ID).execute("open a setting", timeout=30)
    assert isinstance(result, AgentResult) and result.agent_done == "done", result.error
    assert writer.run_dir is not None
    return RunTrace(writer.run_dir), capability


async def test_an_offline_clean_of_each_archived_tree_gives_back_its_steps_fragment(
    tmp_path: Path,
) -> None:
    phone = ScriptedPhone(forest=dict(HOME))

    def opened() -> None:
        if len(phone.inputs) > 1:  # the agent's tap, after setup's test tap
            phone.forest = dict(NETWORK)

    phone.on_tap = opened
    trace, capability = await _run(phone, tmp_path, archive=True)
    cleaner, serializer = capability.ui_tree_cleaner, capability.ui_tree_serializer

    records = trace.step_records
    assert [record.origin for record in records] == ["policy", "policy", "terminal"]
    screens = []
    for step in (record.step for record in records):
        recorded = trace.observation_fragment(step, UI_TREE)
        assert isinstance(recorded, AndroidUITreeObs)
        (raw,) = raw_ui_trees(trace, step)
        offline = cleaner.clean(raw.forest, raw.screen)
        assert offline == recorded.tree(), f"step {step}"
        assert serializer.serialize(offline) == recorded.serialized, f"step {step}"
        assert (raw.producer, raw.screen, raw.omitted_offscreen) == (
            "accessibility_service",
            (1080, 2400),
            0,
        )
        kept = sum(len(window.tree.nodes) for window in recorded.forest.windows)
        whole = sum(len(window.tree.nodes) for window in raw.forest.windows)
        assert whole == recorded.node_count > kept, "the archive keeps what the cleaner dropped"
        screens.append(raw.forest)
    assert screens == [
        AndroidAccessibilityForest.model_validate(HOME),
        AndroidAccessibilityForest.model_validate(NETWORK),
        AndroidAccessibilityForest.model_validate(NETWORK),
    ], "each step's commit took its own capture's tree"
    (first,) = trace.archive_files(0, RawUITreeArchiver.__registry_key__)
    assert (first.name, first.cycle, first.media_type) == (
        "ui_tree-1.json.gz",
        1,
        "application/gzip",
    )


async def test_a_run_without_the_archiver_writes_no_env(tmp_path: Path) -> None:
    trace, _ = await _run(ScriptedPhone(forest=dict(HOME)), tmp_path, archive=False)

    assert trace.step_count == 3
    assert not (trace.trace_dir / ENV_DIR).exists()
    assert raw_ui_trees(trace, 0) == []
    assert trace.config.env_evidence_privacy is None


# ── what is archived, and when ────────────────────────────────────────


@pytest.mark.parametrize(
    "fragments",
    [frozenset({UI_TREE, SCREENSHOT}), frozenset({SCREENSHOT})],
    ids=["tree negotiated", "tree for the archive alone"],
)
async def test_only_a_capture_that_succeeded_is_archived(fragments: frozenset[str]) -> None:
    driver = AndroidArchiveDriver(fragments=fragments)
    driver.capability.bind_archivers(list(driver.archivers))
    try:
        await driver.ensure_setup()
        driver.phone.screenshot_fails = True
        with pytest.raises(CaptureError):
            await driver.observe()
        assert driver.phone.service_calls("tree") == 1, "the tree was captured, the capture failed"
        assert (await driver.capability.collect_archive()).is_empty

        driver.phone.screenshot_fails = False
        await driver.observe()
        batch = await driver.capability.collect_archive()
    finally:
        await driver.close()

    (only,) = batch.files
    assert only.meta.cycle == 2 and not batch.gaps


async def test_a_screen_that_did_not_change_is_stored_once() -> None:
    driver = AndroidArchiveDriver()
    driver.capability.bind_archivers(list(driver.archivers))
    try:
        await driver.observe()
        await driver.observe()
        batch = await driver.capability.collect_archive()
    finally:
        await driver.close()

    first, second = batch.files
    assert first.content_addressed and first.data == second.data, "same tree, same bytes"
    assert (first.name, second.name) == ("ui_tree-1.json.gz", "ui_tree-2.json.gz")
    assert (first.meta.cycle, second.meta.cycle) == (1, 2)


# ── a tree at the node limit ──────────────────────────────────────────


def _long_list(rows: int) -> JsonObject:
    """A list of *rows* rows of three nodes each, past the screen from the 15th: ``3 * rows + 2`` nodes."""
    nodes: list[JsonValue] = [
        {
            "bounds_in_screen": {"right": 1080, "bottom": 2400},
            "class_name": "android.widget.FrameLayout",
            "package_name": "com.example.list",
            "is_visible_to_user": True,
            "child_ids": [1],
            "is_important": True,
        },
        {
            "unique_id": 1,
            "bounds_in_screen": {"top": 200, "right": 1080, "bottom": 2400},
            "class_name": "androidx.recyclerview.widget.RecyclerView",
            "package_name": "com.example.list",
            "view_id_resource_name": "com.example.list:id/recycler",
            "is_visible_to_user": True,
            "is_scrollable": True,
            "child_ids": [2 + 3 * row for row in range(rows)],
            "depth": 1,
            "drawing_order": 1,
            "is_important": True,
        },
    ]
    for row in range(rows):
        top = 200 + 150 * row
        box: JsonObject = {"top": top, "right": 1080, "bottom": top + 150}
        on_screen = top + 150 <= 2400
        layout = 2 + 3 * row
        container: JsonObject = {
            "unique_id": layout,
            "bounds_in_screen": box,
            "class_name": "android.widget.LinearLayout",
            "package_name": "com.example.list",
            "is_clickable": True,
            "is_visible_to_user": on_screen,
            "child_ids": [layout + 1, layout + 2],
            "depth": 2,
            "drawing_order": row + 1,
        }
        nodes.append(container)
        for offset, text in enumerate((f"Item {row}", f"Updated {row % 60} minutes ago")):
            label: JsonObject = {
                "unique_id": layout + 1 + offset,
                "bounds_in_screen": box,
                "class_name": "android.widget.TextView",
                "package_name": "com.example.list",
                "text": text,
                "view_id_resource_name": f"com.example.list:id/{('title', 'summary')[offset]}",
                "is_visible_to_user": on_screen,
                "depth": 3,
                "drawing_order": offset + 1,
                "is_important": True,
            }
            nodes.append(label)
    return {"windows": [{"id": 3, "window_type": "TYPE_APPLICATION", "tree": {"nodes": nodes}}]}


async def test_a_tree_at_the_node_limit_is_kept_whole_without_costing_the_capture() -> None:
    """The source's default limit is 5000 nodes; the device took about 1.1 s to walk that many (§11.2).

    The archive asks the device nothing more and encodes nothing inside the
    capture, whose ``ui_tree_timeout_s`` it therefore never spends; the
    encoding runs when the trace hook collects, and the file is a fraction of
    the tree's JSON. How long collecting such a tree takes is the live
    module's to check, on a real one: a wall clock here would fail on a busy
    machine with nothing wrong.
    """
    driver = AndroidArchiveDriver()
    driver.phone.forest = _long_list(rows=1666)  # 5000 nodes
    driver.capability.bind_archivers(list(driver.archivers))
    encode = mock.patch.object(RawUITree, "encode", autospec=True, side_effect=RawUITree.encode)
    try:
        with encode as encoding:
            await driver.observe()
            assert encoding.call_count == 0, "nothing is encoded inside the capture"
            (file,) = (await driver.capability.collect_archive()).files
            assert encoding.call_count == 1
    finally:
        await driver.close()

    assert driver.phone.tree_reads == [5000], "one request for the capture, at the source's limit"
    raw = RawUITree.decode(file.data)
    assert raw.forest == AndroidAccessibilityForest.model_validate(driver.phone.forest)
    assert sum(len(window.tree.nodes) for window in raw.forest.windows) == 5000
    json_bytes = len(raw.forest.model_dump_json(exclude_defaults=True))
    assert len(file.data) * 8 < json_bytes, f"{len(file.data)} B gzipped of {json_bytes} B"


# ── a run whose agent reads no tree (user ruling 2026-11-03) ──────────


async def test_a_run_whose_agent_reads_no_tree_archives_a_tree_it_never_sees(
    tmp_path: Path,
) -> None:
    """UI-TARS reads the screenshot alone; the archive gets each capture's tree all the same."""
    phone = ScriptedPhone(forest=dict(HOME))
    model = ScriptedModel("Action: press_home()", "Action: finished(content='done')")
    trace, _ = await _run(
        phone,
        tmp_path,
        archive=True,
        agent=UITarsAgent(prompt_template="mobile_use"),
        model=model,
    )

    records = trace.step_records
    assert [record.origin for record in records] == ["policy", "policy", "terminal"]
    for step in (record.step for record in records):
        assert trace.observation_fragment(step, UI_TREE) is None, "the run negotiated no tree"
        (raw,) = raw_ui_trees(trace, step)
        assert raw.forest == AndroidAccessibilityForest.model_validate(HOME)
        assert (raw.producer, raw.screen) == ("accessibility_service", (1080, 2400))
    assert phone.service_calls("tree") == 3, "one ask per capture, for the archive alone"
    prompts = [str(message.content) for call in model.calls for message in call]
    assert not any("Network & internet" in prompt for prompt in prompts), "the agent never sees it"
    assert trace.manifest.status == "complete"


async def test_a_failed_capture_for_the_archive_is_its_gap_and_the_observation_goes_on() -> None:
    driver = AndroidArchiveDriver(fragments=frozenset({SCREENSHOT}))
    driver.capability.bind_archivers(list(driver.archivers))
    try:
        await driver.ensure_setup()
        driver.capability.ui_tree_timeout_s = 0.2
        driver.phone.tree_delay_s = 0.5
        await driver.observe()  # the screenshot is served
        missed = await driver.capability.collect_archive()

        driver.phone.tree_delay_s = 0.0
        await driver.observe()
        taken = await driver.capability.collect_archive()
    finally:
        await driver.close()

    assert not missed.files
    (gap,) = missed.gaps
    assert (gap.archiver_id, gap.error) == ("android.ui_tree_raw", "TimeoutError")
    assert gap.detail == (
        "the UI tree of observation cycle 1 was not captured: no answer within 0.2 s"
    )
    (file,) = taken.files
    assert not taken.gaps and file.meta.cycle == 2
    assert RawUITree.decode(file.data).forest == AndroidAccessibilityForest.model_validate(
        driver.phone.forest
    )


# ── chosen ────────────────────────────────────────────────────────────


def test_the_archiver_is_chosen_through_trace_writer_archive() -> None:
    raw: dict[str, JsonValue] = {
        "capability": [
            {
                "capability_id": "android",
                "backend": {"provider_id": "local_emulator", "avd": "AndroidWorldAvd"},
                "service_apk": "spectragent-device.apk",
                "tap_injector": {"injector_id": "shell_injection"},
                "typing_backend": {"typing_id": "set_text"},
                "ui_tree_cleaner": {"cleaner_id": "androidworld"},
                "ui_tree_serializer": {"serializer_id": "m3a"},
            }
        ],
        "hook": [{"hook_id": "trace_writer", "archive": [{"archiver_id": "android.ui_tree_raw"}]}],
    }
    run = ConfigComposer.for_config(raw).compose(raw)

    (writer,) = [hook for hook in run.instances("hook") if isinstance(hook, TraceWriter)]
    (archiver,) = writer.archive
    assert isinstance(archiver, RawUITreeArchiver)
    capability = run.single("capability")
    assert isinstance(capability, AndroidCapability)
    assert capability.provided_archivers == frozenset({"android.ui_tree_raw"})


def test_declarations() -> None:
    archiver = RawUITreeArchiver()
    assert archiver.archiver_id == "android.ui_tree_raw"
    assert archiver.needs == frozenset({"ui_tree"})
    assert archiver.privacy_markers == frozenset(), "recorded as captured, nothing withheld"
