"""The ``androidworld`` setup component and live verifier against a fake AndroidWorld service.

``tests/_helpers/fake_androidworld.py`` is started exactly as the setup starts
the real service (through the installation's interpreter, with the launcher's
arguments, a whitelisted environment and a process group of its own), so these
cover the setup's lifecycle, its error mapping, cancellation and that the
service's whole process group is gone on every path, and the judgment
AndroidWorld's live verifier reads from the service the setup started. The
real service runs in ``tests/test_core/test_capabilities/test_androidworld_live.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import JsonValue

from spectragent.benchmarks.androidworld import service as androidworld
from spectragent.benchmarks.androidworld.service import (
    FORWARDER_APK,
    LAUNCHER,
    PINNED_COMMIT,
    AndroidWorldError,
    AndroidWorldSetupError,
    checkout_commit,
    goal_after_initialize,
    params_digest,
)
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.benchmarks.androidworld.tasks import AndroidWorldTask, instance_seed
from spectragent.benchmarks.androidworld.verifier import AndroidWorldVerifier
from spectragent.core.capabilities.android.backends.base import (
    AdbEndpoint,
    AndroidProviderSession,
    DeviceNeeds,
    EmulatorAccess,
    EmulatorGrpc,
    UnmetDeviceNeeds,
)
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.schemas.verdict import Judgment, Verdict, VerifierIdentity
from spectragent.verifiers.base import attachment_of
from tests._helpers.android_device import unset_capability
from tests._helpers.assembly import make_env
from tests._helpers.fake_androidworld import fake_installation, requests, service, write_behaviour
from tests._helpers.process_checks import gone, groups, soon

ADB_SERVER_PORT = 40123


def _task(**changes: JsonValue) -> dict[str, JsonValue]:
    """An AndroidWorld task record, as the task list holds it."""
    task, index, suite_seed = changes.get("task", "SystemBrightnessMax"), 0, 42
    assert isinstance(task, str)
    record: dict[str, JsonValue] = {
        "commit": PINNED_COMMIT,
        "family": "android",
        "task": task,
        "index": index,
        "suite_seed": suite_seed,
        "params": {"max_or_min": "max", "seed": instance_seed(suite_seed, task, index)},
        "goal": "Turn brightness to the max value.",
        "goal_at_initialize": "unchanged",
        "complexity": 1.0,
    }
    record.update(changes)
    return record


#: An information-retrieval task, which AndroidWorld scores on the agent's answer.
_ASKS = "SimpleCalendarLocationOfEvent"
_ASKS_GOAL = "What is the location of my Movie night event in Simple Calendar Pro?"


def _asking_task() -> dict[str, JsonValue]:
    return _task(
        family="information_retrieval",
        task=_ASKS,
        params={"seed": instance_seed(42, _ASKS, 0)},
        goal=_ASKS_GOAL,
    )


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
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return fake_installation(tmp_path / "androidworld", monkeypatch)


def _setup(root: Path) -> AndroidWorldSetup:
    return AndroidWorldSetup(root=str(root), start_timeout_s=20.0, call_timeout_s=5.0)


def _calls(root: Path) -> list[tuple[str, str, dict[str, str]]]:
    calls = []
    for entry in requests(root):
        method, path, query = entry["method"], entry["path"], entry["query"]
        assert isinstance(method, str) and isinstance(path, str) and isinstance(query, dict)
        calls.append((method, path, {name: str(value) for name, value in query.items()}))
    return calls


def _after_the_reset(root: Path) -> list[tuple[str, str, JsonValue]]:
    """The calls that followed the task's ``/reset``, each with its JSON body (``None`` without)."""
    sent = [(entry["method"], entry["path"], entry["body"]) for entry in requests(root)]
    paths = [path for _, path, _ in sent]
    return [
        (str(method), str(path), body) for method, path, body in sent[paths.index("/reset") + 1 :]
    ]


