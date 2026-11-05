"""VenusBench tasks on a real emulator, prepared and scored by AndroidWorld's service on VenusBench's checkout, with no agent.

Opt-in, like ``test_androidworld_live.py`` (marker ``requires_android_emulator``,
which also asks for ``SPECTRAGENT_TEST_AVD``): :data:`VENUSBENCH_VARIABLE` names
the VenusBench installation (``spectragent benchmark setup venusbench``), and
``SPECTRAGENT_TEST_SERVICE_APK`` the device-side APK; the AVD is the one the
shipped benchmark names, ``VenusBenchAvd``. Each test boots the device for its
task (``docs/architecture_design.md`` §11.2 "VenusBench-Mobile 的接入方案"):

- ``task_list.py`` writes the task list and the fork's ``open_app`` table under
  VenusBench's interpreter, as its fetch does (family ``venus``, seed 30);
- the Android environment and the shipped ``venusbench`` benchmark are composed
  through the real ``ConfigComposer``: its part (the AVD, the fork's table) is
  merged into a run block that writes neither;
- ``VagueSystemBrightnessMax``, scored on the device's state: 0 unsolved, 1 once
  the brightness is set over adb, with the pinned commit in the verdict;
- ``VagueCurrencyExchange``, scored by a rule on the answer, which the verifier
  hands over for every task: 0 with none, 0 with a wrong one, 1 with the right
  one;
- teardown leaves no service and no emulator, and nothing on adb's port 5037
  that was not there before.
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import JsonValue

from spectragent.benchmarks.androidworld.data import TASK_LIST
from spectragent.benchmarks.selection import read_benchmark
from spectragent.benchmarks.venusbench.benchmark import VenusBenchBenchmark
from spectragent.benchmarks.venusbench.install import VenusBenchInstaller
from spectragent.benchmarks.venusbench.service import VenusBenchInstallation
from spectragent.benchmarks.venusbench.tasks import VENUSBENCH
from spectragent.core.capabilities.android.actions import AndroidOpenAppAction
from spectragent.core.capabilities.android.adb import AdbClient
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.composer import ConfigComposer
from tests._helpers.android_runs import APK_VARIABLE
from tests._helpers.process_checks import gone, groups, soon

#: The VenusBench installation to run; the module skips when it is unset.
VENUSBENCH_VARIABLE = "SPECTRAGENT_TEST_VENUSBENCH"


def _problem() -> str | None:
    root = os.environ.get(VENUSBENCH_VARIABLE)
    if not root:
        return f"set {VENUSBENCH_VARIABLE} to a VenusBench installation"
    if not os.environ.get(APK_VARIABLE):
        return f"set {APK_VARIABLE} to the device-side APK"
    installation = VenusBenchInstallation(Path(root).expanduser())
    return "; ".join(VenusBenchInstaller(installation, "VenusBenchAvd").problems()) or None


pytestmark = [
    pytest.mark.requires_android_emulator,
    pytest.mark.skipif(_problem() is not None, reason=str(_problem())),
]


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _write_task_list(out: Path, task: str) -> dict[str, JsonValue]:
    """Write the list of *task* as VenusBench's fetch makes it, into *out*; its record."""
    import json

    installation = VenusBenchInstallation(Path(os.environ[VENUSBENCH_VARIABLE]).expanduser())
    subprocess.run(
        [str(installation.python), "-I", str(TASK_LIST), "--src", str(installation.src)]
        + ["--out", str(out), "--holders", "venus", "--seed", "30", "--family", "venus"]
        + ["--tasks", task],
        check=True,
        capture_output=True,
        timeout=300,
        env={"PATH": "/usr/bin:/bin", "HOME": str(out)},
    )
    (record,) = json.loads((out / "All.json").read_text())["tasks"]
    return record


