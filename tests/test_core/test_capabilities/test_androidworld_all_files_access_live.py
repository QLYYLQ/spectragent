"""After a fresh-boot task setup, Markor and Simple Gallery Pro open without asking for storage, and VLC asks.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``;
``SPECTRAGENT_TEST_AVD``, ``SPECTRAGENT_TEST_ANDROIDWORLD`` and
``SPECTRAGENT_TEST_SERVICE_APK``). AndroidWorld's one-time app setup gives
Markor, Simple Gallery Pro and VLC All files access, and a fresh boot of
AndroidWorld's AVD lacks it, so every run of a task in them met the app's
storage prompt and the system's All files access page first
(``docs/architecture_design.md`` §11.2 "启动器放在哪里"). The launcher grants it
again to Markor and Simple Gallery Pro once the service is up (``serve.py``'s
``grant_all_files_access``). It leaves VLC out: VLC granted it beforehand, then
opened at the activity AndroidWorld's app table names, never started its media
library. After one task's setup (``MarkorMoveNote``), each app is opened as
``open_app`` opens it with AndroidWorld's table, and the test checks:

- Markor and Simple Gallery Pro: the uid holds ``MANAGE_EXTERNAL_STORAGE`` as
  allow, and the app shows its own screen, without its storage prompt or the
  system's page;
- VLC: the uid does not hold it, and VLC shows its own prompt. Granted through
  that prompt and the system's page, as an agent grants it, VLC's media library
  works: its Video tab and its Playlists tab finish loading.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass

import pytest
import pytest_asyncio

from spectragent.benchmarks.androidworld.open_app import read_open_app_table
from spectragent.core.capabilities.android.actions import open_app
from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import UI_TREE, AndroidUITreeObs
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.schemas.action import Action
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
#: The apps the launcher grants: each one's name in AndroidWorld's table, its package, a
#: label of its own screen, and what it shows without the access (seen in our seed-42 runs).
GRANTED = {
    "markor": (
        "net.gsantner.markor",
        "Create a new file or folder",
        ("Storage permission required to read and write files",),
    ),
    "simple gallery pro": (
        "com.simplemobiletools.gallery.pro",
        "More options",
        ("All files", "Media only"),
    ),
}
VLC = "org.videolan.vlc"
#: VLC's prompt, its dialog's button, and what its Video and Playlists tabs show
#: until its media library answers (seen in our seed-42 runs).
VLC_PROMPT, VLC_CONFIRM, VLC_LOADING = "GRANT PERMISSION", "OK", "Loading"
#: The system's page an app's prompt leads to, and the switch on it.
SYSTEM_PAGE, SYSTEM_SWITCH = "All files access", "Allow access to manage all files"


@dataclass
class Prepared:
    capability: AndroidCapability
    adb: AdbClient
    serial: str
    #: AndroidWorld's app table, as the benchmark hands it to ``open_app``.
    apps: Mapping[str, str]
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
        yield Prepared(
            capability=capability,
            adb=AdbClient(session.endpoint.host, session.endpoint.port),
            serial=session.serial,
            apps=read_open_app_table(data),
        )
    finally:
        await env.teardown()


async def _tree(prepared: Prepared) -> AndroidUITreeObs:
    prepared.cycle += 1
    await prepared.capability.begin_observation(prepared.cycle)
    tree = await prepared.capability.resolve_fragment(UI_TREE)
    assert isinstance(tree, AndroidUITreeObs)
    return tree


@dataclass(frozen=True)
class Screen:
    """The texts and descriptions on the screen of an app and of the system's settings.

    Attributes:
        labels: Every one of them.
        selected: Those of selected nodes: VLC's tab in front.
    """

    labels: frozenset[str]
    selected: frozenset[str]


async def _screen(prepared: Prepared, package: str) -> Screen:
    tree = await _tree(prepared)
    nodes = [
        node
        for window in tree.tree().forest.windows
        for node in window.tree.nodes
        if node.is_visible_to_user and node.package_name in (package, "com.android.settings")
    ]
    return Screen(
        labels=frozenset(
            label for node in nodes for label in (node.text, node.content_description) if label
        ),
        selected=frozenset(
            label
            for node in nodes
            if node.is_selected
            for label in (node.text, node.content_description)
            if label
        ),
    )


async def _screen_until(
    prepared: Prepared, package: str, seen: Callable[[Screen], bool], within_s: float
) -> Screen:
    deadline = time.monotonic() + within_s
    screen = await _screen(prepared, package)
    while not seen(screen) and time.monotonic() < deadline:
        screen = await _screen(prepared, package)
    return screen


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


async def _mode(prepared: Prepared, package: str) -> str:
    """The All files access app op of *package*'s uid, as ``appops get`` reports it."""
    command = f"appops get --uid {package} MANAGE_EXTERNAL_STORAGE"
    return (
        await asyncio.wait_for(prepared.adb.shell(prepared.serial, command), 10)
    ).stdout.decode()