async def _judged(setup: AndroidWorldSetup, answer: str | None) -> Judgment:
    """AndroidWorld's live verifier on the task *setup* started, handed *answer*.

    It reads no device of its own (AndroidWorld's check reads it through the
    service), so the one handed over is never touched.
    """
    return await AndroidWorldVerifier().judge(_device(Path("/unread")), setup, answer=answer)


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
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    made = setup.made_goal()
    reading = await _judged(setup, None)
    await setup.stop()

    assert made is None, "the task's goal is the listed one"
    task = AndroidWorldTask.model_validate(_task())
    assert AndroidWorldVerifier().identity() == VerifierIdentity(
        verifier_id="androidworld", version=PINNED_COMMIT
    )
    assert reading == Judgment(
        score=1.0,
        evidence={
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
        ("GET", "/task/start_on_home_screen", address),
        ("POST", "/reset", {"go_home": "true"}),
        ("GET", "/task/score", address),
        ("POST", "/task/tear_down", address),
    ]  # fmt: skip
    assert await _service_is_gone(root)


async def test_the_service_runs_the_launcher_under_androidworlds_interpreter_with_a_bare_environment(
    root: Path, tmp_path: Path
) -> None:
    setup = _setup(root)
    device = _device(tmp_path)
    await setup.start(device, _task())
    try:
        started = service(root)
        argv, environment = started["argv"], started["env"]
        assert isinstance(argv, list) and isinstance(environment, dict)
        workdir = Path(str(environment["TMPDIR"])).parent
        links = {link.name: os.readlink(link) for link in (workdir / "bin").iterdir()}
    finally:
        await setup.stop()

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
    env = {name: value for name, value in environment.items() if name not in _PYTHON_ADDS}
    assert env == {
        "PATH": f"{workdir / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "HOME": str(tmp_path / "run-home"),
        "TMPDIR": str(workdir / "tmp"),
        "GRPC_ENABLE_FORK_SUPPORT": "0",
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }
    assert links == {"ffmpeg": shutil.which("ffmpeg")}, "the host's ffmpeg alone"
    assert not workdir.exists(), "its working directory is removed with it"


#: What the shell and the interpreter add to the environment the setup hands over.
_PYTHON_ADDS = frozenset({"PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING", "OLDPWD"})


#: A MiniWoB++ goal: the fixed part the list records, then the instruction the page made.
_MINIWOB_FIXED = "Follow the instructions shown on the top of the screen."
_MINIWOB_GOAL = (
    "Follow the instructions shown on the top of the screen: Click on the Cancel button."
)


def _miniwob_task() -> dict[str, JsonValue]:
    """A MiniWoB++ task as the list holds it: only its goal's fixed part is known before it starts."""
    return _task(
        family="miniwob",
        task="click-button",
        params={"task_name": "click-button", "seed": instance_seed(42, "click-button", 0)},
        goal=_MINIWOB_FIXED,
        goal_at_initialize="made",
    )


async def test_a_miniwob_task_reads_its_goal_after_initializing_and_that_goal_is_the_task_text(
    root: Path, tmp_path: Path
) -> None:
    write_behaviour(
        root,
        {
            "/task/goal": {"json": {"goal": _MINIWOB_GOAL}},
            "/task/start_on_home_screen": {"json": {"start_on_home_screen": False}},
        },
    )
    setup = _setup(root)
    await setup.start(_device(tmp_path), _miniwob_task())
    try:
        made = setup.made_goal()
        reading = await _judged(setup, None)
    finally:
        await setup.stop()

    paths = [path for _, path, _ in _calls(root) if path != "/health"]
    assert paths[:5] == [
        "/suite/reinitialize",
        "/task/initialize",
        "/task/start_on_home_screen",
        "/reset",
        "/task/goal",
    ]
    assert ("POST", "/reset", {"go_home": "false"}) in _calls(root)
    assert made == _MINIWOB_GOAL, "as AndroidWorld hands it to an agent, unchanged"
    assert reading.evidence["goal"] == _MINIWOB_GOAL


#: An information-retrieval goal naming a date, before and after AndroidWorld's initialize
#: rewords the date: the wording suite seed 42's instance draws, since initialize seeds the
#: draw with the instance's seed (``docs/architecture_design.md`` §11.2).
_DATED = "SimpleCalendarAnyEventsOnDate"
_DATED_BEFORE = (
    "Do I have any events October 26 2023 in Simple Calendar Pro? Answer with the titles only."
)
_DATED_AFTER = "Do I have any events the Thursday after next in Simple Calendar Pro? Answer with the titles only."


def _dated_task() -> dict[str, JsonValue]:
    """An information-retrieval task whose goal names a date, which initialize rewords."""
    return _task(
        family="information_retrieval",
        task=_DATED,
        params={"date": "October 26 2023", "seed": instance_seed(42, _DATED, 0)},
        goal=_DATED_BEFORE,
        goal_at_initialize="rewritten",
    )


def test_a_goal_made_or_rewritten_at_initialize_completes_the_task_text(root: Path) -> None:
    setup = _setup(root)
    assert setup.makes_goal(_miniwob_task())
    assert setup.makes_goal(_dated_task())
    assert not setup.makes_goal(_task())
    assert not setup.makes_goal(_asking_task())
    assert setup.made_goal() is None, "nothing was made before a start"


async def test_a_goal_rewritten_at_initialize_is_checked_before_it_and_read_after_it(
    root: Path, tmp_path: Path
) -> None:
    """As AndroidWorld's runner: it reads ``task.goal`` once the task is initialized."""
    write_behaviour(
        root, {"/task/goal": {"json_in_turn": [{"goal": _DATED_BEFORE}, {"goal": _DATED_AFTER}]}}
    )
    setup = _setup(root)
    await setup.start(_device(tmp_path), _dated_task())
    try:
        made = setup.made_goal()
    finally:
        await setup.stop()

    paths = [path for _, path, _ in _calls(root) if path != "/health"]
    assert paths[:6] == [
        "/suite/reinitialize",
        "/task/goal",
        "/task/initialize",
        "/task/start_on_home_screen",
        "/reset",
        "/task/goal",
    ]
    assert made == _DATED_AFTER


@pytest.mark.parametrize(
    ("goal", "message"),
    [
        (_MINIWOB_GOAL, None),
        ("Follow the instructions shown on the top of the screen:Click.", None),
        ("Follow the instructions shown on the top of the screen: ", "says nothing after"),
        ("Follow the instructions shown on the top of the screen:", "says nothing after"),
        ("Follow the instructions shown on the top of the screen.", "says nothing after"),
        ("Follow the instructions: Click on the Cancel button.", "does not start with the fixed"),
        ("", "the goal '', which is no goal"),
        (None, "the goal None, which is no goal"),
    ],
)
def test_a_made_goal_must_say_something_after_the_fixed_part_the_list_records(
    goal: object, message: str | None
) -> None:
    task = AndroidWorldTask.model_validate(_miniwob_task())
    if message is None:
        assert goal_after_initialize(task, goal) == goal
        return
    with pytest.raises(AndroidWorldError, match=message):
        goal_after_initialize(task, goal)


@pytest.mark.parametrize(
    ("goal", "message"),
    [(_DATED_AFTER, None), (_DATED_BEFORE, None), ("  ", "which is no goal"), (7, "is no goal")],
)
def test_a_rewritten_goal_must_be_a_goal(goal: object, message: str | None) -> None:
    """Its rewording is AndroidWorld's; the check before initialize compared the rest."""
    task = AndroidWorldTask.model_validate(_dated_task())
    if message is None:
        assert goal_after_initialize(task, goal) == goal
        return
    with pytest.raises(AndroidWorldError, match=message):
        goal_after_initialize(task, goal)


async def test_an_empty_goal_fails_the_start_and_the_service_is_stopped(
    root: Path, tmp_path: Path
) -> None:
    """A page that had shown no instruction when AndroidWorld read it leaves only the prefix."""
    empty = "Follow the instructions shown on the top of the screen: "
    write_behaviour(root, {"/task/goal": {"json": {"goal": empty}}})
    setup = _setup(root)

    with pytest.raises(
        AndroidWorldError,
        match=r"click-button #0 \(suite seed 42\) the goal '.*: ', which says nothing after its "
        r"fixed part: the page had shown no instruction when AndroidWorld read it",
    ):
        await setup.start(_device(tmp_path), _miniwob_task())
    await setup.stop()  # what the capability's teardown does after a failed setup

    assert setup.made_goal() is None
    assert await _service_is_gone(root)


# ── the episode: AndroidWorld ends a MiniWoB++ episode itself ────────


async def test_a_miniwob_episode_is_over_when_the_service_says_the_page_ended_it(
    root: Path, tmp_path: Path
) -> None:
    write_behaviour(root, {"/task/goal": {"json": {"goal": _MINIWOB_GOAL}}})
    setup = _setup(root)
    assert not setup.answers_episode_over(), "no task started yet"
    await setup.start(_device(tmp_path), _miniwob_task())
    try:
        answers = setup.answers_episode_over()
        before = await setup.episode_over()
        write_behaviour(root, {"/task/episode_over": {"json": {"over": True}}})
        after = await setup.episode_over()
    finally:
        await setup.stop()

    assert answers, "the capability waits for a still screen, then asks"
    assert (before, after) == (False, True)
    address = {"task_type": "click-button", "task_idx": "0"}
    asked = [
        (method, query) for method, path, query in _calls(root) if path == "/task/episode_over"
    ]
    assert asked == [("GET", address)] * 2


async def test_a_task_of_another_family_is_never_ended_by_its_page_and_nothing_is_asked(
    root: Path, tmp_path: Path
) -> None:
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    try:
        answers, over = setup.answers_episode_over(), await setup.episode_over()
    finally:
        await setup.stop()

    assert (answers, over) == (False, False), "the capability neither waits nor asks"
    assert "/task/episode_over" not in [path for _, path, _ in _calls(root)]


async def test_an_episode_question_the_service_cannot_answer_is_an_ordinary_error(
    root: Path, tmp_path: Path
) -> None:
    write_behaviour(root, {"/task/goal": {"json": {"goal": _MINIWOB_GOAL}}})
    setup = _setup(root)
    await setup.start(_device(tmp_path), _miniwob_task())
    try:
        for rule, message in [
            ({"status": 500}, r"GET /task/episode_over answered HTTP 500"),
            ({"json": {"over": "yes"}}, r"answered no over: \{'over': 'yes'\}"),
            ({"json": {}}, r"answered no over: \{\}"),
        ]:
            write_behaviour(root, {"/task/episode_over": rule})
            with pytest.raises(AndroidWorldError, match=message):
                await setup.episode_over()
        write_behaviour(root, {"/task/episode_over": {"hang": True}})
        setup.call_timeout_s = 0.5  # only the question that never answers waits on it
        with pytest.raises(TimeoutError, match=r"GET /task/episode_over: .* within 0.5 s"):
            await setup.episode_over()
    finally:
        await setup.stop()

    assert await _service_is_gone(root)


async def test_an_episode_question_before_any_task_is_an_ordinary_error(root: Path) -> None:
    with pytest.raises(AndroidWorldError, match="no AndroidWorld task was initialized"):
        await _setup(root).episode_over()


def _launcher() -> ModuleType:
    """The launcher, loaded from its file, whose top level imports only the standard library.

    AndroidWorld's interpreter runs it as a program; it is loaded here only to
    call the rule its ``/task/episode_over`` answers with.
    """
    spec = importlib.util.spec_from_file_location("androidworld_launcher", LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("raw", "over"),
    [
        ("", False),  # the page has reported nothing yet
        ("0", False),  # AndroidWorld's runner does not end the episode on a zero either
        ("0.0", False),
        ("1", True),
        ("-1", True),
        ("0.87", True),  # use-colorwheel: AndroidWorld's int() raises; its episode still ends
        ("-0.5", True),  # tic-tac-toe's draw
    ],
)
def test_the_launcher_ends_a_miniwob_episode_on_a_stored_reward_that_is_not_zero(
    raw: str, over: bool
) -> None:
    assert _launcher().reward_ends_episode(raw) is over


def test_the_launcher_refuses_a_reward_that_is_no_number() -> None:
    with pytest.raises(ValueError, match="could not convert"):
        _launcher().reward_ends_episode("lost")


#: What android_env raises when a pull finds a file the directory listing named gone.
_PULL_LOST_A_FILE = (
    "Error executing adb command: [adb -P 41000 -s emulator-5556 pull /data/data/net.osmand/"
    "databases/map_markers_db-journal /tmp/x]\nCaused by: Command 'adb pull' returned non-zero "
    "exit status 1.\nadb stdout: [b\"adb: error: failed to stat remote object '/data/data/"
    "net.osmand/databases/map_markers_db-journal': No such file or directory\\n\"]\n"
    "adb stderr: [None]"
)
#: The same words from the ``rm -r`` that empties the directory before the push.
_RM_LOST_A_FILE = (
    "Error executing adb command: [adb -P 41000 -s emulator-5556 shell rm -r /data/data/"
    "net.osmand/databases/*]\nCaused by: Command 'adb shell rm' returned non-zero exit "
    "status 1.\nadb stdout: [b'rm: /data/data/net.osmand/databases/map_markers_db-journal: "
    "No such file or directory\\n']\nadb stderr: [None]"
)
_TABLE = ("map_markers", "/data/data/net.osmand/databases/map_markers_db")


@dataclass
class _Tables:
    """AndroidWorld's table_exists, launch_app and delete_all_rows_from_table, faked for the launcher's wait."""

    #: What each look answers, in turn; the last one repeats.
    looks: list[bool | Exception]
    #: What each clear does, in turn (``None`` deletes the rows); later ones succeed.
    clears: list[Exception | None] = field(default_factory=list)
    env: object = field(default_factory=object)
    launched: list[str] = field(default_factory=list)
    cleared: list[tuple[object, ...]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def exists(self, table_name: str, remote_db_file_path: str, env: object) -> bool:
        assert (table_name, remote_db_file_path, env) == (*_TABLE, self.env)
        answer = self.looks.pop(0) if len(self.looks) > 1 else self.looks[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def launch(self, app_name: str, env: object) -> None:
        assert env is self.env
        self.launched.append(app_name)

    def clear(self, *arguments: object) -> None:
        self.cleared.append(arguments)
        outcome = self.clears.pop(0) if self.clears else None
        if outcome is not None:
            raise outcome

    def clear_once_the_table_is_there(self, wait_s: float = 5.0) -> None:
        wrapped = _launcher().wait_for_the_table(
            self.clear,
            exists=self.exists,
            launch=self.launch,
            warn=self.notes.append,
            wait_s=wait_s,
            poll_s=0.01,
        )
        wrapped(*_TABLE, self.env, "osmand")


async def test_the_episode_starts_where_the_initialized_task_says(
    root: Path, tmp_path: Path
) -> None:
    """A task may change start_on_home_screen in its initialize; the runner reads it only then."""
    write_behaviour(root, {"/task/start_on_home_screen": {"json": {"start_on_home_screen": False}}})
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())  # the list says it starts on the home screen
    await setup.stop()
    assert ("POST", "/reset", {"go_home": "false"}) in _calls(root)


async def test_a_start_the_service_does_not_say_is_refused(root: Path, tmp_path: Path) -> None:
    write_behaviour(root, {"/task/start_on_home_screen": {"json": {}}})
    setup = _setup(root)
    with pytest.raises(AndroidWorldError, match="answered no start_on_home_screen"):
        await setup.start(_device(tmp_path), _task())
    await setup.stop()


class _Taps:
    """AndroidWorld's find-and-tap, failing with each of *failures* in turn, then tapping."""

    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)
        self.calls: list[tuple[object, ...]] = []
        self.notes: list[str] = []
        self.slept: list[float] = []

    def find_and_click(self, *arguments: object) -> None:
        self.calls.append(arguments)
        if self.failures:
            raise self.failures.pop(0)

    def tap(self, tries: int = 3) -> None:
        wrapped = _launcher().tap_on_a_settled_screen(
            self.find_and_click, warn=self.notes.append, tries=tries, sleep=self.slept.append
        )
        wrapped("SAVE", "env")


_STALE = ValueError("Invalid element index: 15, must be between 0 and 12.")


def test_the_launcher_taps_again_when_the_index_went_stale_between_androidworlds_reads() -> None:
    taps = _Taps(_STALE)
    taps.tap()
    assert taps.calls == [("SAVE", "env"), ("SAVE", "env")]
    assert taps.slept == [1.0] and len(taps.notes) == 1 and "tapping again" in taps.notes[0]


def test_the_launcher_raises_the_last_stale_index_and_any_other_failure() -> None:
    taps = _Taps(_STALE, _STALE, _STALE)
    with pytest.raises(ValueError, match="Invalid element index"):
        taps.tap()
    assert len(taps.calls) == 3
    other = _Taps(ValueError("Element with text 'SAVE' not found"))
    with pytest.raises(ValueError, match="not found"):
        other.tap()
    assert len(other.calls) == 1 and other.notes == []


def test_the_launcher_clears_a_table_that_is_there_without_launching_the_app() -> None:
    tables = _Tables(looks=[True])

    tables.clear_once_the_table_is_there()

    assert tables.launched == [] and tables.notes == []
    assert tables.cleared == [(*_TABLE, tables.env, "osmand", None)]


def test_the_launcher_launches_the_app_once_and_clears_once_its_table_is_there() -> None:
    tables = _Tables(looks=[False, False, True])

    tables.clear_once_the_table_is_there()

    assert tables.launched == ["osmand"]
    assert tables.cleared == [(*_TABLE, tables.env, "osmand", None)]
    assert len(tables.notes) == 1 and tables.notes[0].startswith("osmand made table map_markers")


def test_the_launcher_hands_a_table_the_app_never_makes_over_after_its_limit() -> None:
    """AndroidWorld's own function then launches, sleeps and fails, as it always did."""
    tables = _Tables(looks=[False])

    started = time.monotonic()
    tables.clear_once_the_table_is_there(wait_s=0.2)
    waited = time.monotonic() - started

    assert 0.2 <= waited < 1.0
    assert tables.launched == ["osmand"] and len(tables.cleared) == 1
    assert tables.notes[0].startswith("osmand had made no table map_markers")


def test_the_launcher_counts_a_look_whose_pull_lost_a_file_as_no_table_yet() -> None:
    tables = _Tables(looks=[RuntimeError(_PULL_LOST_A_FILE), True])

    tables.clear_once_the_table_is_there()

    assert tables.launched == ["osmand"] and len(tables.cleared) == 1
    with pytest.raises(RuntimeError, match="device 'emulator-5556' not found"):
        _Tables(
            looks=[RuntimeError("device 'emulator-5556' not found")]
        ).clear_once_the_table_is_there()


def test_the_launcher_clears_once_more_when_androidworlds_own_pull_lost_a_file() -> None:
    """Its pulls come before it changes anything on the device; it runs once more, and only once."""
    tables = _Tables(looks=[True], clears=[RuntimeError(_PULL_LOST_A_FILE)])

    tables.clear_once_the_table_is_there()

    assert tables.cleared == [(*_TABLE, tables.env, "osmand", None)] * 2
    assert tables.launched == [] and "clearing again" in tables.notes[0]
    twice = _Tables(looks=[True], clears=[RuntimeError(_PULL_LOST_A_FILE)] * 2)
    with pytest.raises(RuntimeError, match="failed to stat remote object"):
        twice.clear_once_the_table_is_there()
    assert len(twice.cleared) == 2


def test_the_launcher_raises_the_rm_emptying_the_directory_without_clearing_again() -> None:
    """The same words from ``rm -r`` come after it deleted files, so the device has changed."""
    tables = _Tables(looks=[True], clears=[RuntimeError(_RM_LOST_A_FILE)])

    with pytest.raises(RuntimeError, match="shell rm -r"):
        tables.clear_once_the_table_is_there()

    assert len(tables.cleared) == 1


def test_the_launcher_counts_only_a_pull_as_one_that_lost_a_file() -> None:
    assert _launcher().pull_lost_a_file(RuntimeError(_PULL_LOST_A_FILE))
    assert not _launcher().pull_lost_a_file(RuntimeError(_RM_LOST_A_FILE))
    assert not _launcher().pull_lost_a_file(RuntimeError("No such file or directory"))


@dataclass
class _AppOps:
    """The device's shell, faked for the launcher's All files access grant: each app's uid mode."""

    allowed: set[str]
    commands: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def shell(self, command: list[str]) -> str:
        self.commands.append(command)
        verb, _, package, op, *mode = command[1:]
        assert (command[0], op) == ("appops", "MANAGE_EXTERNAL_STORAGE")
        if verb == "set":
            assert mode == ["allow"]
            self.allowed.add(package)
            return ""
        if package in self.allowed:
            return "MANAGE_EXTERNAL_STORAGE: allow; time=+1m ago\n"
        return "No operations.\nDefault mode: default\n"

    def grant(self) -> None:
        _launcher().grant_all_files_access(self.shell, warn=self.notes.append)


def test_the_launcher_grants_all_files_access_where_androidworlds_setup_left_none() -> None:
    ops = _AppOps(allowed={"com.simplemobiletools.gallery.pro"})

    ops.grant()

    assert ops.allowed == {"net.gsantner.markor", "com.simplemobiletools.gallery.pro"}
    assert [command[1] for command in ops.commands] == ["get", "set", "get"]
    assert ops.commands[1] == [
        "appops",
        "set",
        "--uid",
        "net.gsantner.markor",
        "MANAGE_EXTERNAL_STORAGE",
        "allow",
    ]
    assert len(ops.notes) == 1 and "All files access" in ops.notes[0]
    # VLC asks itself: granted beforehand, it never starts its media library.
    assert not any("org.videolan.vlc" in command for command in ops.commands)


@dataclass
class _Adb:
    """android_env's AdbController.execute_command, faked: what each command reached it with."""

    ran: list[tuple[object, list[str], float | None, bool]] = field(default_factory=list)

    def execute(
        self, controller: object, args: list[str], timeout: float | None, device_specific: bool
    ) -> bytes:
        self.ran.append((controller, args, timeout, device_specific))
        return b"what adb printed\n"


_FORWARDER_FLAGS = (
    "com.google.androidenv.accessibilityforwarder/"
    "com.google.androidenv.accessibilityforwarder.FlagsBroadcastReceiver"
)
#: Every broadcast the service can send, as AndroidWorld 55ddaf5 and android_env
#: 1.2.3 hand it to execute_command: AndroidWorld's through issue_generic_request,
#: directly or through send_android_intent, and android_env's own through its
#: send_broadcast request.
_BROADCASTS = {
    "the MiniWoB++ app's reads (miniwob_base._extract_data)": [
        "-a",
        "com.google.androidenv.miniwob.app.GET_REWARD_ACTION",
    ],
    "Clipper's clipboard write (adb_utils.set_clipboard_contents)": [
        "-a",
        "clipper.set",
        "-e",
        "text",
        "Hello\\ there",
    ],
    "Clipper's clipboard read (adb_utils.get_clipboard_contents)": ["-a", "clipper.get"],
    "Retro Music's media scan (retro_music._scan_music_directory)": [
        "-a",
        "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d",
        '"file:///storage/emulated/0/Music"',
    ],
    "the overlay (AsyncAndroidEnv.display_message, suite_utils' demo mode)": [
        "-a",
        "com.example.ACTION_UPDATE_OVERLAY",
        "--es",
        "task_type_string",
        '"Agent answered:"',
        "--es",
        "goal_string",
        '"Paris"',
    ],
    "the forwarder's tree logs (A11yGrpcWrapper._enable_a11y_tree_logs)": [
        "-a",
        "accessibility_forwarder.intent.action.ENABLE_ACCESSIBILITY_TREE_LOGS",
        "-n",
        _FORWARDER_FLAGS,
    ],
    "the forwarder's gRPC port (A11yGrpcWrapper._configure_grpc)": [
        "-a",
        'accessibility_forwarder.intent.action.SET_GRPC --ei "port" 41123',
        "-n",
        _FORWARDER_FLAGS,
    ],
}
#: Commands that are no broadcast, among them the other intent send_android_intent
#: builds and the activity start of android_env's start_activity request.
_OTHER_COMMANDS = {
    "an activity started by intent (send_android_intent's start)": [
        "shell",
        "am",
        "start",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        "content://contacts/people/",
    ],
    "an activity started by name (start_activity)": [
        "shell",
        "am",
        "start",
        "",
        "-W",
        "-n",
        "ca.zgrs.clipper/ca.zgrs.clipper.Main",
    ],
    "the task stack (get_current_activity)": ["shell", "am", "stack", "list"],
    "a shell command": ["shell", "appops", "get", "--uid", "org.videolan.vlc"],
    "a pull": ["pull", "/data/data/net.osmand/databases/map_markers_db", "/tmp/x"],
    "the devices (init_server)": ["devices"],
}


@pytest.mark.parametrize("intent", _BROADCASTS.values(), ids=_BROADCASTS)
def test_the_launcher_sends_every_broadcast_to_the_foreground_queue(intent: list[str]) -> None:
    adb, controller = _Adb(), object()
    execute_command = _launcher().broadcast_in_the_foreground(adb.execute)
    command = ["shell", "am", "broadcast", *intent]

    assert execute_command(controller, command, 10) == b"what adb printed\n"

    assert adb.ran == [
        (controller, ["shell", "am", "broadcast", "--receiver-foreground", *intent], 10, True)
    ]


@pytest.mark.parametrize("command", _OTHER_COMMANDS.values(), ids=_OTHER_COMMANDS)
def test_the_launcher_runs_every_other_adb_command_as_it_came(command: list[str]) -> None:
    adb, controller = _Adb(), object()
    execute_command = _launcher().broadcast_in_the_foreground(adb.execute)

    assert execute_command(controller, command) == b"what adb printed\n"
    execute_command(controller, command, timeout=5, device_specific=False)

    assert adb.ran == [(controller, command, None, True), (controller, command, 5, False)]


@dataclass
class _Page:
    """What AndroidWorld's UI tree shows of a MiniWoB++ page, faked for the launcher's waits for it."""

    #: The ids each look sees on the screen, in turn; the last one repeats.
    screens: list[set[str]]
    looks: int = 0
    notes: list[str] = field(default_factory=list)

    def shown(self) -> set[str]:
        self.looks += 1
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def wait(self, *, there: bool, wait_s: float = 5.0) -> None:
        _launcher().wait_for_the_cover(
            self.shown, there=there, warn=self.notes.append, wait_s=wait_s, poll_s=0.01
        )


#: A page still loading (only the status bar has ids), one whose onload has covered
#: the task until its episode starts, and one whose reset has started the episode
#: and hidden the cover.
_LOADING = {"com.android.systemui:id/clock"}
_COVERED = {"click-canvas", "sync-task-cover", "query", "subbtn"}
_RUNNING = {"click-canvas", "query", "subbtn"}


def test_the_launcher_resets_a_page_that_loaded_within_androidworlds_second_at_once() -> None:
    page = _Page(screens=[_COVERED])

    page.wait(there=True)

    assert page.looks == 1 and page.notes == []


def test_the_launcher_waits_for_a_page_still_loading_after_androidworlds_second() -> None:
    page = _Page(screens=[_LOADING, _LOADING, _COVERED])

    page.wait(there=True)

    assert page.looks == 3
    assert len(page.notes) == 1 and page.notes[0].endswith("its reset waited")


def test_the_launcher_reads_the_instruction_once_the_reset_has_started_the_episode() -> None:
    """count-shape: read before the page had made it, the instruction was empty."""
    page = _Page(screens=[_COVERED, _COVERED, _RUNNING])

    page.wait(there=False)

    assert page.looks == 3
    assert len(page.notes) == 1 and page.notes[0].endswith("the instruction's read waited")


@pytest.mark.parametrize(
    ("there", "screen", "message"),
    [
        (
            True,
            _LOADING,
            r"cover was not in AndroidWorld's UI tree 0.2 s after AndroidWorld's 1 s; "
            r"the tree last showed \['com.android.systemui:id/clock'\]",
        ),
        (
            False,
            _COVERED,
            r"cover was still in AndroidWorld's UI tree 0.2 s after its reset; the tree last "
            r"showed \['click-canvas', 'query', 'subbtn', 'sync-task-cover'\]",
        ),
    ],
)
def test_the_launcher_fails_initialize_on_a_page_that_never_gets_there(
    there: bool, screen: set[str], message: str
) -> None:
    page = _Page(screens=[screen])

    started = time.monotonic()
    with pytest.raises(RuntimeError, match=message):
        page.wait(there=there, wait_s=0.2)

    assert 0.2 <= time.monotonic() - started < 1.0
    assert page.notes == []


@dataclass
class _MiniWoBApp:
    """The MiniWoB++ app and AndroidWorld's UI tree, faked for the launcher's steps that start an episode."""

    #: What each look sees on the screen, in turn; the last one repeats.
    screens: list[set[str]]
    env: object = field(default_factory=object)
    #: Each step, in order: an intent's extras, a sleep, a look or the read.
    steps: list[object] = field(default_factory=list)

    def start(self, env: object, extras: list[str]) -> None:
        assert env is self.env
        self.steps.append(extras)

    def sleep(self, seconds: float) -> None:
        self.steps.append(("sleep", seconds))

    def shown(self, env: object) -> set[str]:
        assert env is self.env
        self.steps.append("look")
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def read(self, env: object) -> str:
        assert env is self.env
        self.steps.append("read")
        return "Select hPh22 and click Submit."


def test_the_launcher_starts_an_episode_in_androidworlds_order_with_its_two_waits() -> None:
    """The order is the fix: the reset waits for the loaded page, the read for the started episode."""
    app = _MiniWoBApp(screens=[_LOADING, _COVERED, _COVERED, _RUNNING])

    instruction = _launcher().start_the_episode(
        "click-option",
        app.env,
        start=app.start,
        shown=app.shown,
        read=app.read,
        warn=lambda note: None,
        sleep=app.sleep,
        poll_s=0.01,
    )

    assert instruction == "Select hPh22 and click Submit."
    assert app.steps == [
        ["--es", "RL_TASK_APP_CONFIG", """'{"task":"click-option"}'"""],
        ("sleep", 1),
        "look",  # still loading
        "look",  # the cover is there: the page has loaded
        ["--ez", "reset", "true"],
        ["--ez", "step", "true"],
        "look",  # the cover is still there
        "look",  # the cover is gone: the episode has started
        "read",
    ]


async def test_a_service_that_builds_another_task_is_refused_and_stopped(
    root: Path, tmp_path: Path
) -> None:
    write_behaviour(root, {"/task/goal": {"json": {"goal": "Turn brightness to the min value."}}})
    setup = _setup(root)

    with pytest.raises(
        AndroidWorldError,
        match="the task list records 'Turn brightness to the max value.': either its parameters "
        "cannot be rebuilt from its address, or the checkout or its dependencies differ",
    ):
        await setup.start(_device(tmp_path), _task())
    await setup.stop()  # what the capability's teardown does after a failed setup

    assert "/task/initialize" not in [path for _, path, _ in _calls(root)]
    assert await _service_is_gone(root)


# ── the run's answer: information-retrieval tasks are scored on it ───


async def _scored(
    root: Path, tmp_path: Path, task: dict[str, JsonValue], answer: str | None
) -> Judgment:
    """Prepare *task*, score it handed *answer*, end it."""
    write_behaviour(root, {"/task/goal": {"json": {"goal": task["goal"]}}})
    setup = _setup(root)
    await setup.start(_device(tmp_path), task)
    try:
        return await _judged(setup, answer)
    finally:
        await setup.stop()


async def test_an_information_retrieval_task_hands_the_answer_over_before_the_score(
    root: Path, tmp_path: Path
) -> None:
    """As AndroidWorld's own runner: the answer action first, then the score."""
    reading = await _scored(root, tmp_path, _asking_task(), "Mall")

    address = {"task_type": _ASKS, "task_idx": "0"}
    assert _after_the_reset(root) == [
        ("POST", "/execute_action", {"action_type": "answer", "text": "Mall"}),
        ("GET", "/task/score", None),
        ("POST", "/task/tear_down", None),
    ]
    assert [query for _, path, query in _calls(root) if path == "/task/score"] == [address]
    assert reading.evidence["answer"] == "sent"


async def test_an_information_retrieval_task_without_an_answer_is_scored_on_none(
    root: Path, tmp_path: Path
) -> None:
    """Nothing is sent: AndroidWorld scores the empty answer 0, as when its agent never answers."""
    reading = await _scored(root, tmp_path, _asking_task(), None)

    assert [path for _, path, _ in _after_the_reset(root)] == ["/task/score", "/task/tear_down"]
    assert reading.evidence["answer"] == "none"


async def test_a_task_scored_on_the_device_never_sends_the_answer(
    root: Path, tmp_path: Path
) -> None:
    reading = await _scored(root, tmp_path, _task(), "Mall")

    assert [path for _, path, _ in _after_the_reset(root)] == ["/task/score", "/task/tear_down"]
    assert "answer" not in reading.evidence


async def test_an_answer_the_service_refuses_is_an_ordinary_error_and_nothing_is_scored(
    root: Path, tmp_path: Path
) -> None:
    """A score read without the answer would not be this run's: "not scored", not a 0."""
    setup = _setup(root)
    write_behaviour(root, {"/task/goal": {"json": {"goal": _ASKS_GOAL}}})
    await setup.start(_device(tmp_path), _asking_task())
    try:
        write_behaviour(root, {"/execute_action": {"status": 500}})
        with pytest.raises(AndroidWorldError, match="POST /execute_action answered HTTP 500"):
            await _judged(setup, "Mall")
    finally:
        await setup.stop()

    assert "/task/score" not in [path for _, path, _ in _calls(root)]
    assert await _service_is_gone(root)


# ── the service's own failures ───────────────────────────────────────


async def test_a_service_that_exits_while_starting_is_reported_with_its_log(
    root: Path, tmp_path: Path
) -> None:
    write_behaviour(root, {"exit_code": 3})
    setup = _setup(root)

    with pytest.raises(AndroidWorldError, match=r"exited with code 3[\s\S]*giving up"):
        await setup.start(_device(tmp_path), _task())

    assert await _service_is_gone(root)


async def test_a_service_that_is_not_ready_in_time_is_stopped(root: Path, tmp_path: Path) -> None:
    write_behaviour(root, {"start_delay_s": 60})
    setup = AndroidWorldSetup(root=str(root), start_timeout_s=1.0)

    with pytest.raises(TimeoutError, match="was not ready after 1 s"):
        await setup.start(_device(tmp_path), _task())

    assert await _service_is_gone(root)


async def test_a_call_that_preparing_the_task_needs_is_bounded(root: Path, tmp_path: Path) -> None:
    write_behaviour(root, {"/task/initialize": {"hang": True}})
    setup = AndroidWorldSetup(root=str(root), start_timeout_s=20.0, call_timeout_s=0.5)

    with pytest.raises(TimeoutError, match="POST /task/initialize: .* no answer within 0.5 s"):
        await setup.start(_device(tmp_path), _task())
    await setup.stop()

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
    root: Path, tmp_path: Path, rule: dict[str, JsonValue], message: str
) -> None:
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    try:
        write_behaviour(root, {"/task/score": rule})
        with pytest.raises(AndroidWorldError, match=message):
            await _judged(setup, None)
    finally:
        await setup.stop()
    assert await _service_is_gone(root)