@asynccontextmanager
async def _prepared(
    data: Path, task: str
) -> AsyncIterator[tuple[AndroidEnvironment, AndroidCapability, dict[str, JsonValue]]]:
    """The device booted with *task* prepared on it by the shipped benchmark's setup."""
    record = _write_task_list(data, task)
    block = read_benchmark("venusbench")
    block["task_loader"] = {"benchmark_name": "venusbench", "data_dir": str(data)}
    block["setup"] = {"setup_id": "venusbench", "root": os.environ[VENUSBENCH_VARIABLE]}
    root: dict[str, JsonValue] = {
        "environment": [
            {
                "env_type": "android",
                "capabilities": [
                    {
                        "capability_id": "android",
                        "service_apk": os.environ[APK_VARIABLE],
                        "tap_injector": {"injector_id": "shell_injection"},
                        "typing_backend": {"typing_id": "key_events", "fold_accents": True},
                        "ui_tree_cleaner": {"cleaner_id": "androidworld"},
                        "ui_tree_serializer": {"serializer_id": "m3a"},
                    }
                ],
            }
        ],
        "benchmark": [block],  # type: ignore[list-item]
    }
    run = ConfigComposer.for_config(root).compose(root)
    env, capability, benchmark = (
        run.single("environment"),
        run.single("capability"),
        run.single("benchmark"),
    )
    assert isinstance(env, AndroidEnvironment) and isinstance(capability, AndroidCapability)
    assert isinstance(benchmark, VenusBenchBenchmark)
    task_id = f"{task}/30/0"
    context = benchmark.task_loader.get_task_context(task_id)
    verifiers = benchmark.live_verifiers()
    env.check_setup(context.env, setups=[benchmark.setup], live_verifiers=verifiers)
    env.bind_setups([benchmark.setup])
    env.bind_live_verifiers(verifiers)
    await env.setup(context.env)
    try:
        yield env, capability, record
    finally:
        await env.teardown()


async def _shell(capability: AndroidCapability, command: str) -> str:
    session = capability._session
    assert session is not None
    client = AdbClient(session.endpoint.host, session.endpoint.port)
    return (await client.shell(session.serial, command)).stdout.decode().strip()


async def test_a_task_scored_on_the_device_scores_0_unsolved_and_1_once_solved(
    tmp_path: Path,
) -> None:
    listening = _listening(5037)
    async with _prepared(tmp_path, "VagueSystemBrightnessMax") as (env, capability, _):
        (open_app,) = [a for a in capability.actions if isinstance(a, AndroidOpenAppAction)]
        assert capability.backend.avd == "VenusBenchAvd"  # type: ignore[attr-defined]
        assert open_app.apps is not None and "zipxtract" in open_app.apps
        session, setup = capability._session, capability._setup
        assert session is not None and setup is not None
        service = setup._service  # type: ignore[attr-defined]
        (unsolved,) = await env.verify_live(timeout_s=60, answer=None)
        await _shell(capability, "settings put system screen_brightness 255")
        (solved,) = await env.verify_live(timeout_s=60, answer=None)
    assert (unsolved.status, unsolved.score) == ("judged", 0.0), unsolved.detail
    assert (solved.status, solved.score) == ("judged", 1.0), solved.detail
    assert (solved.verifier.verifier_id, solved.verifier.version) == (
        "venusbench",
        VENUSBENCH.commit,
    )
    assert solved.evidence["family"] == "venus" and solved.evidence["answer"] == "none"
    assert gone(service.process.pid)
    assert await soon(lambda: service.group.id not in groups())
    assert not _listening(session.endpoint.port)
    assert _listening(5037) == listening


async def test_a_task_scored_on_the_answer_gets_the_runs_answer(tmp_path: Path) -> None:
    async with _prepared(tmp_path, "VagueCurrencyExchange") as (env, _, record):
        params = record["params"]
        assert isinstance(params, dict) and isinstance(params["answer"], str)
        (none,) = await env.verify_live(timeout_s=60, answer=None)
        (wrong,) = await env.verify_live(timeout_s=60, answer="1.000")
        (right,) = await env.verify_live(timeout_s=60, answer=f"It is {params['answer']}.")
    assert [verdict.score for verdict in (none, wrong, right)] == [0.0, 0.0, 1.0], [
        verdict.detail for verdict in (none, wrong, right)
    ]
    assert right.evidence["answer"] == "sent"
