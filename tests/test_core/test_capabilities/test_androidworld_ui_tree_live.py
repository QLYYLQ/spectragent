"""A UI-tree-scored AndroidWorld task solved through our tree, beside AndroidWorld's own forwarder.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). ``ClockStopWatchRunning`` is one of the 7
tasks AndroidWorld scores from the UI tree its accessibility forwarder sends
(``docs/architecture_design.md`` §11.2 "UI 树来源"): a forwarder our service
unbinds or starves would leave AndroidWorld scoring a stale tree. With our
service capturing the tree on every observation:

- both services are bound;
- the unsolved task scores 0;
- ``click`` by index, on the elements of our cleaned tree, opens the Stopwatch
  tab and starts it; our tree then shows "Pause" and "Lap", and AndroidWorld,
  reading its own forwarder's tree, scores the task 1.

Measured 2026-11-02 on AndroidWorld's AVD: 0.0 unsolved, 1.0 solved.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import UI_TREE, AndroidUITreeObs
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.schemas.action import Action
from tests._helpers.android_runs import (
    FORWARDER,
    androidworld_capability,
    androidworld_setup,
    installation_problem,
    write_task_list,
)

TASK = "ClockStopWatchRunning"

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
    pytest.mark.asyncio(loop_scope="module"),
]


@dataclass
class Prepared:
    env: AndroidEnvironment
    capability: AndroidCapability
    cycle: int = 0


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def prepared(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Prepared]:
    data = tmp_path_factory.mktemp("androidworld")
    write_task_list(data, TASK)
    context = AndroidWorldTaskLoader(data_dir=str(data)).get_task_context(f"{TASK}/42/0")
    capability, setup = androidworld_capability(), androidworld_setup()
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    env.check_setup(context.env, setups=[setup])
    env.bind_setups([setup])
    await env.setup(context.env)
    try:
        yield Prepared(env=env, capability=capability)
    finally:
        await env.teardown()


async def _tree(prepared: Prepared) -> AndroidUITreeObs:
    prepared.cycle += 1
    await prepared.capability.begin_observation(prepared.cycle)
    tree = await prepared.capability.resolve_fragment(UI_TREE)
    assert isinstance(tree, AndroidUITreeObs)
    return tree


async def _click(prepared: Prepared, label: str) -> None:
    """Tap the element whose text or description is *label*, by its index in a fresh tree."""
    tree = await _tree(prepared)
    for index, node in tree.tree().element_nodes():
        if label in (node.text, node.content_description):
            tapped = await prepared.capability.execute_action(
                Action(name="android.click", params={"index": index})
            )
            assert tapped.success, tapped.error
            return
    raise AssertionError(f"no element {label!r}; the tree reads:\n{tree.serialized}")


async def test_our_service_and_androidworlds_forwarder_are_both_bound(prepared: Prepared) -> None:
    session = prepared.capability._session
    assert session is not None
    client = AdbClient(session.endpoint.host, session.endpoint.port)
    enabled = (
        await client.shell(session.serial, "settings get secure enabled_accessibility_services")
    ).stdout.decode()

    assert FORWARDER in enabled and "org.spectragent.device/.DeviceService" in enabled


async def test_the_stopwatch_task_scores_0_unsolved_and_1_once_started_by_index(
    prepared: Prepared,
) -> None:
    (unsolved,) = await prepared.env.verify_live(timeout_s=90, answer=None)
    opened = await prepared.capability.execute_action(
        Action(name="android.open_app", params={"app_name": "Clock"})
    )
    assert opened.success, opened.error
    await _click(prepared, "Stopwatch")
    await _click(prepared, "Start")
    running = await _tree(prepared)
    (solved,) = await prepared.env.verify_live(timeout_s=90, answer=None)

    labels = {
        label
        for _, node in running.tree().element_nodes()
        for label in (node.text, node.content_description)
    }
    assert {"Pause", "Lap"} <= labels, running.serialized
    assert (unsolved.status, unsolved.score) == ("judged", 0.0), unsolved.detail
    assert (solved.status, solved.score) == ("judged", 1.0), solved.detail