@pytest.mark.parametrize(("answered", "score"), [(False, 0.0), (True, 1.0)])
async def test_a_bool_score_is_0_or_1_as_androidworlds_runner_counts_it(
    root: Path, tmp_path: Path, answered: bool, score: float
) -> None:
    """RetroPlaylistDuration answers False for a wrongly named playlist: a failure, not "not scored"."""
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    try:
        write_behaviour(root, {"/task/score": {"json": {"score": answered}}})
        reading = await _judged(setup, None)
    finally:
        await setup.stop()

    assert reading.score == score


async def test_a_dead_service_is_an_ordinary_error_that_says_so(root: Path, tmp_path: Path) -> None:
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    try:
        assert setup._service is not None
        setup._service.process.kill()  # the setup's own child
        await setup._service.process.wait()
        with pytest.raises(AndroidWorldError, match="could not be reached .*exited with code"):
            await _judged(setup, None)
    finally:
        await setup.stop()
    assert await _service_is_gone(root)


async def test_a_server_error_ends_with_the_traceback_the_service_logs_after_answering(
    root: Path, tmp_path: Path
) -> None:
    """Starlette answers before uvicorn logs why: the message waits for the cause."""
    cause = "ValueError: Could not extract utterance; something went wrong."
    traceback = (
        "ERROR:    Exception in ASGI application\n"
        "Traceback (most recent call last):\n"
        '  File "miniwob_base.py", line 47, in _get_episode_utterance\n'
        f"{cause}\n"
    )
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    try:
        write_behaviour(
            root, {"/task/score": {"status": 500, "log": traceback, "log_after_s": 0.5}}
        )
        with pytest.raises(AndroidWorldError, match="GET /task/score answered HTTP 500") as caught:
            await _judged(setup, None)
    finally:
        await setup.stop()

    assert str(caught.value).endswith(cause)


