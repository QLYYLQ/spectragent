"""Markor, Simple Gallery Pro and VLC open without asking for storage after a fresh-boot task setup.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). AndroidWorld's one-time app setup gives
the three apps All files access, and a fresh boot of AndroidWorld's AVD
lacks it, so every run of a task in them met the app's storage prompt and
the system's All files access page first (``docs/architecture_design.md``
§11.2 "启动器放在哪里"). The launcher grants it again once the service is up
(``serve.py``'s ``grant_all_files_access``). After one task's setup
(``MarkorMoveNote``), the test checks for each app:

- its uid holds ``MANAGE_EXTERNAL_STORAGE`` as allow;
- launched as AndroidWorld launches apps, it shows its own screen, without
  its storage prompt or the system's page.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest
import pytest_asyncio

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import UI_TREE, AndroidUITreeObs
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from tests._helpers.android_runs import (
    androidworld_capability,
    androidworld_setup,
    bind_androidworld,
    installation_problem,
    write_task_list,
)

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
    pytest.mark.asyncio(loop_scope="module"),
]

TASK = "MarkorMoveNote"
#: Each app: its package, the activity AndroidWorld's launch_app starts, a label of its
#: own screen, and what it shows without the access (seen in our seed-42 runs).
APPS = {
    "markor": (
        "net.gsantner.markor",
        "net.gsantner.markor/net.gsantner.markor.activity.MainActivity",
        "Create a new file or folder",
        ("Storage permission required to read and write files",),
    ),
    "simple gallery pro": (
        "com.simplemobiletools.gallery.pro",
        "com.simplemobiletools.gallery.pro/com.simplemobiletools.gallery.pro.activities.MainActivity",
        "More options",
        ("All files", "Media only"),
    ),
    "vlc": (
        "org.videolan.vlc",
        "org.videolan.vlc/.gui.MainActivity",
        "Video",
        ("Permission not granted", "GRANT PERMISSION"),
    ),
}
#: The system's page an app's prompt leads to.
SYSTEM_PAGE = "All files access"


@dataclass
class Prepared:
    capability: AndroidCapability
    adb: AdbClient
    serial: str
    #: The launcher's notes on what it granted.
    granted: list[str] = field(default_factory=list)
    cycle: int = 0


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def prepared(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Prepared]:
    data = tmp_path_factory.mktemp("androidworld")
    write_task_list(data, TASK)
    context = AndroidWorldTaskLoader(data_dir=str(data)).get_task_context(f"{TASK}/42/0")
    capability, setup = androidworld_capability(), androidworld_setup()
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    bind_androidworld(env, context.env, setup)
    await env.setup(context.env)
    try:
        service, session = setup._service, capability._session
        assert service is not None and session is not None
        granted = [
            line.split("serve.py: ", 1)[1]
            for line in service.log.read_text(errors="replace").splitlines()
            if "All files access" in line
        ]
        print(f"{TASK}: the launcher: {granted or 'granted nothing'}")
        adb = AdbClient(session.endpoint.host, session.endpoint.port)
        yield Prepared(capability=capability, adb=adb, serial=session.serial, granted=granted)
    finally:
        await env.teardown()


async def _labels(prepared: Prepared, package: str) -> set[str]:
    """The texts and descriptions on the screen of *package* and of the system's settings."""
    prepared.cycle += 1
    await prepared.capability.begin_observation(prepared.cycle)
    tree = await prepared.capability.resolve_fragment(UI_TREE)
    assert isinstance(tree, AndroidUITreeObs)
    return {
        label
        for window in tree.tree().forest.windows
        for node in window.tree.nodes
        if node.is_visible_to_user and node.package_name in (package, "com.android.settings")
        for label in (node.text, node.content_description)
        if label
    }


async def _labels_until(
    prepared: Prepared, package: str, seen: Callable[[set[str]], bool], within_s: float
) -> set[str]:
    deadline = time.monotonic() + within_s
    labels = await _labels(prepared, package)
    while not seen(labels) and time.monotonic() < deadline:
        labels = await _labels(prepared, package)
    return labels


@pytest.mark.parametrize("app", APPS)
async def test_the_app_opens_on_its_own_screen_without_asking_for_storage(
    prepared: Prepared, app: str
) -> None:
    package, activity, own, asks = APPS[app]
    prompts = (*asks, SYSTEM_PAGE)
    mode = (
        await prepared.adb.shell(
            prepared.serial, f"appops get --uid {package} MANAGE_EXTERNAL_STORAGE"
        )
    ).stdout.decode()
    assert "MANAGE_EXTERNAL_STORAGE: allow" in mode, mode

    await prepared.adb.shell(prepared.serial, f"am start -n {activity}")
    labels = await _labels_until(
        prepared,
        package,
        lambda labels: own in labels or any(prompt in labels for prompt in prompts),
        within_s=30,
    )
    # One more still screen: a prompt can come right after the app's own screen.
    labels |= await _labels(prepared, package)

    assert not any(prompt in labels for prompt in prompts), labels
    assert own in labels, labels
    print(f"{app}: opened on its own screen ({own!r}); no storage prompt")