async def _allowed_within(prepared: Prepared, package: str, within_s: float) -> str:
    """:func:`_mode` once it says allow, or as it reads when *within_s* is over.

    Settings sets the op when it handles the tap on its switch, which can come
    after the tap returns: on a fresh boot under a load of about 20, a read
    80 ms after the tap said default and the next, at 130 ms, allow (2026-11-04).
    """
    deadline = time.monotonic() + within_s
    mode = await _mode(prepared, package)
    while "MANAGE_EXTERNAL_STORAGE: allow" not in mode and time.monotonic() < deadline:
        mode = await _mode(prepared, package)
    return mode


async def _open(prepared: Prepared, name: str) -> None:
    """Open the app *name* names in AndroidWorld's table, as ``open_app`` opens it."""
    refused = await open_app(prepared.capability, prepared.apps, name)
    assert refused is None, refused


@pytest.mark.parametrize("app", GRANTED)
async def test_a_granted_app_opens_on_its_own_screen_without_asking_for_storage(
    prepared: Prepared, app: str
) -> None:
    package, own, asks = GRANTED[app]
    prompts = (*asks, SYSTEM_PAGE)
    mode = await _mode(prepared, package)
    assert "MANAGE_EXTERNAL_STORAGE: allow" in mode, mode

    await _open(prepared, app)
    screen = await _screen_until(
        prepared,
        package,
        lambda screen: own in screen.labels or any(p in screen.labels for p in prompts),
        within_s=30,
    )
    # One more still screen: a prompt can come right after the app's own screen.
    labels = screen.labels | (await _screen(prepared, package)).labels

    assert not any(prompt in labels for prompt in prompts), labels
    assert own in labels, labels
    print(f"{app}: opened on its own screen ({own!r}); no storage prompt")


async def test_vlc_asks_for_storage_and_its_library_works_once_granted_through_its_prompt(
    prepared: Prepared,
) -> None:
    mode = await _mode(prepared, VLC)
    assert "MANAGE_EXTERNAL_STORAGE: allow" not in mode, mode

    await _open(prepared, "vlc")
    asked = await _screen_until(
        prepared, VLC, lambda screen: VLC_PROMPT in screen.labels, within_s=30
    )
    assert VLC_PROMPT in asked.labels, f"VLC asked for no storage: {sorted(asked.labels)}"

    await _click(prepared, VLC_PROMPT)
    await _screen_until(prepared, VLC, lambda screen: VLC_CONFIRM in screen.labels, within_s=15)
    await _click(prepared, VLC_CONFIRM)
    await _screen_until(prepared, VLC, lambda screen: SYSTEM_SWITCH in screen.labels, within_s=15)
    await _click(prepared, SYSTEM_SWITCH)
    granted = await _allowed_within(prepared, VLC, within_s=10)
    assert "MANAGE_EXTERNAL_STORAGE: allow" in granted, granted
    back = await prepared.capability.execute_action(
        Action(name="android.press_key", params={"key": "back"})
    )
    assert back.success, back.error

    def loaded(tab: str) -> Callable[[Screen], bool]:
        def seen(screen: Screen) -> bool:
            return tab in screen.selected and not {VLC_PROMPT, VLC_LOADING} & screen.labels

        return seen

    video = await _screen_until(prepared, VLC, loaded("Video"), within_s=60)
    assert loaded("Video")(video), f"VLC's Video tab did not finish loading: {video}"
    await _click(prepared, "Playlists")
    playlists = await _screen_until(prepared, VLC, loaded("Playlists"), within_s=60)
    assert loaded("Playlists")(playlists), f"VLC's Playlists tab did not finish: {playlists}"
    print(f"vlc: asked, granted through its prompt; Video tab {sorted(video.labels)}")
    print(f"vlc: Playlists tab {sorted(playlists.labels)}")