async def test_a_server_error_ends_with_the_handlers_traceback_not_one_logged_on_the_way(
    root: Path, tmp_path: Path
) -> None:
    """While it handles a request, AndroidWorld logs tracebacks of its own: absl's, of an adb command it retries."""
    retried = (
        "ERROR:absl:Failed to execute ADB command (try 1 of 3): [adb shell am start  -W -n "
        "net.osmand/net.osmand.plus.activities.MapActivity]\n"
        "Traceback (most recent call last):\n"
        '  File "adb_controller.py", line 122, in execute_command\n'
        "subprocess.TimeoutExpired: Command 'am start' timed out after 5.0 seconds\n"
        "WARNING:absl:serve.py: not restarting the adb server the run shares; retrying\n"
    )
    cause = "sqlite3.OperationalError: no such table: map_markers"
    traceback = (
        "ERROR:    Exception in ASGI application\n"
        "Traceback (most recent call last):\n"
        '  File "sqlite_utils.py", line 164, in delete_all_rows_from_table\n'
        f"{cause}\n"
    )
    write_behaviour(
        root,
        {
            "/task/initialize": {
                "status": 500,
                "log_before": retried,
                "log": traceback,
                "log_after_s": 0.5,
            }
        },
    )
    setup = _setup(root)

    with pytest.raises(
        AndroidWorldError, match="POST /task/initialize answered HTTP 500"
    ) as caught:
        await setup.start(_device(tmp_path), _task())
    await setup.stop()

    assert str(caught.value).endswith(cause)


