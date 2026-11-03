"""One AndroidWorld task on a real emulator, prepared and scored by AndroidWorld's own service, with no agent.

Opt-in, like ``test_android_emulator_live.py`` (marker
``requires_android_emulator``): ``SPECTRAGENT_TEST_AVD`` names the AVD
AndroidWorld's one-time setup prepared, ``SPECTRAGENT_TEST_ANDROIDWORLD``
the AndroidWorld installation (its venv, its checkout at the pinned commit,
its cached forwarder; ``docs/architecture_design.md`` §11.2), and
``SPECTRAGENT_TEST_SERVICE_APK`` the device-side APK. In file order:

- ``task_list.py`` writes the task list for ``SystemBrightnessMax`` under
  AndroidWorld's interpreter, and the ``androidworld`` loader reads it;
- the capability, composed from YAML with ``task_oracle: androidworld``, boots
  the device with what the oracle needs and starts the service, which
  prepares the task;
- the touch and pointer overlays are off, so screenshots do not carry them;
- our accessibility service is bound beside AndroidWorld's forwarder, which
  AndroidWorld's service wrote over the setting when it started;
- the unsolved task scores 0 and, once the brightness is set over adb, 1, with
  the oracle's version in the verdict's identity and the task in its evidence;
- teardown leaves no service, emulator or adb server, and nothing listens on
  adb's default port 5037 that did not before.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio

from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.backends.base import AndroidProviderSession
from spectragent.core.capabilities.android.backends.local_emulator import LocalEmulatorProvider
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.oracle.androidworld import (
    PINNED_COMMIT,
    AndroidWorldOracle,
)
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.schemas.verdict import Verdict
from tests._helpers.android_runs import (
    FORWARDER,
    androidworld_capability,
    installation_problem,
    write_task_list,
)
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
    oracle: AndroidWorldOracle
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
    context = AndroidWorldTaskLoader(data_dir=str(data)).get_task_context(TASK_ID)
    capability = androidworld_capability()
    oracle = capability.task_oracle
    assert isinstance(oracle, AndroidWorldOracle)
    env = AndroidEnvironment(capabilities={"android": capability})  # type: ignore[call-arg]
    env.check_setup(context.env)
    await env.setup(context.env)
    try:
        session, service, provider = capability._session, oracle._service, capability.backend
        assert session is not None and service is not None
        assert isinstance(provider, LocalEmulatorProvider)
        run = provider._runs[session.session_id]  # what the run owns, for the last test
        assert run.emulator_group is not None
        yield Prepared(
            env=env,
            capability=capability,
            oracle=oracle,
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


async def test_teardown_leaves_nothing_running(prepared: Prepared) -> None:
    await prepared.env.teardown()

    assert gone(prepared.service["pid"])
    assert await soon(lambda: prepared.service["group"] not in groups()), "its adb logcat too"
    assert await soon(lambda: prepared.emulator_group not in groups())
    assert not _listening(prepared.session.endpoint.port)
    assert _listening(5037) == prepared.listening_on_5037
    for score in prepared.scores:
        print(f"score {score.score} in {score.duration_ms} ms")
