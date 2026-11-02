"""The ``androidworld`` task oracle: AndroidWorld's own service prepares, scores and ends each task.

``docs/architecture_design.md`` §11.2 ("任务裁判与 ``AndroidSetup``"). AndroidWorld's
FastAPI service runs out of process, in AndroidWorld's own venv at a pinned
commit (its pinned dependencies would clash with Spectragent's lock file),
against the run's emulator, while observations and actions go through
Spectragent's own bridge. One task is one run:

1. **start**, once the provider booted the device meeting :attr:`device_needs`
   (the emulator's gRPC behind a token, adbd as root, the guest's Wi-Fi up,
   and a fresh device whose changes are thrown away with the run):
   the launcher (``androidworld_service/serve.py``) runs the service under
   AndroidWorld's interpreter, in a process group of its own tied to this
   process (``processes.py``), on a free loopback port, with a whitelisted
   environment, and the wait for ``/health`` is bounded. Then the task: the
   suite its seed makes, a check that the service built the task the list
   records, ``/task/initialize`` and ``/reset``.
2. **read_score**, at the Runtime's scoring point: ``/task/score``, within
   the run's ``oracle_timeout``. HTTP 500, a broken answer or a dead service
   raise :class:`AndroidWorldError`, so the environment records "not scored".
3. **stop**, at teardown: ``/task/tear_down`` (best effort, since the device's
   changes are thrown away with the run; a failure is logged), then the
   service's process group: SIGTERM, then SIGKILL for the whole group, its
   ``adb logcat`` child included.

The service handles one request at a time and cannot cancel one in progress:
a score call cut off by the run's limit goes on, and the tear-down waits
behind it until its own limit. Every service is the run's own, so nothing
waits on it afterwards.

The service makes the task again from its address, so a task whose parameters
it cannot rebuild cannot be the listed one: ``MarkorCreateFolder`` names its
folder after the host's clock when it is made (the only one of the pinned
commit's 116 tasks that reads the clock). Such tasks are named in
``UNREPRODUCIBLE``: the loader leaves them out and :meth:`check_setup` refuses
them, so they never reach a booted device.

Timings measured on the spike (Apple M1 Pro, load 6-14, 2026-11-01): start
17-32 s, initialize 0.6-8 s, reset 0.1-1.2 s (22-27 s when AndroidWorld
reinstalled its accessibility forwarder), score median 0.1 s, p90 6.1 s, max
18.2 s, tear-down 0.3-2.2 s.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import httpx
from pydantic import Field, JsonValue, ValidationError

from spectragent.core.capabilities.android.adb_server import HOST, free_port
from spectragent.core.capabilities.android.backends.base import (
    AndroidProviderSession,
    DeviceNeeds,
    EmulatorAccess,
    EmulatorGrpc,
)
from spectragent.core.capabilities.android.oracle._registrar import (
    AndroidTaskOracleBase,
    OracleTask,
)
from spectragent.core.capabilities.android.oracle.androidworld_tasks import (
    INFORMATION_RETRIEVAL,
    UNREPRODUCIBLE,
    AndroidWorldTask,
)
from spectragent.core.capabilities.android.processes import ProcessGroup, finish, log_tail
from spectragent.schemas.oracle_score import OracleReading

logger = logging.getLogger(__name__)

#: The AndroidWorld commit the launcher's patches are written against.
PINNED_COMMIT = "55ddaf5edc6f6c25673aed343907d5b9dca66f84"
#: The accessibility forwarder build the pinned android_env installs, cached by
#: the one-time setup; without it the service downloads it on every start.
FORWARDER_APK = "2024.05.13-accessibility_forwarder.apk"
#: The launcher, run by AndroidWorld's interpreter; Spectragent never imports it.
LAUNCHER = Path(__file__).resolve().parent / "androidworld_service" / "serve.py"
#: How long the service gets to exit after SIGTERM before its group is killed.
STOP_GRACE_S = 5.0
_HEALTH_POLL_S = 0.5
_HEALTH_CALL_S = 2.0
_LOOPBACK = "localhost,127.0.0.1,::1"


class AndroidWorldError(RuntimeError):
    """AndroidWorld's service failed, answered an error, or answered something it never does."""