async def test_a_server_error_whose_cause_is_never_logged_waits_only_its_limit(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(androidworld, "_TRACEBACK_WAIT_S", 0.3)
    clock = asyncio.get_running_loop().time
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    try:
        write_behaviour(root, {"/task/score": {"status": 500, "log": ""}})
        started = clock()
        with pytest.raises(AndroidWorldError, match="GET /task/score answered HTTP 500"):
            await _judged(setup, None)
        waited = clock() - started
    finally:
        await setup.stop()

    assert 0.3 <= waited < 2.0


async def test_a_failed_tear_down_is_logged_and_the_service_is_still_stopped(
    root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_behaviour(root, {"/task/tear_down": {"status": 500}})
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())

    await setup.stop()

    assert "tear-down of SystemBrightnessMax #0 failed" in caplog.text
    assert await _service_is_gone(root)


async def test_a_service_that_ignores_sigterm_is_killed_with_its_group(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(androidworld, "STOP_GRACE_S", 0.5)
    write_behaviour(root, {"ignore_sigterm": True})
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())

    await setup.stop()

    assert await _service_is_gone(root)


async def test_stop_is_safe_before_a_start_and_twice(root: Path, tmp_path: Path) -> None:
    setup = _setup(root)
    await setup.stop()
    await setup.start(_device(tmp_path), _task())
    await setup.stop()
    await setup.stop()

    assert [path for _, path, _ in _calls(root)].count("/task/tear_down") == 1


# ── one device for several tasks (§4.10.1 "环境跨任务复用") ─────────


def test_it_declares_that_it_resets_the_device_between_tasks() -> None:
    assert AndroidWorldSetup().resets_between_tasks


async def test_a_kept_service_serves_the_next_task_after_androidworlds_reset(
    root: Path, tmp_path: Path
) -> None:
    """One service for two tasks, as AndroidWorld's own runner keeps one environment: the next
    task's component takes it instead of starting one, which would take every app's snapshot
    again from the first task's leftovers."""
    goals: list[JsonValue] = [{"goal": "Turn brightness to the max value."}, {"goal": _ASKS_GOAL}]
    write_behaviour(root, {"/task/goal": {"json_in_turn": goals}})
    first = _setup(root)
    await first.start(_device(tmp_path), _task())
    kept = await first.stop(keep=True)
    started = service(root)
    assert kept is not None and not gone(started["pid"]), "still running for the next task"

    second = _setup(root)
    await second.start(_device(tmp_path), _asking_task(), kept=kept)
    await second.stop()

    assert service(root) == started, "no second service was started"
    brightness = {"task_type": "SystemBrightnessMax", "task_idx": "0"}
    asks = {"task_type": _ASKS, "task_idx": "0"}
    assert [call for call in _calls(root) if call[1] != "/health"] == [
        ("GET", "/suite/reinitialize", {"task_family": "android", "n_task_combinations": "1", "seed": "42"}),
        ("GET", "/task/goal", brightness),
        ("POST", "/task/initialize", brightness),
        ("GET", "/task/start_on_home_screen", brightness),
        ("POST", "/reset", {"go_home": "true"}),
        ("POST", "/task/tear_down", brightness),
        ("GET", "/suite/reinitialize", {"task_family": "information_retrieval", "n_task_combinations": "1", "seed": "42"}),
        ("GET", "/task/goal", asks),
        ("POST", "/task/initialize", asks),
        ("GET", "/task/start_on_home_screen", asks),
        ("POST", "/reset", {"go_home": "true"}),
        ("POST", "/task/tear_down", asks),
    ]  # fmt: skip
    assert await _service_is_gone(root)


async def test_a_failed_reset_stops_the_service_and_is_raised(root: Path, tmp_path: Path) -> None:
    write_behaviour(root, {"/task/tear_down": {"status": 500}})
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())

    with pytest.raises(AndroidWorldError, match="/task/tear_down answered HTTP 500"):
        await setup.stop(keep=True)

    assert await _service_is_gone(root)
    assert await setup.stop() is None, "nothing left to stop"


