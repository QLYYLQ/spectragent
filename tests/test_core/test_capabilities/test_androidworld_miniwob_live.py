"""MiniWoB++ tasks prepared on fresh boots by AndroidWorld's own service, through the two launcher patches they need.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). Each task boots its own read-only emulator,
and AndroidWorld's service initializes it there. The tasks are ones the
2026-11-03 sweep (seed 42, three emulators) lost to two launcher gaps
(``docs/architecture_design.md`` §11.2 "启动器放在哪里"):

- the MiniWoB++ app's reads, sent as ordered broadcasts, waited behind Play
  Services' burst after the boot until adb's 10 s timeout: the instruction at
  initialize (``click-checkboxes-soft``, ``email-inbox-forward``; ``click-pie``
  and ``login-user-popup`` in the sweep) or the reward at the first episode
  question (``count-shape``). The launcher now sends every broadcast to the
  foreground queue (``serve.py``'s ``broadcast_in_the_foreground``);
- AndroidWorld resets the page a fixed 1 s after loading it and reads the
  instruction right after: it read an empty one (``count-sides``;
  ``click-pie`` and ``login-user-popup`` in the rerun), or the page was
  covered for good after its instruction was read (``click-option``). The
  launcher's initialize now waits for the page to load before the reset, and
  for its episode to start before the read (``wait_for_the_cover``).

For each task: setup succeeds; the run's text is the goal AndroidWorld made,
and its instruction is the one the page shows, with no cover over the task;
the episode question answers that the episode is not over; the unsolved task
scores 0. Each test prints its timings and which of the launcher's waits
waited, which depends on how fast the page loads on that boot.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityNodeInfo
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import UI_TREE, AndroidUITreeObs
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from tests._helpers.android_runs import (
    androidworld_capability,
    androidworld_setup,
    installation_problem,
    write_task_list,
)

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
]

#: What a MiniWoB++ goal starts with: AndroidWorld's template before the instruction.
PREFIX = "Follow the instructions shown on the top of the screen: "
TASKS = (
    "click-pie",
    "login-user-popup",
    "count-sides",
    "click-option",
    "email-inbox-forward",
    "click-checkboxes-soft",
    "count-shape",
)


async def _page(capability: AndroidCapability) -> tuple[set[str], set[str]]:
    """The ids of the visible nodes on the screen, and the text under each, without whitespace.

    A node's text is its own and its descendants', in tree order, as the
    screen reads it. ``core.getUtterance`` reads ``#query``, which some pages
    fill through an inner element the tree shows without the id
    (count-shape), so the instruction is looked for under any node.
    """
    await capability.begin_observation(1)
    tree = await capability.resolve_fragment(UI_TREE)
    assert isinstance(tree, AndroidUITreeObs)
    ids: set[str] = set()
    texts: set[str] = set()
    for window in tree.tree().forest.windows:
        nodes = {node.unique_id: node for node in window.tree.nodes}
        for node in window.tree.nodes:
            if node.is_visible_to_user:
                ids.add(node.view_id_resource_name)
                texts.add("".join(_text_under(node, nodes).split()))
    return ids, texts


def _text_under(
    node: AndroidAccessibilityNodeInfo, nodes: dict[int, AndroidAccessibilityNodeInfo]
) -> str:
    # A capture leaves out off-screen nodes past its max_nodes; their parents still name them.
    return node.text + "".join(
        _text_under(nodes[child], nodes) for child in node.child_ids if child in nodes
    )


@pytest.mark.parametrize("task", TASKS)
async def test_a_miniwob_task_initializes_on_a_fresh_boot_and_its_episode_runs(
    task: str, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    write_task_list(data, task, family="miniwob")
    context = AndroidWorldTaskLoader(data_dir=str(data)).get_task_context(f"{task}/42/0")
    capability, setup = androidworld_capability(), androidworld_setup()
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    env.check_setup(context.env, setups=[setup])
    env.bind_setups([setup])
    started = time.monotonic()
    await env.setup(context.env)
    try:
        setup_s = time.monotonic() - started
        service = setup._service
        assert service is not None
        log = service.log.read_text(errors="replace")
        goal = await capability.task_text_from_setup()
        assert goal is not None and goal.startswith(PREFIX), goal
        instruction = goal.removeprefix(PREFIX)

        ids, texts = await _page(capability)
        assert "sync-task-cover" not in ids and "click-canvas" in ids, ids
        assert "".join(instruction.split()) in texts, (instruction, texts)

        asked = time.monotonic()
        over = await capability.episode_over()
        asked_s = time.monotonic() - asked
        assert over is False
        (unsolved,) = await env.verify_live(timeout_s=60, answer=None)
        assert (unsolved.status, unsolved.score) == ("judged", 0.0), unsolved.detail
    finally:
        await env.teardown()

    waited = [
        line.split("serve.py: ", 1)[1] for line in log.splitlines() if "MiniWoB++ page" in line
    ]
    print(
        f"{task}: setup {setup_s:.1f} s; launcher: {waited or 'no wait'}; "
        f"episode question {asked_s:.2f} s; goal {goal!r}"
    )
