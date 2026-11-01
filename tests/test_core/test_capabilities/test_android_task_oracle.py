"""The Android task oracle: ``none``, and ``androidworld`` against a fake AndroidWorld service.

``tests/_helpers/fake_androidworld.py`` is started exactly as the oracle starts
the real service (through the installation's interpreter, with the launcher's
arguments, a whitelisted environment and a process group of its own), so these
cover the oracle's lifecycle, its error mapping, cancellation and that the
service's whole process group is gone on every path. The real service runs in
``test_androidworld_live.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.android.backends.base import (
    AdbEndpoint,
    AndroidProviderSession,
    DeviceNeeds,
    EmulatorAccess,
    EmulatorGrpc,
)
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.oracle import androidworld
from spectragent.core.capabilities.android.oracle.androidworld import (
    FORWARDER_APK,
    LAUNCHER,
    PINNED_COMMIT,
    AndroidWorldError,
    AndroidWorldOracle,
    AndroidWorldSetupError,
    checkout_commit,
    params_digest,
)
from spectragent.core.capabilities.android.oracle.androidworld_tasks import (
    AndroidWorldTask,
    instance_seed,
)
from spectragent.core.capabilities.android.oracle.none import NoTaskOracle
from spectragent.schemas.oracle_score import OracleReading
from tests._helpers.assembly import make_env
from tests._helpers.fake_androidworld import fake_installation, requests, service, write_behaviour
from tests._helpers.process_checks import gone, groups, soon

ADB_SERVER_PORT = 40123


def _task(**changes: Any) -> dict[str, Any]:
    """An AndroidWorld task record, as the task list holds it."""
    task, index, suite_seed = changes.get("task", "SystemBrightnessMax"), 0, 42
    record: dict[str, Any] = {
        "commit": PINNED_COMMIT,
        "family": "android",
        "task": task,
        "index": index,
        "suite_seed": suite_seed,
        "params": {"max_or_min": "max", "seed": instance_seed(suite_seed, task, index)},
        "goal": "Turn brightness to the max value.",
        "goal_made_at_initialize": False,
        "complexity": 1.0,
        "start_on_home_screen": True,
    }
    record.update(changes)
    return record


def _device(tmp_path: Path) -> AndroidProviderSession:
    """A booted emulator, as ``local_emulator`` hands it over with every need met."""
    return AndroidProviderSession(
        session_id="session",
        provider_id="local_emulator",
        endpoint=AdbEndpoint(
            host="127.0.0.1",
            port=ADB_SERVER_PORT,
            kind="server",
            encrypted=False,
            needs_adb_key=False,
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
def root(tmp_path: Path) -> Path:
    return fake_installation(tmp_path / "androidworld")


def _oracle(root: Path) -> AndroidWorldOracle:
    return AndroidWorldOracle(root=str(root), start_timeout_s=20.0, call_timeout_s=5.0)


def _calls(root: Path) -> list[tuple[str, str, dict[str, str]]]:
    return [(entry["method"], entry["path"], entry["query"]) for entry in requests(root)]


async def _service_is_gone(root: Path) -> bool:
    """Whether the service, the ``sleep`` it left in its group and the group itself are all gone."""
    started = service(root)
    return (
        gone(started["pid"])
        and await soon(lambda: gone(started["child"]))
        and await soon(lambda: started["group"] not in groups())
    )


# ── the lifecycle ────────────────────────────────────────────────────


async def test_a_task_is_prepared_scored_and_ended_and_the_service_is_gone_after(
    root: Path, tmp_path: Path
) -> None:
    oracle = _oracle(root)
    await oracle.start(_device(tmp_path), _task())
    reading = await oracle.read_score()
    await oracle.stop()

    task = AndroidWorldTask.model_validate(_task())
    assert reading == OracleReading(
        score=1.0,
        oracle="androidworld",
        oracle_version=PINNED_COMMIT,
        provenance={
            "family": "android",
            "task": "SystemBrightnessMax",
            "index": "0",
            "suite_seed": "42",
            "seed": str(task.params["seed"]),
            "params_sha256": params_digest(task.params),
        },
    )
    address = {"task_type": "SystemBrightnessMax", "task_idx": "0"}
    assert [call for call in _calls(root) if call[1] != "/health"] == [
        ("GET", "/suite/reinitialize", {"task_family": "android", "n_task_combinations": "1", "seed": "42"}),
        ("GET", "/task/goal", address),
        ("POST", "/task/initialize", address),
        ("POST", "/reset", {"go_home": "true"}),
        ("GET", "/task/score", address),
        ("POST", "/task/tear_down", address),
    ]  # fmt: skip
    assert await _service_is_gone(root)


async def test_the_service_runs_the_launcher_under_androidworlds_interpreter_with_a_bare_environment(
    root: Path, tmp_path: Path
) -> None:
    oracle = _oracle(root)
    device = _device(tmp_path)
    await oracle.start(device, _task())
    try:
        started = service(root)
    finally:
        await oracle.stop()

    argv = started["argv"]
    assert argv[:3] == ["-I", "-u", str(LAUNCHER)], "isolated, unbuffered, the launcher"
    pairs = dict(zip(argv[3::2], argv[4::2], strict=True))
    assert pairs == {
        "--port": pairs["--port"],
        "--src": str(root / "src"),
        "--adb": "/sdk/platform-tools/adb",
        "--adb-server-port": str(ADB_SERVER_PORT),
        "--console-port": "5556",
        "--grpc-target": "127.0.0.1:41000",
        "--grpc-token-file": "/run/pid_1.ini",
        "--forwarder-apk": str(root / "cache" / "a11y" / FORWARDER_APK),
    }
    env = {name: value for name, value in started["env"].items() if name not in _PYTHON_ADDS}
    assert env == {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "HOME": str(tmp_path / "run-home"),
        "TMPDIR": env["TMPDIR"],
        "GRPC_ENABLE_FORK_SUPPORT": "0",
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }
    assert not Path(env["TMPDIR"]).exists(), "its working directory is removed with it"


#: What the shell and the interpreter add to the environment the oracle hands over.
_PYTHON_ADDS = frozenset({"PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING", "OLDPWD"})


async def test_a_minwob_task_reads_its_goal_after_initializing_and_records_it(
    root: Path, tmp_path: Path
) -> None:
    goal = "Follow the instructions shown on the top of the screen: Click on the Cancel button."
    write_behaviour(root, {"/task/goal": {"json": {"goal": goal}}})
    task = _task(
        family="miniwob",
        task="click-button",
        params={"task_name": "click-button", "seed": instance_seed(42, "click-button", 0)},
        goal="Follow the instructions shown on the top of the screen.",
        goal_made_at_initialize=True,
        start_on_home_screen=False,
    )
    oracle = _oracle(root)
    await oracle.start(_device(tmp_path), task)
    try:
        reading = await oracle.read_score()
    finally:
        await oracle.stop()

    paths = [path for _, path, _ in _calls(root) if path != "/health"]
    assert paths[:4] == ["/suite/reinitialize", "/task/initialize", "/reset", "/task/goal"]
    assert ("POST", "/reset", {"go_home": "false"}) in _calls(root)
    assert reading is not None and reading.provenance["goal"] == goal


async def test_a_service_that_builds_another_task_is_refused_and_stopped(
    root: Path, tmp_path: Path
) -> None:
    write_behaviour(root, {"/task/goal": {"json": {"goal": "Turn brightness to the min value."}}})
    oracle = _oracle(root)

    with pytest.raises(
        AndroidWorldError, match="the task list records 'Turn brightness to the max"
    ):
        await oracle.start(_device(tmp_path), _task())
    await oracle.stop()  # what the capability's teardown does after a failed setup

    assert "/task/initialize" not in [path for _, path, _ in _calls(root)]
    assert await _service_is_gone(root)


# ── the service's own failures ───────────────────────────────────────


async def test_a_service_that_exits_while_starting_is_reported_with_its_log(
    root: Path, tmp_path: Path
) -> None:
    write_behaviour(root, {"exit_code": 3})
    oracle = _oracle(root)

    with pytest.raises(AndroidWorldError, match=r"exited with code 3[\s\S]*giving up"):
        await oracle.start(_device(tmp_path), _task())

    assert await _service_is_gone(root)


async def test_a_service_that_is_not_ready_in_time_is_stopped(root: Path, tmp_path: Path) -> None:
    write_behaviour(root, {"start_delay_s": 60})
    oracle = AndroidWorldOracle(root=str(root), start_timeout_s=1.0)

    with pytest.raises(TimeoutError, match="was not ready after 1 s"):
        await oracle.start(_device(tmp_path), _task())

    assert await _service_is_gone(root)


async def test_a_call_that_preparing_the_task_needs_is_bounded(root: Path, tmp_path: Path) -> None:
    write_behaviour(root, {"/task/initialize": {"hang": True}})
    oracle = AndroidWorldOracle(root=str(root), start_timeout_s=20.0, call_timeout_s=0.5)

    with pytest.raises(TimeoutError, match="POST /task/initialize: .* no answer within 0.5 s"):
        await oracle.start(_device(tmp_path), _task())
    await oracle.stop()

    assert await _service_is_gone(root)


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"status": 500}, r"GET /task/score answered HTTP 500"),
        ({"text": "<html>not json</html>"}, r"not a JSON object: '<html>not json</html>'"),
        ({"json": {"verdict": "yes"}}, r"no finite score: \{'verdict': 'yes'\}"),
        ({"json": {"score": "1.0"}}, r"no finite score"),
    ],
)
async def test_a_score_the_service_cannot_give_is_an_ordinary_error(
    root: Path, tmp_path: Path, rule: dict[str, Any], message: str
) -> None:
    oracle = _oracle(root)
    await oracle.start(_device(tmp_path), _task())
    try:
        write_behaviour(root, {"/task/score": rule})
        with pytest.raises(AndroidWorldError, match=message):
            await oracle.read_score()
    finally:
        await oracle.stop()
    assert await _service_is_gone(root)


async def test_a_dead_service_is_an_ordinary_error_that_says_so(root: Path, tmp_path: Path) -> None:
    oracle = _oracle(root)
    await oracle.start(_device(tmp_path), _task())
    try:
        assert oracle._service is not None
        oracle._service.process.kill()  # the oracle's own child
        await oracle._service.process.wait()
        with pytest.raises(AndroidWorldError, match="could not be reached .*exited with code"):
            await oracle.read_score()
    finally:
        await oracle.stop()
    assert await _service_is_gone(root)


async def test_a_failed_tear_down_is_logged_and_the_service_is_still_stopped(
    root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_behaviour(root, {"/task/tear_down": {"status": 500}})
    oracle = _oracle(root)
    await oracle.start(_device(tmp_path), _task())

    await oracle.stop()

    assert "tear-down of SystemBrightnessMax #0 failed" in caplog.text
    assert await _service_is_gone(root)


async def test_a_service_that_ignores_sigterm_is_killed_with_its_group(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(androidworld, "STOP_GRACE_S", 0.5)
    write_behaviour(root, {"ignore_sigterm": True})
    oracle = _oracle(root)
    await oracle.start(_device(tmp_path), _task())

    await oracle.stop()

    assert await _service_is_gone(root)


async def test_stop_is_safe_before_a_start_and_twice(root: Path, tmp_path: Path) -> None:
    oracle = _oracle(root)
    await oracle.stop()
    await oracle.start(_device(tmp_path), _task())
    await oracle.stop()
    await oracle.stop()

    assert [path for _, path, _ in _calls(root)].count("/task/tear_down") == 1


async def test_a_score_before_any_task_is_an_ordinary_error(root: Path) -> None:
    with pytest.raises(AndroidWorldError, match="no AndroidWorld task was initialized"):
        await _oracle(root).read_score()


async def test_a_device_without_the_emulators_grpc_is_refused(root: Path, tmp_path: Path) -> None:
    device = _device(tmp_path).model_copy(update={"emulator": None})

    with pytest.raises(AndroidWorldError, match="drives an emulator over its gRPC endpoint"):
        await _oracle(root).start(device, _task())
    assert requests(root) == [], "no service was started"


# ── cancellation ─────────────────────────────────────────────────────


async def test_a_start_cancelled_while_waiting_for_the_service_stops_it_before_ending(
    root: Path, tmp_path: Path
) -> None:
    write_behaviour(root, {"start_delay_s": 60})
    starting = asyncio.ensure_future(_oracle(root).start(_device(tmp_path), _task()))
    assert await soon(lambda: (root / "record" / "service.json").is_file(), within_s=20)

    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert await _service_is_gone(root)


async def test_a_cancelled_score_stays_a_cancellation(root: Path, tmp_path: Path) -> None:
    oracle = _oracle(root)
    await oracle.start(_device(tmp_path), _task())
    try:
        write_behaviour(root, {"/task/score": {"hang": True}})
        scoring = asyncio.ensure_future(oracle.read_score())
        assert await soon(lambda: "/task/score" in [p for _, p, _ in _calls(root)], within_s=20)

        scoring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scoring
    finally:
        await oracle.stop()
    assert await _service_is_gone(root)


# ── at the scoring point, through the environment ────────────────────


async def _environment_scores(oracle: AndroidWorldOracle, timeout_s: float) -> list[Any]:
    capability = AndroidCapability()
    capability.task_oracle = oracle
    env = make_env(AndroidEnvironment, {"android": capability})
    return await env.read_oracle_scores(timeout_s=timeout_s)


async def test_the_environment_records_the_score_and_each_way_of_not_scoring(
    root: Path, tmp_path: Path
) -> None:
    oracle = _oracle(root)
    await oracle.start(_device(tmp_path), _task())
    try:
        (scored,) = await _environment_scores(oracle, timeout_s=5.0)
        write_behaviour(root, {"/task/score": {"status": 500}})
        (failed,) = await _environment_scores(oracle, timeout_s=5.0)
        write_behaviour(root, {"/task/score": {"hang": True}})
        (late,) = await _environment_scores(oracle, timeout_s=0.5)
    finally:
        await oracle.stop()

    assert (scored.status, scored.score, scored.oracle) == ("scored", 1.0, "androidworld")
    assert (failed.status, failed.reason) == ("not_scored", "error")
    assert failed.detail is not None and failed.detail.startswith("AndroidWorldError: GET /task")
    assert (late.status, late.reason) == ("not_scored", "timeout")
    assert await _service_is_gone(root)


async def test_with_no_oracle_the_environment_records_nothing() -> None:
    env = make_env(AndroidEnvironment, {"android": AndroidCapability()})
    assert await env.read_oracle_scores(timeout_s=1.0) == []


# ── before anything starts: check_setup ──────────────────────────────


def test_what_the_androidworld_oracle_needs_of_the_device() -> None:
    assert AndroidWorldOracle(root="/aw").device_needs == DeviceNeeds(
        emulator_grpc=True, root=True, guest_wifi=True
    )


def test_a_good_installation_and_task_pass(root: Path) -> None:
    _oracle(root).check_setup(_task())


@pytest.mark.parametrize(
    ("breakage", "message"),
    [
        ("no root", "there is no AndroidWorld installation at"),
        ("no venv", "there is no AndroidWorld venv interpreter at"),
        ("no src", "there is no AndroidWorld checkout at"),
        ("other commit", "is at 0000000000000000000000000000000000000000, and the launcher"),
        ("no forwarder", "there is no cached accessibility forwarder at"),
    ],
)
def test_an_installation_the_service_cannot_start_from_is_refused(
    root: Path, breakage: str, message: str
) -> None:
    if breakage == "no root":
        root = root / "missing"
    elif breakage == "no venv":
        (root / ".venv" / "bin" / "python").unlink()
    elif breakage == "no src":
        (root / "src" / "server" / "android_server.py").unlink()
    elif breakage == "other commit":
        (root / "src" / ".git" / "HEAD").write_text("0" * 40 + "\n")
    else:
        (root / "cache" / "a11y" / FORWARDER_APK).unlink()

    with pytest.raises(AndroidWorldSetupError, match=message):
        _oracle(root).check_setup(_task())


@pytest.mark.parametrize(
    ("task", "message"),
    [
        (None, "the task carries no AndroidWorld task"),
        ({"task": "SystemBrightnessMax"}, "the task's oracle_task is not an AndroidWorld task"),
        (_task(commit="f" * 40), "the task list was made with AndroidWorld ffff"),
        (_task(family="information_retrieval"), "an information-retrieval task, scored on"),
        (_task(params={"seed": 1}), "params.seed is 1, and the instance AndroidWorld makes"),
    ],
)
def test_a_task_the_oracle_cannot_start_is_refused(
    root: Path, task: dict[str, Any] | None, message: str
) -> None:
    with pytest.raises(AndroidWorldSetupError, match=message):
        _oracle(root).check_setup(task)


def test_every_problem_is_named_at_once(root: Path) -> None:
    (root / "cache" / "a11y" / FORWARDER_APK).unlink()

    with pytest.raises(AndroidWorldSetupError) as refused:
        _oracle(root).check_setup(None)
    assert "forwarder" in str(refused.value) and "no AndroidWorld task" in str(refused.value)


@pytest.mark.parametrize("layout", ["detached", "loose ref", "packed ref", "gitdir file"])
def test_the_checkouts_commit_is_read_from_its_git_directory(tmp_path: Path, layout: str) -> None:
    src = tmp_path / "src"
    git = src / ".git"
    if layout == "gitdir file":
        git = tmp_path / "worktrees" / "src"
        src.mkdir()
        (src / ".git").write_text(f"gitdir: {git}\n")
    git.mkdir(parents=True)
    if layout == "detached":
        (git / "HEAD").write_text(f"{PINNED_COMMIT}\n")
    else:
        (git / "HEAD").write_text("ref: refs/heads/main\n")
    if layout in ("loose ref", "gitdir file"):
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "refs" / "heads" / "main").write_text(f"{PINNED_COMMIT}\n")
    if layout == "packed ref":
        (git / "packed-refs").write_text(
            f"# pack-refs with: peeled fully-peeled sorted\n{PINNED_COMMIT} refs/heads/main\n"
        )

    assert checkout_commit(src) == PINNED_COMMIT


def test_a_checkout_without_git_has_no_commit(tmp_path: Path) -> None:
    assert checkout_commit(tmp_path) is None


# ── none ─────────────────────────────────────────────────────────────


async def test_none_needs_nothing_scores_nothing_and_refuses_a_benchmark_task(
    tmp_path: Path,
) -> None:
    oracle = NoTaskOracle()
    assert oracle.device_needs == DeviceNeeds()
    oracle.check_setup(None)
    await oracle.start(_device(tmp_path), None)
    assert await oracle.read_score() is None
    await oracle.stop()

    with pytest.raises(ValueError, match="task_oracle is 'none': the task would run unprepared"):
        oracle.check_setup(_task())
