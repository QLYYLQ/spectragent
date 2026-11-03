"""AndroidWorld's own service, run out of process for each task, and the installation it runs from.

``docs/architecture_design.md`` §11.2 ("任务裁判与 ``AndroidSetup``", "``androidworld``
裁判"). AndroidWorld's FastAPI service runs in AndroidWorld's own venv at a
pinned commit (its pinned dependencies would clash with Spectragent's lock
file), against the run's emulator, while observations and actions go through
Spectragent's own bridge. The ``androidworld`` setup component
(``setup.py``) starts one per task once the device is up and stops it first
at teardown; the score is read from the same service (``scoring.py``).

:meth:`AndroidWorldService.start` runs the launcher
(``androidworld_service/serve.py``) under AndroidWorld's interpreter, in a
process group of its own tied to this process (``processes.py`` of the
Android package), on a free loopback port, with a whitelisted environment
whose ``PATH`` adds to the system's directories only a link to the host's
ffmpeg (:meth:`Installation.ffmpeg`), and waits for ``/health`` within a bound.
:meth:`AndroidWorldService.call` makes one bounded call and turns an error
answer, a broken reply or a dead service into :class:`AndroidWorldError`, its
message ending with the service's log; :meth:`AndroidWorldService.close`
stops the process group (SIGTERM, then SIGKILL for the whole group, its
``adb logcat`` child included).

The service handles one request at a time and cannot cancel one in progress:
a score call cut off by the run's limit goes on, and the tear-down waits
behind it until its own limit. Every service is the run's own, so nothing
waits on it afterwards.

Timings measured on the spike (Apple M1 Pro, load 6-14, 2026-11-01): start
17-32 s, initialize 0.6-8 s, reset 0.1-1.2 s (22-27 s when AndroidWorld
reinstalled its accessibility forwarder), score median 0.1 s, p90 6.1 s, max
18.2 s, tear-down 0.3-2.2 s. OsmAndMarker's initialize took 46 s on a fresh
boot (2026-11-03), while the launcher waited for OsmAnd to make the table
AndroidWorld clears (``serve.py``'s ``wait_for_the_table``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import JsonValue

from spectragent.benchmarks.androidworld.tasks import AndroidWorldTask
from spectragent.core.capabilities.android.adb_server import HOST, free_port
from spectragent.core.capabilities.android.backends.base import (
    AndroidProviderSession,
    EmulatorAccess,
    EmulatorGrpc,
)
from spectragent.core.capabilities.android.processes import ProcessGroup, finish, log_tail
from spectragent.core.data_loader._common import resolve_data_dir

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
#: The longest a call that got a server error waits for the service to log why:
#: Starlette answers before uvicorn logs the handler's traceback.
_TRACEBACK_WAIT_S = 2.0
_TRACEBACK_POLL_S = 0.05
#: The line uvicorn logs right before the traceback of a handler that raised.
_HANDLER_RAISED = "Exception in ASGI application"
_LOOPBACK = "localhost,127.0.0.1,::1"


def default_root() -> Path:
    """Where the installation is when the setup leaves ``root`` out.

    ``android/androidworld/installation`` under the benchmark data root
    (``$SPECTRAGENT_BENCHMARK_DATA``, the data root's default otherwise),
    read on each call as the data root is. It does not follow the task
    loader's ``data_dir``: the installation is not the task data.
    """
    return resolve_data_dir("android", "androidworld", None) / "installation"


class AndroidWorldError(RuntimeError):
    """AndroidWorld's service failed, answered an error, or answered something it never does."""


class AndroidWorldSetupError(ValueError):
    """The installation or the task is one the setup component would refuse to start; every problem named."""


@dataclass(frozen=True)
class Installation:
    """An AndroidWorld installation, laid out as the setup component's ``root`` describes."""

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

    def ffmpeg(self) -> Path:
        """Where Spectragent's own PATH finds ffmpeg, as an absolute path.

        Part of the installation (AndroidWorld's README: "Install ffmpeg"): its
        music tasks make their mp3 files with pydub, which runs ffmpeg from
        PATH, so the service's ``bin/``, the only directory its PATH adds to the
        system's, links to this one. pydub runs ffprobe only to read audio,
        which the pinned code never does, so a host without ffprobe runs every
        task.

        Raises:
            AndroidWorldSetupError: PATH has no ffmpeg; the message says why the service needs it.
        """
        found = shutil.which("ffmpeg")
        if found is None:
            raise AndroidWorldSetupError(
                "there is no ffmpeg on PATH: AndroidWorld generates its music tasks' mp3 files "
                "with pydub, which runs ffmpeg; install ffmpeg, as AndroidWorld's README says"
            )
        return Path(found).absolute()

    def problems(self) -> list[str]:
        """What keeps the service from starting here, one entry per problem."""
        found = self._problems_under_root()
        try:
            self.ffmpeg()
        except AndroidWorldSetupError as exc:
            found.append(str(exc))
        return found

    def _problems_under_root(self) -> list[str]:
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


def goal_after_initialize(task: AndroidWorldTask, goal: object) -> str:
    """*goal*, the goal AndroidWorld holds for *task* once it is initialized, when it is a whole one.

    A ``made`` goal (MiniWoB++) exists only after initialize, and the task list
    records only its fixed part: the template before its placeholder, ending in
    a full stop ("Follow the instructions shown on the top of the screen.").
    The device's goal goes on with the instruction the page made (": Click on
    the Submit button."). A page that had shown none when AndroidWorld read it
    leaves an empty one (an empty string, which AndroidWorld does not ask
    again for), so the goal is the fixed part alone. AndroidWorld gives the
    page a fixed 1 s to load; the launcher's initialize waits for the page to
    load before AndroidWorld's reset, and for its episode to start before the
    read (``serve.py``'s ``wait_for_the_cover``). A ``rewritten`` goal
    (information retrieval) has
    the date it names reworded, so only its being a goal is checked here; the
    check before initialize compared the rest (``docs/architecture_design.md``
    §11.2).

    Raises:
        AndroidWorldError: *goal* is not a string or is blank; a made goal does
            not start with the fixed part, or says nothing after it.
    """
    what = f"AndroidWorld made {task.task} #{task.index} (suite seed {task.suite_seed}) the goal"
    if not isinstance(goal, str) or not goal.strip():
        raise AndroidWorldError(f"{what} {goal!r}, which is no goal")
    if task.goal_at_initialize != "made":
        return goal
    fixed = task.goal.removesuffix(".")
    if not goal.startswith(fixed):
        raise AndroidWorldError(
            f"{what} {goal!r}, which does not start with the fixed part the task list records "
            f"({task.goal!r})"
        )
    if not goal[len(fixed) :].strip(" \t\n:."):
        raise AndroidWorldError(
            f"{what} {goal!r}, which says nothing after its fixed part: the page had shown no "
            f"instruction when AndroidWorld read it"
        )
    return goal


def _size(log: Path) -> int:
    """How many bytes *log* holds; 0 when it cannot be read."""
    try:
        return log.stat().st_size
    except OSError:
        return 0


def _logged_a_traceback(log: Path, since: int) -> bool:
    """Whether *log* holds, past byte *since*, a handler's whole traceback: its last line is the exception.

    Only what follows uvicorn's ``Exception in ASGI application`` counts.
    AndroidWorld logs tracebacks of its own while it handles a request (absl
    logs each adb command it retries), and those end before the handler fails.
    """
    try:
        with log.open("rb") as stream:
            stream.seek(since)
            written = stream.read().decode(errors="replace")
    except OSError:
        return False
    _, found, handler = written.rpartition(_HANDLER_RAISED)
    if not found or "Traceback (most recent call last):" not in handler:
        return False
    if not handler.endswith("\n"):
        return False
    last = [line for line in handler.splitlines() if line.strip()][-1]
    return not last.startswith((" ", "\t", "Traceback", "During handling", "The above exception"))


async def _until_traceback_logged(log: Path, since: int, limit_s: float) -> None:
    """Wait, at most *limit_s*, until *log* holds a handler's whole traceback written past byte *since*."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not _logged_a_traceback(log, since) and loop.time() < deadline:
        await asyncio.sleep(_TRACEBACK_POLL_S)


@dataclass
class AndroidWorldService:
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
    ) -> AndroidWorldService:
        """Start the service for *device* and wait, at most *timeout_s*, until it is healthy.

        The service is a second client of the run's adb server, *device*'s
        endpoint, through the same adb build and with the emulator's ``HOME``.

        Raises:
            AndroidWorldSetupError: Spectragent's PATH has no ffmpeg.
            AndroidWorldError: It exited first; the message ends with its log.
            TimeoutError: It was not healthy in time.
        """
        ffmpeg = installation.ffmpeg()
        port = free_port()
        group = await ProcessGroup.open()
        workdir: Path | None = None
        try:
            workdir = Path(tempfile.mkdtemp(prefix="spectragent-androidworld-"))
            (workdir / "tmp").mkdir()
            (workdir / "bin").mkdir()
            (workdir / "bin" / "ffmpeg").symlink_to(ffmpeg)
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
                    # bin/ holds the link to the host's ffmpeg and nothing else.
                    "PATH": f"{workdir / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
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
        body: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        """One call, with *body* as its JSON body when given; the JSON object it replies.

        *timeout_s* ``None`` leaves the limit to the caller (the scoring point's).
        After a server error (5xx) it waits, at most ``_TRACEBACK_WAIT_S``, for
        the service to log the handler's traceback, which uvicorn writes only
        after Starlette has answered, so the message ends with the cause.

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
        logged = _size(self.log)
        limit = asyncio.timeout(timeout_s)
        try:
            async with limit:
                response = await self.client.request(method, path, params=query, json=body)
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
        if response.status_code != 200:
            if response.status_code >= 500:
                # Its cause, the handler's traceback, reaches the log after the answer.
                await _until_traceback_logged(self.log, logged, _TRACEBACK_WAIT_S)
            raise AndroidWorldError(
                f"{what} answered HTTP {response.status_code} ({response.text[:200]!r}); the end "
                f"of the service's log:\n{log_tail(self.log, lines=8)}"
            )
        try:
            reply = response.json()
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            raise AndroidWorldError(
                f"{what} answered something that is not a JSON object: {response.text[:200]!r}"
            )
        return reply

    async def close(self) -> None:
        """Stop the service (SIGTERM, then SIGKILL for its group), close the client, remove its directory."""
        try:
            await self.group.close(self.process, grace_s=STOP_GRACE_S)
        finally:
            await self.client.aclose()
            shutil.rmtree(self.workdir, ignore_errors=True)