class AndroidWorldSetupError(ValueError):
    """The installation or the task is one the oracle would refuse to start; every problem named."""


@dataclass(frozen=True)
class Installation:
    """An AndroidWorld installation, laid out as the oracle's ``root`` describes."""

    root: Path

    @property
    def python(self) -> Path:
        return self.root / ".venv" / "bin" / "python"

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def forwarder_apk(self) -> Path:
        return self.root / "cache" / "a11y" / FORWARDER_APK

    def problems(self) -> list[str]:
        """What keeps the service from starting here, one entry per problem."""
        if not self.root.is_dir():
            return [f"there is no AndroidWorld installation at {self.root}"]
        found: list[str] = []
        if not (self.python.is_file() and self.python.stat().st_mode & 0o111):
            found.append(f"there is no AndroidWorld venv interpreter at {self.python}")
        if not (self.src / "server" / "android_server.py").is_file():
            found.append(f"there is no AndroidWorld checkout at {self.src}")
        else:
            commit = checkout_commit(self.src)
            if commit != PINNED_COMMIT:
                at = f"is at {commit}" if commit else "has a commit that cannot be read"
                found.append(
                    f"the checkout {self.src} {at}, and the launcher is written against "
                    f"{PINNED_COMMIT}: check that commit out there"
                )
        if not self.forwarder_apk.is_file():
            found.append(
                f"there is no cached accessibility forwarder at {self.forwarder_apk}: copy the "
                f"one AndroidWorld's one-time setup downloaded there"
            )
        return found


def checkout_commit(src: Path) -> str | None:
    """The commit the git checkout at *src* is at, read from its ``.git``; ``None`` when unreadable.

    Follows a ``.git`` file (``gitdir:``) and a worktree's ``commondir``, a
    symbolic ``HEAD`` to a loose ref or to ``packed-refs``. Needs no ``git``.
    """
    git = src / ".git"
    try:
        if git.is_file():
            pointer = git.read_text().strip()
            if not pointer.startswith("gitdir:"):
                return None
            git = (src / pointer.removeprefix("gitdir:").strip()).resolve()
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text().strip()).resolve()
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.removeprefix("ref:").strip()
        for directory in (git, common):
            if (directory / ref).is_file():
                return (directory / ref).read_text().strip() or None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                commit, _, name = line.partition(" ")
                if name == ref:
                    return commit
    except OSError:
        return None
    return None


