"""One AndroidWorld task on a real emulator, prepared and scored by AndroidWorld's own service, with no agent.

Opt-in, like ``test_android_emulator_live.py`` (marker
``requires_android_emulator``): ``SPECTRAGENT_TEST_AVD`` names the AVD
AndroidWorld's one-time setup prepared, ``SPECTRAGENT_TEST_ANDROIDWORLD``
the AndroidWorld installation (its venv, its checkout at the pinned commit,
its cached forwarder; ``docs/architecture_design.md`` §11.2), and
``SPECTRAGENT_TEST_SERVICE_APK`` the device-side APK. In file order:

- ``task_list.py`` writes the task list for ``SystemBrightnessMax`` and
  AndroidWorld's ``open_app`` table under AndroidWorld's interpreter;
- the Android environment and the ``androidworld`` benchmark are composed
  through the real ``ConfigComposer``: the run's Android block writes no AVD
  and no app table, and the benchmark's part (the test AVD, AndroidWorld's
  table) is merged into it;
- the capability, hosting the benchmark's setup component, boots the device
  with what the component needs and starts the service, which prepares the
  task;
- the touch and pointer overlays are off, so screenshots do not carry them;
- our accessibility service is bound beside AndroidWorld's forwarder, which
  AndroidWorld's service wrote over the setting when it started;
- the unsolved task scores 0 and, once the brightness is set over adb, 1, with
  the pinned commit in the verdict's identity and the task in its evidence;
- ``open_app`` opens AndroidWorld's default-app names ``browser``, ``gallery``
  and ``email`` as its ``launch_app`` does: the app AndroidWorld's own command
  for the name's data URI brings to the front comes to the front;
- teardown leaves no service, emulator or adb server, and nothing listens on
  adb's default port 5037 that did not before.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest
import pytest_asyncio

from spectragent.benchmarks.androidworld.benchmark import AndroidWorldBenchmark
from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.core.capabilities.android.actions import AndroidOpenAppAction
from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.backends.base import AndroidProviderSession
from spectragent.core.capabilities.android.backends.local_emulator import LocalEmulatorProvider
from spectragent.core.capabilities.android.bridge import BridgeError, ForegroundApp
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.effects import AndroidOpenAppEffect
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.schemas.action import Action
from spectragent.schemas.verdict import Verdict
from tests._helpers.android_runs import (
    FORWARDER,
    androidworld_composed,
    installation_problem,
    write_task_list,
)
from tests._helpers.benchmark_data import ANDROIDWORLD_DEFAULT_APP_URIS
from tests._helpers.process_checks import gone, groups, soon

TASK = "SystemBrightnessMax"
TASK_ID = f"{TASK}/42/0"

pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(installation_problem() is not None, reason=str(installation_problem())),
    pytest.mark.asyncio(loop_scope="module"),
]


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@dataclass
class Prepared:
    """The device with the task prepared on it, and what was running before."""

    env: AndroidEnvironment
    capability: AndroidCapability
    setup: AndroidWorldSetup
    session: AndroidProviderSession
    service: dict[str, int]
    emulator_group: int
    listening_on_5037: bool
    scores: list[Verdict]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def prepared(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Prepared]:
    listening = _listening(5037)
    data = tmp_path_factory.mktemp("androidworld")
    write_task_list(data, TASK)
    run = androidworld_composed(data)
    env, capability, benchmark = (
        run.single("environment"),
        run.single("capability"),
        run.single("benchmark"),
    )
    assert isinstance(env, AndroidEnvironment) and isinstance(capability, AndroidCapability)
    assert isinstance(benchmark, AndroidWorldBenchmark)
    setup = benchmark.setup
    context = benchmark.task_loader.get_task_context(TASK_ID)
    verifiers = benchmark.live_verifiers()  # the benchmark's own, as the YAML lists it
    env.check_setup(context.env, setups=[setup], live_verifiers=verifiers)
    env.bind_setups([setup])
    env.bind_live_verifiers(verifiers)
    await env.setup(context.env)
    try:
        session, service, provider = capability._session, setup._service, capability.backend
        assert session is not None and service is not None
        assert isinstance(provider, LocalEmulatorProvider)
        run = provider._runs[session.session_id]  # what the run owns, for the last test
        assert run.emulator_group is not None
        yield Prepared(
            env=env,
            capability=capability,
            setup=setup,
            session=session,
            service={"pid": service.process.pid, "group": service.group.id},
            emulator_group=run.emulator_group.id,
            listening_on_5037=listening,
            scores=[],
        )
    finally:
        await env.teardown()


async def _shell(prepared: Prepared, command: str) -> str:
    client = AdbClient(prepared.session.endpoint.host, prepared.session.endpoint.port)
    return (await client.shell(prepared.session.serial, command)).stdout.decode().strip()


async def test_the_benchmark_gives_the_avd_and_androidworlds_app_table(prepared: Prepared) -> None:
    """Merged from the benchmark's part into a run block that writes neither (§4.10.1)."""
    capability = prepared.capability
    (open_app,) = [a for a in capability.actions if isinstance(a, AndroidOpenAppAction)]

    assert capability.backend.avd == os.environ["SPECTRAGENT_TEST_AVD"]  # type: ignore[attr-defined]
    assert open_app.apps is not None
    assert open_app.apps["opentracks"] == (
        "de.dennisguse.opentracks/de.dennisguse.opentracks.TrackListActivity"
    )
    assert open_app.apps["vlc"] == "org.videolan.vlc/.gui.MainActivity"