async def test_a_service_with_no_task_initialized_is_never_kept(root: Path, tmp_path: Path) -> None:
    write_behaviour(root, {"/task/initialize": {"status": 500}})
    setup = _setup(root)
    with pytest.raises(AndroidWorldError, match="/task/initialize answered HTTP 500"):
        await setup.start(_device(tmp_path), _task())

    with pytest.raises(AndroidWorldError, match="no AndroidWorld task was initialized"):
        await setup.stop(keep=True)

    assert await _service_is_gone(root)


async def test_a_reset_cut_short_by_cancellation_stops_the_service_it_kept(
    root: Path, tmp_path: Path
) -> None:
    """The tear-down still answers after the cancellation; the service it kept is nobody's then."""
    write_behaviour(root, {"/task/tear_down": {"delay_s": 0.5}})
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    stopping = asyncio.create_task(setup.stop(keep=True))
    for _ in range(500):  # until the tear-down reached the service
        if any(path == "/task/tear_down" for _, path, _ in _calls(root)):
            break
        await asyncio.sleep(0.01)

    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert await _service_is_gone(root)


async def test_a_kept_service_is_the_components_even_when_its_task_is_refused(
    root: Path, tmp_path: Path
) -> None:
    first = _setup(root)
    await first.start(_device(tmp_path), _task())
    kept = await first.stop(keep=True)

    second = _setup(root)
    with pytest.raises(AndroidWorldSetupError, match="is not an AndroidWorld task"):
        await second.start(_device(tmp_path), {"not": "a task"}, kept=kept)
    await second.stop()

    assert await _service_is_gone(root)