def params_digest(params: dict[str, JsonValue]) -> str:
    """The sha256 of the task's parameters as canonical JSON."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class _Service:
    """One running AndroidWorld service: its process group, its port and its working directory."""

    group: ProcessGroup
    process: asyncio.subprocess.Process
    port: int
    workdir: Path
    client: httpx.AsyncClient

    @property
    def log(self) -> Path:
        return self.workdir / "service.log"

    @classmethod
    async def start(
        cls,
        installation: Installation,
        device: AndroidProviderSession,
        emulator: EmulatorAccess,
        grpc: EmulatorGrpc,
        timeout_s: float,
    ) -> _Service:
        """Start the service for *device* and wait, at most *timeout_s*, until it is healthy.

        The service is a second client of the run's adb server, *device*'s
        endpoint, through the same adb build and with the emulator's ``HOME``.

        Raises:
            AndroidWorldError: It exited first; the message ends with its log.
            TimeoutError: It was not healthy in time.
        """
        port = free_port()
        group = await ProcessGroup.open()
        workdir: Path | None = None
        try:
            workdir = Path(tempfile.mkdtemp(prefix="spectragent-androidworld-"))
            (workdir / "tmp").mkdir()
            process = await group.spawn(
                [
                    str(installation.python),
                    "-I",
                    "-u",
                    str(LAUNCHER),
                    "--port",
                    str(port),
                    "--src",
                    str(installation.src),
                    "--adb",
                    emulator.adb,
                    "--adb-server-port",
                    str(device.endpoint.port),
                    "--console-port",
                    str(emulator.console_port),
                    "--grpc-target",
                    f"{grpc.host}:{grpc.port}",
                    "--grpc-token-file",
                    grpc.token_file,
                    "--forwarder-apk",
                    str(installation.forwarder_apk),
                ],
                env={
                    # Nothing else: android_env logs its whole environment at INFO.
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "LANG": "en_US.UTF-8",
                    "HOME": emulator.home,
                    "TMPDIR": str(workdir / "tmp"),
                    "GRPC_ENABLE_FORK_SUPPORT": "0",
                    "NO_PROXY": _LOOPBACK,
                    "no_proxy": _LOOPBACK,
                },
                log=workdir / "service.log",
            )
        except BaseException:
            await finish(asyncio.ensure_future(group.close(None, grace_s=0)))
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
            raise
        service = cls(
            group=group,
            process=process,
            port=port,
            workdir=workdir,
            client=httpx.AsyncClient(
                base_url=f"http://{HOST}:{port}", trust_env=False, timeout=None
            ),
        )
        try:
            await service._until_healthy(timeout_s)
        except BaseException as failure:
            try:
                await finish(asyncio.ensure_future(service.close()))
            except Exception as exc:
                failure.add_note(f"stopping the AndroidWorld service also failed: {exc}")
            raise
        return service

    async def _until_healthy(self, timeout_s: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last = "it had not answered"
        while True:
            if self.process.returncode is not None:
                raise AndroidWorldError(
                    f"AndroidWorld's service exited with code {self.process.returncode} before "
                    f"it was ready:\n{log_tail(self.log)}"
                )
            try:
                async with asyncio.timeout(_HEALTH_CALL_S):
                    answer = await self.client.get("/health")
                if answer.status_code == 200:
                    return
                last = f"/health answered HTTP {answer.status_code}"
            except (httpx.TransportError, TimeoutError) as exc:
                last = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"AndroidWorld's service was not ready after {timeout_s:g} s ({last}):\n"
                    f"{log_tail(self.log)}"
                )
            await asyncio.sleep(_HEALTH_POLL_S)

    async def call(
        self,
        method: str,
        path: str,
        params: dict[str, str | int | bool],
        *,
        timeout_s: float | None,
    ) -> dict[str, JsonValue]:
        """One call; its JSON object answer.

        *timeout_s* ``None`` leaves the limit to the caller (the scoring point's).

        Raises:
            AndroidWorldError: The service answered an error status or no JSON
                object, or could not be reached; the message ends with its log.
            TimeoutError: No answer within *timeout_s*.
        """
        query = {
            name: str(value).lower() if isinstance(value, bool) else value
            for name, value in params.items()
        }
        what = f"{method} {path}"
        limit = asyncio.timeout(timeout_s)
        try:
            async with limit:
                answer = await self.client.request(method, path, params=query)
        except TimeoutError:
            if not limit.expired():
                raise
            raise TimeoutError(
                f"{what}: AndroidWorld's service gave no answer within {timeout_s:g} s"
            ) from None
        except httpx.TransportError as exc:
            state = (
                f"it exited with code {self.process.returncode}"
                if self.process.returncode is not None
                else "it is still running"
            )
            raise AndroidWorldError(
                f"{what}: AndroidWorld's service could not be reached ({type(exc).__name__}: "
                f"{exc}; {state}):\n{log_tail(self.log, lines=8)}"
            ) from None
        if answer.status_code != 200:
            raise AndroidWorldError(
                f"{what} answered HTTP {answer.status_code} ({answer.text[:200]!r}); the end of "
                f"the service's log:\n{log_tail(self.log, lines=8)}"
            )
        try:
            body = answer.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise AndroidWorldError(
                f"{what} answered something that is not a JSON object: {answer.text[:200]!r}"
            )
        return body

    async def close(self) -> None:
        """Stop the service (SIGTERM, then SIGKILL for its group), close the client, remove its directory."""
        try:
            await self.group.close(self.process, grace_s=STOP_GRACE_S)
        finally:
            await self.client.aclose()
            shutil.rmtree(self.workdir, ignore_errors=True)


@dataclass
class AndroidWorldOracle(AndroidTaskOracleBase):
    """AndroidWorld's tasks, prepared, scored and ended by AndroidWorld's own service."""

    __registry_key__ = "androidworld"

    root: Annotated[
        str,
        Field(
            description=(
                "The AndroidWorld installation ('~' expanded): its venv (.venv/bin/python, "
                "Python 3.11), its checkout at the commit the launcher is written against "
                "(src/), and the accessibility forwarder its one-time setup cached "
                f"(cache/a11y/{FORWARDER_APK}). The AVD the provider boots must be the one that "
                "setup prepared."
            )
        ),
    ]
    start_timeout_s: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Seconds AndroidWorld's service may take to start against the booted device "
                "and answer /health; it took 17-32 s, re-taking its app snapshots included."
            ),
        ),
    ] = 120.0
    call_timeout_s: Annotated[
        float,
        Field(
            gt=0,
            description=(
                "Seconds each call that prepares or ends the task may take (the suite, the "
                "goal, initialize, reset, tear-down); initialize took up to 8 s and reset up "
                "to 27 s when AndroidWorld reinstalled its accessibility forwarder. The score "
                "is limited by the run's oracle_timeout instead."
            ),
        ),
    ] = 60.0

    _service: _Service | None = field(default=None, init=False, repr=False)
    _stopping: asyncio.Future[None] | None = field(default=None, init=False, repr=False)
    _task: AndroidWorldTask | None = field(default=None, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)
    #: The goal the device made at initialize, for a task whose goal is made then.
    _device_goal: str | None = field(default=None, init=False, repr=False)

    @property
    def installation(self) -> Installation:
        return Installation(Path(self.root).expanduser())

    @property
    def device_needs(self) -> DeviceNeeds:
        # The service drives the emulator over its gRPC, restarts adbd as root
        # when it attaches (which would drop the device under the bridge), and
        # its accessibility forwarder reaches the host over the guest's Wi-Fi.
        # It re-takes each app's snapshot from the device it starts on, and
        # tasks restore their apps from those: a run's changes kept in the AVD
        # would become every later run's start.
        return DeviceNeeds(emulator_grpc=True, root=True, guest_wifi=True, fresh=True)

    def check_setup(self, task: OracleTask | None) -> None:
        problems = self.installation.problems()
        try:
            self._parse(task)
        except AndroidWorldSetupError as exc:
            problems.append(str(exc))
        if problems:
            raise AndroidWorldSetupError(
                "the androidworld oracle cannot start: " + "; ".join(problems)
            )

    async def start(self, device: AndroidProviderSession, task: OracleTask | None) -> None:
        parsed = self._parse(task)
        emulator = device.emulator
        if emulator is None or emulator.grpc is None:
            raise AndroidWorldError(
                f"AndroidWorld drives an emulator over its gRPC endpoint, and the device "
                f"{device.provider_id!r} brought up offers none"
            )
        self._service = await _Service.start(
            self.installation, device, emulator, emulator.grpc, self.start_timeout_s
        )
        self._task, self._device_goal = parsed, None
        await self._prepare(self._service, parsed)

    async def _prepare(self, service: _Service, task: AndroidWorldTask) -> None:
        """The task on the device: its suite, the check that it is the listed task, initialize, reset."""
        address: dict[str, str | int | bool] = {"task_type": task.task, "task_idx": task.index}
        await service.call(
            "GET",
            "/suite/reinitialize",
            {
                "task_family": task.family,
                "n_task_combinations": task.index + 1,
                "seed": task.suite_seed,
            },
            timeout_s=self.call_timeout_s,
        )
        if not task.goal_made_at_initialize:
            goal = (
                await service.call("GET", "/task/goal", address, timeout_s=self.call_timeout_s)
            ).get("goal")
            if goal != task.goal:
                raise AndroidWorldError(
                    f"AndroidWorld built {task.task} #{task.index} (suite seed {task.suite_seed}) with "
                    f"the goal {goal!r}, and the task list records {task.goal!r}: either its "
                    f"parameters cannot be rebuilt from its address, or the checkout or its "
                    f"dependencies differ from those the list was made with"
                )
        await service.call("POST", "/task/initialize", address, timeout_s=self.call_timeout_s)
        self._initialized = True
        await service.call(
            "POST", "/reset", {"go_home": task.start_on_home_screen}, timeout_s=self.call_timeout_s
        )
        if task.goal_made_at_initialize:
            goal = (
                await service.call("GET", "/task/goal", address, timeout_s=self.call_timeout_s)
            ).get("goal")
            self._device_goal = goal if isinstance(goal, str) else None

    async def read_score(self, *, answer: str | None) -> OracleReading | None:
        del answer  # only information-retrieval tasks score it, and check_setup refuses them
        service, task = self._service, self._task
        if service is None or task is None or not self._initialized:
            raise AndroidWorldError("no AndroidWorld task was initialized on this device")
        reply = await service.call(
            "GET", "/task/score", {"task_type": task.task, "task_idx": task.index}, timeout_s=None
        )
        # Some tasks answer a bool (RetroPlaylistDuration returns False for a
        # wrongly named playlist); AndroidWorld's own runner counts it as 0 or 1.
        score = reply.get("score")
        if not isinstance(score, int | float) or not math.isfinite(score):
            raise AndroidWorldError(f"GET /task/score answered no finite score: {reply!r}")
        provenance = {
            "family": task.family,
            "task": task.task,
            "index": str(task.index),
            "suite_seed": str(task.suite_seed),
            "seed": str(task.params["seed"]),
            "params_sha256": params_digest(task.params),
        }
        if self._device_goal is not None:
            provenance["goal"] = self._device_goal
        return OracleReading(
            score=float(score),
            oracle=self.oracle_id,
            oracle_version=PINNED_COMMIT,
            provenance=provenance,
        )

    async def stop(self) -> None:
        service = self._service
        if service is None:
            return
        if self._stopping is None or self._stopping.done():
            self._stopping = asyncio.ensure_future(self._stop(service))
        await finish(self._stopping)
        self._service = None
        self._task = None

    async def _stop(self, service: _Service) -> None:
        task, initialized = self._task, self._initialized
        self._initialized = False
        try:
            if initialized and task is not None:
                try:
                    await service.call(
                        "POST",
                        "/task/tear_down",
                        {"task_type": task.task, "task_idx": task.index},
                        timeout_s=self.call_timeout_s,
                    )
                except Exception as exc:  # the device is discarded with the run either way
                    logger.warning(
                        "AndroidWorld's tear-down of %s #%d failed: %s", task.task, task.index, exc
                    )
        finally:
            await service.close()

    @staticmethod
    def _parse(task: OracleTask | None) -> AndroidWorldTask:
        """*task* as an AndroidWorld task this oracle can start.

        Raises:
            AndroidWorldSetupError: There is none, it is no AndroidWorld task,
                it comes from another commit, it is scored on the agent's
                answer, which does not reach the oracle yet, or the service
                cannot build it again from its address.
        """
        if task is None:
            raise AndroidWorldSetupError(
                "the task carries no AndroidWorld task (oracle_task); run tasks from the "
                "androidworld task loader"
            )
        try:
            parsed = AndroidWorldTask.model_validate(task)
        except ValidationError as exc:
            raise AndroidWorldSetupError(
                f"the task's oracle_task is not an AndroidWorld task: {exc}"
            ) from None
        if parsed.commit != PINNED_COMMIT:
            raise AndroidWorldSetupError(
                f"the task list was made with AndroidWorld {parsed.commit}, and this oracle runs "
                f"{PINNED_COMMIT}: generate the list again with the pinned checkout"
            )
        if parsed.family == INFORMATION_RETRIEVAL:
            raise AndroidWorldSetupError(
                f"{parsed.task} is an information-retrieval task, scored on the agent's answer, "
                f"and the answer does not reach the oracle yet"
            )
        if parsed.task in UNREPRODUCIBLE:
            raise AndroidWorldSetupError(f"{parsed.task} cannot run: {UNREPRODUCIBLE[parsed.task]}")
        return parsed