async def test_the_touch_and_pointer_overlays_are_off(prepared: Prepared) -> None:
    assert await _shell(prepared, "settings get system show_touches") == "0"
    assert await _shell(prepared, "settings get system pointer_location") == "0"


async def test_our_service_is_bound_beside_androidworlds_forwarder(prepared: Prepared) -> None:
    enabled = await _shell(prepared, "settings get secure enabled_accessibility_services")
    side = prepared.capability._side
    assert side is not None

    assert FORWARDER in enabled.split(":") and "org.spectragent.device/.DeviceService" in enabled
    assert (await side.service.ping()).uptime_ms > 0


async def test_the_task_scores_0_unsolved_and_1_once_solved(prepared: Prepared) -> None:
    (unsolved,) = await prepared.env.verify_live(timeout_s=60, answer=None)
    await _shell(prepared, "settings put system screen_brightness 255")
    (solved,) = await prepared.env.verify_live(timeout_s=60, answer=None)
    prepared.scores += [unsolved, solved]

    assert (unsolved.status, unsolved.score) == ("judged", 0.0), unsolved.detail
    assert (solved.status, solved.score) == ("judged", 1.0), solved.detail
    assert (solved.verifier.verifier_id, solved.verifier.version) == ("androidworld", PINNED_COMMIT)
    assert {key: solved.evidence[key] for key in ("family", "task", "index")} == {
        "family": "android",
        "task": TASK,
        "index": "0",
    }


async def test_default_app_names_open_what_androidworlds_own_command_opens(
    prepared: Prepared,
) -> None:
    """AndroidWorld's ``launch_app`` opens these with ``am start -a android.intent.action.VIEW -d <uri>``.

    Each name is opened twice from the home screen: by that command, sent as
    AndroidWorld sends it (``adb_utils._launch_default_app``), then by
    ``open_app``; the same app comes to the front both times. The app is
    stopped after each, so both start it from the same state: Gmail, opened
    again while its first screen is up, stays behind the home screen.
    """
    capability = prepared.capability
    (open_app,) = [a for a in capability.actions if isinstance(a, AndroidOpenAppAction)]
    assert open_app.default_app_uris == ANDROIDWORLD_DEFAULT_APP_URIS  # from the checkout
    home = await _home(prepared)

    for name in ("browser", "gallery", "email"):
        uri = ANDROIDWORLD_DEFAULT_APP_URIS[name]
        await _shell(prepared, f"am start -a android.intent.action.VIEW -d {uri}")
        theirs = await _opened(prepared, home)
        await _stop(prepared, theirs)
        opened = await capability.execute_action(
            Action(name="android.open_app", params={"app_name": name})
        )
        ours = await _opened(prepared, home)
        await _stop(prepared, ours)
        print(f"open_app({name!r}), {uri}: AndroidWorld's command opened {theirs}; ours {ours}")

        assert opened.success, opened.error
        assert opened.effect == AndroidOpenAppEffect(effect_id="android.open_app", uri=uri)
        assert ours.package not in (None, home.package)
        assert ours.package == theirs.package


async def test_a_uri_no_app_takes_fails_with_ams_reason(prepared: Prepared) -> None:
    """``am`` exits 0 and says on stderr that nothing resolves the intent."""
    with pytest.raises(BridgeError, match="unable to resolve Intent") as refused:
        await prepared.capability.bridge.launcher.view("nosuchscheme:")
    print(refused.value)


async def _home(prepared: Prepared) -> ForegroundApp:
    """Press home and return the home screen's app, once it has a window in front."""
    await prepared.capability.execute_action(
        Action(name="android.press_key", params={"key": "home"})
    )
    return await _shown(prepared, lambda front: True)


async def _stop(prepared: Prepared, front: ForegroundApp) -> None:
    """Stop the app *front* names, then go home."""
    if front.package is not None:
        await _shell(prepared, f"am force-stop {front.package}")
    await _home(prepared)


async def _opened(prepared: Prepared, home: ForegroundApp) -> ForegroundApp:
    """The app that came up over the home screen; a cold start takes seconds."""
    return await _shown(prepared, lambda front: front.package != home.package)


async def _shown(
    prepared: Prepared, wanted: Callable[[ForegroundApp], bool], within_s: float = 20.0
) -> ForegroundApp:
    """The app in front once it has a window and is *wanted*, or whatever is in front after *within_s*."""
    deadline = time.monotonic() + within_s
    front = await prepared.capability.bridge.apps.foreground()
    while not (front.window is not None and wanted(front)) and time.monotonic() < deadline:
        await asyncio.sleep(0.2)  # polling a window change, at most within_s
        front = await prepared.capability.bridge.apps.foreground()
    return front


async def test_teardown_leaves_nothing_running(prepared: Prepared) -> None:
    await prepared.env.teardown()

    assert gone(prepared.service["pid"])
    assert await soon(lambda: prepared.service["group"] not in groups()), "its adb logcat too"
    assert await soon(lambda: prepared.emulator_group not in groups())
    assert not _listening(prepared.session.endpoint.port)
    assert _listening(5037) == prepared.listening_on_5037
    for score in prepared.scores:
        print(f"score {score.score} in {score.duration_ms} ms")