async def test_a_score_before_any_task_is_an_ordinary_error(root: Path) -> None:
    with pytest.raises(AndroidWorldError, match="no AndroidWorld task was initialized"):
        await _judged(_setup(root), None)


async def test_a_device_without_the_emulators_grpc_is_refused(root: Path, tmp_path: Path) -> None:
    device = _device(tmp_path).model_copy(update={"emulator": None})

    with pytest.raises(AndroidWorldError, match="drives an emulator over its gRPC endpoint"):
        await _setup(root).start(device, _task())
    assert requests(root) == [], "no service was started"


# ── cancellation ─────────────────────────────────────────────────────


async def test_a_start_cancelled_while_waiting_for_the_service_stops_it_before_ending(
    root: Path, tmp_path: Path
) -> None:
    write_behaviour(root, {"start_delay_s": 60})
    starting = asyncio.ensure_future(_setup(root).start(_device(tmp_path), _task()))
    assert await soon(lambda: (root / "record" / "service.json").is_file(), within_s=20)

    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert await _service_is_gone(root)


async def test_a_cancelled_score_stays_a_cancellation(root: Path, tmp_path: Path) -> None:
    setup = _setup(root)
    await setup.start(_device(tmp_path), _task())
    try:
        write_behaviour(root, {"/task/score": {"hang": True}})
        scoring = asyncio.ensure_future(_judged(setup, None))
        assert await soon(lambda: "/task/score" in [p for _, p, _ in _calls(root)], within_s=20)

        scoring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scoring
    finally:
        await setup.stop()
    assert await _service_is_gone(root)


