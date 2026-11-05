"""The ``venusbench`` setup component and live verifier against a fake AndroidWorld service.

They are AndroidWorld's (``androidworld/service_setup.py``, ``scoring.py``), so
``tests/test_benchmarks/test_androidworld/test_setup.py`` covers the shared
lifecycle; this covers what VenusBench gives them: its installation and
launcher, its pinned code, the tasks it leaves out, an answer handed over for
every task, and no goal read again after the reset. The real service runs in
``tests/test_core/test_capabilities/test_venusbench_live.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue

from spectragent.benchmarks.androidworld.service import AndroidWorldError, AndroidWorldSetupError
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.benchmarks.androidworld.tasks import instance_seed
from spectragent.benchmarks.venusbench.service import LAUNCHER, VenusBenchInstallation
from spectragent.benchmarks.venusbench.setup import VenusBenchSetup
from spectragent.benchmarks.venusbench.tasks import UNSUPPORTED, VENUSBENCH
from spectragent.benchmarks.venusbench.verifier import VenusBenchVerifier
from spectragent.core.capabilities.android.backends.base import (
    AdbEndpoint,
    AndroidProviderSession,
    EmulatorAccess,
    EmulatorGrpc,
)
from spectragent.schemas.verdict import VerifierIdentity
from tests._helpers.fake_androidworld import fake_venusbench_installation, requests, service

TASK = "VagueSystemBrightnessMax"
GOAL = "It's too dark on the screen; nothing is visible."


def _task(**changes: JsonValue) -> dict[str, JsonValue]:
    """A VenusBench task record, as its task list holds it."""
    task = changes.get("task", TASK)
    assert isinstance(task, str)
    record: dict[str, JsonValue] = {
        "commit": VENUSBENCH.commit,
        "family": "venus",
        "task": task,
        "index": 0,
        "suite_seed": 30,
        "params": {"seed": instance_seed(30, task, 0)},
        "goal": GOAL,
        "goal_at_initialize": "unchanged",
        "complexity": 2.0,
    }
    record.update(changes)
    return record


def _device(tmp_path: Path) -> AndroidProviderSession:
    return AndroidProviderSession(
        session_id="session",
        provider_id="local_emulator",
        endpoint=AdbEndpoint(
            host="127.0.0.1", port=40123, kind="server", encrypted=False, needs_adb_key=False
        ),
        serial="emulator-5556",
        emulator=EmulatorAccess(
            console_port=5556,
            home=str(tmp_path / "run-home"),
            adb="/sdk/platform-tools/adb",
            grpc=EmulatorGrpc(host="127.0.0.1", port=41000, token_file="/run/pid_1.ini"),
        ),
    )


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    behaviour: dict[str, JsonValue] = {"/task/goal": {"json": {"goal": GOAL}}}
    return fake_venusbench_installation(tmp_path / "venusbench", monkeypatch, behaviour)


def _setup(root: Path) -> VenusBenchSetup:
    return VenusBenchSetup(root=str(root), start_timeout_s=20.0, call_timeout_s=5.0)


def _sent(root: Path) -> list[tuple[str, str, dict[str, str], JsonValue]]:
    found = []
    for entry in requests(root):
        method, path, query = entry["method"], entry["path"], entry["query"]
        assert isinstance(method, str) and isinstance(path, str) and isinstance(query, dict)
        found.append((method, path, {k: str(v) for k, v in query.items()}, entry["body"]))
    return found


async def test_a_task_is_prepared_on_venusbenchs_checkout_and_judged_with_its_answer(
    root: Path, tmp_path: Path
) -> None:
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    judged = await VenusBenchVerifier().judge(_device(tmp_path), setup, answer="106.775")
    await setup.stop()

    argv = service(root)["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--src") + 1] == str(root / "src")
    assert argv[argv.index("--service-app") + 1] == str(root / "server" / "android_server.py")
    assert argv[argv.index("--files") + 1] == str(root / "files" / "files_gui_browsing")
    address = {"task_type": TASK, "task_idx": "0"}
    assert _sent(root) == [
        ("GET", "/health", {}, None),
        (
            "GET",
            "/suite/reinitialize",
            {"task_family": "venus", "n_task_combinations": "1", "seed": "30"},
            None,
        ),
        ("GET", "/task/goal", address, None),
        ("POST", "/task/initialize", address, None),
        ("GET", "/task/start_on_home_screen", address, None),
        ("POST", "/reset", {"go_home": "true"}, None),
        ("POST", "/execute_action", {}, {"action_type": "answer", "text": "106.775"}),
        ("GET", "/task/score", address, None),
        ("POST", "/task/tear_down", address, None),
    ], "no goal is read again after the reset, and the answer goes before the score"
    assert setup.made_goal() is None and not setup.makes_goal(_task())
    assert not setup.answers_episode_over() and not await setup.episode_over()
    assert judged.score == 1.0
    assert judged.evidence["family"] == "venus" and judged.evidence["answer"] == "sent"
    assert VenusBenchVerifier().identity() == VerifierIdentity(
        verifier_id="venusbench", version=VENUSBENCH.commit
    )


async def test_a_run_without_an_answer_hands_none_over(root: Path, tmp_path: Path) -> None:
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    judged = await VenusBenchVerifier().judge(_device(tmp_path), setup, answer=None)
    await setup.stop()
    assert "/execute_action" not in [path for _, path, _, _ in _sent(root)]
    assert judged.evidence["answer"] == "none"


def test_the_launcher_is_venusbenchs() -> None:
    assert VenusBenchInstallation.launcher == LAUNCHER
    assert LAUNCHER.name == "serve.py" and LAUNCHER.parent.name == "venusbench_service"


@pytest.mark.parametrize("task", sorted(UNSUPPORTED)[:3] + ["MultiRoundMarkorCreateNoteReverse"])
def test_a_task_the_first_version_does_not_run_is_refused_with_its_reason(
    root: Path, task: str
) -> None:
    with pytest.raises(AndroidWorldSetupError) as refused:
        _setup(root).check_setup(_task(task=task, params={"seed": instance_seed(30, task, 0)}))
    assert (
        str(refused.value)
        == f"the venusbench setup cannot start: {task} cannot run: " + (UNSUPPORTED[task])
    )


@pytest.mark.requires_benchmark_data("android/venusbench")
def test_the_pinned_commits_tasks_are_45_run_and_144_left_out_with_a_reason() -> None:
    """Against the task list fetched from the pinned commit (``spectragent benchmark fetch venusbench``)."""
    from spectragent.benchmarks.androidworld.tasks import read_task_list
    from spectragent.core.data_loader._common import resolve_data_dir, resolve_split_file

    data = resolve_data_dir("android", "venusbench", None)
    listed = {task.task for task in read_task_list(resolve_split_file(data, "All")).tasks}
    assert len(listed) == 189
    assert set(UNSUPPORTED) <= listed, "every task left out is one the pinned commit lists"
    assert len(UNSUPPORTED) == 144 and len(listed - set(UNSUPPORTED)) == 45
    assert all(reason.strip() for reason in UNSUPPORTED.values())


def test_a_task_listed_from_androidworld_is_refused(root: Path) -> None:
    from spectragent.benchmarks.androidworld.service import PINNED_COMMIT

    with pytest.raises(AndroidWorldSetupError, match="made with VenusBench " + PINNED_COMMIT):
        _setup(root).check_setup(_task(commit=PINNED_COMMIT))


def test_what_venusbenchs_service_needs_is_named(root: Path) -> None:
    (root / "server" / "android_server.py").write_text("another file\n")
    (root / "files" / "files_gui_browsing" / "zip" / "a.zip").unlink()
    with pytest.raises(AndroidWorldSetupError) as refused:
        _setup(root).check_setup(None)
    message = str(refused.value)
    assert f"{root / 'server' / 'android_server.py'} is not AndroidWorld's" in message
    assert "1 of VenusBench's external files are missing" in message
    assert message.endswith(
        f"spectragent benchmark setup <a benchmark YAML whose venusbench setup has root {root}> "
        "installs VenusBench there"
    )


async def test_venusbenchs_verifier_refuses_androidworlds_setup(tmp_path: Path) -> None:
    with pytest.raises(AndroidWorldError, match="the venusbench setup component started"):
        await VenusBenchVerifier().judge(
            _device(tmp_path), AndroidWorldSetup(root=str(tmp_path)), answer=None
        )
