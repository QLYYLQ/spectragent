"""A Markor task prepared on a fresh boot opens Markor with no storage permission dialog.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). AndroidWorld's one-time app setup gives
Markor, Simple Gallery Pro and VLC All files access, and a fresh boot of
AndroidWorld's AVD lacks it, so every Markor run met Markor's "Storage
permission required" dialog and the system's All files access page first
(``docs/architecture_design.md`` §11.2 "启动器放在哪里"). The launcher grants it
again once the service is up (``serve.py``'s ``grant_all_files_access``). The
test checks, after setup:

- Markor's uid holds ``MANAGE_EXTERNAL_STORAGE`` as allow;
- Markor, launched as AndroidWorld launches apps, shows its files, not the
  dialog or the system page.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import UI_TREE, AndroidUITreeObs
from spectragent.core.capabilities.android.oracle.androidworld import AndroidWorldOracle
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from tests._helpers.android_runs import (
    androidworld_capability,
    installation_problem,
    write_task_list,
)

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
]

TASK = "MarkorMoveNote"
MARKOR = "net.gsantner.markor"
#: What Markor shows without the access: its own dialog, then the system's page.
ASKS = ("Storage permission required to read and write files", "All files access")
#: Markor's files screen: its button for a new file or folder.
FILES = "Create a new file or folder"


async def test_markor_opens_on_its_files_after_a_fresh_boot(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_task_list(data, TASK)
    context = AndroidWorldTaskLoader(data_dir=str(data)).get_task_context(f"{TASK}/42/0")
    capability = androidworld_capability()
    oracle = capability.task_oracle
    assert isinstance(oracle, AndroidWorldOracle)
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    env.check_setup(context.env)
    await env.setup(context.env)
    try:
        service, session = oracle._service, capability._session
        assert service is not None and session is not None
        granted = [
            line.split("serve.py: ", 1)[1]
            for line in service.log.read_text(errors="replace").splitlines()
            if "All files access" in line
        ]
        adb = AdbClient(session.endpoint.host, session.endpoint.port)
        mode = (
            await adb.shell(session.serial, f"appops get --uid {MARKOR} MANAGE_EXTERNAL_STORAGE")
        ).stdout.decode()
        assert "MANAGE_EXTERNAL_STORAGE: allow" in mode, mode

        # AndroidWorld's launch_app: am start of the app's activity.
        await adb.shell(
            session.serial, f"am start -n {MARKOR}/net.gsantner.markor.activity.MainActivity"
        )
        texts: set[str] = set()
        deadline = time.monotonic() + 30
        for cycle in range(1, 1000):
            await capability.begin_observation(cycle)
            tree = await capability.resolve_fragment(UI_TREE)
            assert isinstance(tree, AndroidUITreeObs)
            texts = {
                label
                for window in tree.tree().forest.windows
                for node in window.tree.nodes
                if node.is_visible_to_user and node.package_name in (MARKOR, "com.android.settings")
                for label in (node.text, node.content_description)
                if label
            }
            if FILES in texts or any(ask in texts for ask in ASKS) or time.monotonic() > deadline:
                break
        assert not any(ask in texts for ask in ASKS), texts
        assert FILES in texts, texts
    finally:
        await env.teardown()

    print(f"{TASK}: Markor opened on its files; the launcher: {granted or 'granted nothing'}")