# ── at the scoring point, through the environment ────────────────────


async def _environment_verdicts(
    setup: AndroidWorldSetup, device: AndroidProviderSession, timeout_s: float
) -> list[Verdict]:
    """The scoring point's verdicts, AndroidWorld's verifier bound beside *setup*."""
    capability = unset_capability()
    env = make_env(AndroidEnvironment, {"android": capability})
    env.bind_setups([setup])
    env.bind_live_verifiers([AndroidWorldVerifier()])
    capability._session = device  # the device its setup brought up, which *setup* started on
    return await env.verify_live(timeout_s=timeout_s, answer=None)


async def test_the_environment_records_the_verdict_and_each_way_of_judging_nothing(
    root: Path, tmp_path: Path
) -> None:
    setup, device = _setup(root), _device(tmp_path)
    await setup.start(device, _task())
    try:
        (judged,) = await _environment_verdicts(setup, device, timeout_s=5.0)
        write_behaviour(root, {"/task/score": {"status": 500}})
        (failed,) = await _environment_verdicts(setup, device, timeout_s=5.0)
        write_behaviour(root, {"/task/score": {"hang": True}})
        (late,) = await _environment_verdicts(setup, device, timeout_s=0.5)
    finally:
        await setup.stop()

    who = VerifierIdentity(verifier_id="androidworld", version=PINNED_COMMIT)
    assert (judged.verifier, judged.attachment, judged.status, judged.score) == (
        who,
        "live",
        "judged",
        1.0,
    )
    assert (failed.verifier, failed.status, failed.reason) == (who, "not_judged", "error")
    assert failed.detail is not None and failed.detail.startswith("AndroidWorldError: GET /task")
    assert (late.verifier, late.status, late.reason) == (who, "not_judged", "timeout")
    assert await _service_is_gone(root)


async def test_with_no_verifier_the_environment_records_nothing(root: Path) -> None:
    env = make_env(AndroidEnvironment, {"android": unset_capability()})
    env.bind_setups([_setup(root)])
    assert await env.verify_live(timeout_s=1.0, answer=None) == []


async def test_the_verifier_judges_only_through_androidworlds_setup(tmp_path: Path) -> None:
    """It reads the task and its state from the service that setup started, and nothing else."""
    with pytest.raises(AndroidWorldError, match="the android capability hosts none"):
        await AndroidWorldVerifier().judge(_device(tmp_path), None, answer=None)


def test_the_verifier_judges_live_on_the_answer_as_androidworld_at_its_commit() -> None:
    verifier = AndroidWorldVerifier()
    assert attachment_of(verifier) == "live" and "answer" in verifier.needs
    assert verifier.capability_id == "android"
    assert verifier.identity() == VerifierIdentity(
        verifier_id="androidworld", version=PINNED_COMMIT
    )
    # The slug every run's verdict file is named by, before and after the verifier landed.
    assert verifier.identity().slug == "androidworld_44136fa3"


def test_the_verifier_needs_of_the_device_only_what_the_setup_boots_it_with(root: Path) -> None:
    """Requirements, checked in ``prepare()``: the androidworld setup's device meets them."""
    verifier, setup = AndroidWorldVerifier(), _setup(root)
    assert verifier.device_needs == DeviceNeeds(emulator_grpc=True, root=True)
    assert set(verifier.device_needs.named()) <= set(setup.device_needs.named())
    env = make_env(AndroidEnvironment, {"android": unset_capability()})
    with pytest.raises(UnmetDeviceNeeds, match="'androidworld' needs emulator_grpc, root of the"):
        env.check_setup({}, live_verifiers=[verifier])  # no setup: nothing beyond ADB


# ── before anything starts: check_setup ──────────────────────────────


def test_what_the_androidworld_setup_needs_of_the_device() -> None:
    assert AndroidWorldSetup(root="/aw").device_needs == DeviceNeeds(
        emulator_grpc=True, root=True, guest_wifi=True, fresh=True
    )


def test_a_good_installation_and_task_pass(root: Path) -> None:
    _setup(root).check_setup(_task())


def test_an_information_retrieval_task_passes(root: Path) -> None:
    """Scored on the run's answer, which the scoring point hands over (§4.2)."""
    _setup(root).check_setup(_asking_task())


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
        _setup(root).check_setup(_task())


async def test_a_path_without_ffmpeg_is_refused_before_the_service_starts(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    setup = _setup(root)

    with pytest.raises(AndroidWorldSetupError) as refused:
        setup.check_setup(_task())
    with pytest.raises(AndroidWorldSetupError, match="there is no ffmpeg on PATH"):
        await setup.start(_device(tmp_path), _task())

    assert (
        "there is no ffmpeg on PATH: AndroidWorld generates its music tasks' mp3 files with "
        "pydub, which runs ffmpeg"
    ) in str(refused.value)
    assert not (root / "record" / "service.json").exists(), "no service was started"


@pytest.mark.parametrize(
    ("task", "message"),
    [
        ({"task": "SystemBrightnessMax"}, "the task's oracle_task is not an AndroidWorld task"),
        (_task(commit="f" * 40), "the task list was made with AndroidWorld ffff"),
        (_task(params={"seed": 1}), "params.seed is 1, and the instance AndroidWorld makes"),
        (
            _task(task="MarkorCreateFolder"),
            "MarkorCreateFolder cannot run: it names its folder after the host's clock",
        ),
    ],
)
def test_a_task_the_setup_cannot_start_is_refused(
    root: Path, task: dict[str, JsonValue] | None, message: str
) -> None:
    with pytest.raises(AndroidWorldSetupError, match=message):
        _setup(root).check_setup(task)


@pytest.mark.parametrize("task", [{"task": "SystemBrightnessMax"}, _task(commit="f" * 40)])
def test_a_task_from_no_list_of_the_pinned_commit_names_the_fetch(
    root: Path, task: dict[str, JsonValue]
) -> None:
    """In one line, with the command that writes the lists again, not pydantic's report."""
    with pytest.raises(AndroidWorldSetupError) as refused:
        _setup(root).check_setup(task)
    message = str(refused.value)
    assert message.endswith(
        "fetch the task lists again with spectragent benchmark fetch androidworld"
    )
    assert "\n" not in message


def test_every_problem_is_named_at_once(root: Path) -> None:
    (root / "cache" / "a11y" / FORWARDER_APK).unlink()

    with pytest.raises(AndroidWorldSetupError) as refused:
        _setup(root).check_setup({"task": "SystemBrightnessMax"})
    assert "forwarder" in str(refused.value) and "not an AndroidWorld task" in str(refused.value)


async def test_without_a_task_only_the_installation_is_checked_and_start_refuses(
    root: Path, tmp_path: Path
) -> None:
    """``config check --benchmark androidworld`` prepares with no task: the task is setup's to refuse."""
    setup = _setup(root)

    setup.check_setup(None)
    assert not setup.makes_goal(None)
    with pytest.raises(AndroidWorldSetupError, match="the task carries no AndroidWorld task"):
        await setup.start(_device(tmp_path), None)

    (root / "cache" / "a11y" / FORWARDER_APK).unlink()
    with pytest.raises(AndroidWorldSetupError, match="forwarder"):
        setup.check_setup(None)


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
