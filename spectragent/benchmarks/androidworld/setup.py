"""The ``androidworld`` setup component: AndroidWorld's own service prepares and ends each task.

``docs/architecture_design.md`` §4.10.1, §11.2 ("任务裁判拆成两个基准组件").
Written in the AndroidWorld benchmark's ``setup`` block and hosted by the
Android capability (``AndroidBenchmarkSetup``), which gives it the booted
device. One task is one run:

1. **start**, once the provider booted the device meeting :attr:`device_needs`
   (the emulator's gRPC behind a token, adbd as root, the guest's Wi-Fi up,
   and a fresh device whose changes are thrown away with the run): the
   service starts (``service.py``), then the task: the suite its seed makes, a
   check that the service built the task the list records,
   ``/task/initialize`` and ``/reset``. A task whose goal is made at initialize
   (MiniWoB++) or rewritten then (information retrieval naming a date) reads
   its goal from ``/task/goal`` again, refused when it is no whole goal
   (:func:`~spectragent.benchmarks.androidworld.service.goal_after_initialize`);
   that goal completes the run's task text (:meth:`AndroidWorldSetup.made_goal`).
2. **episode_over**, after each of the agent's steps: a MiniWoB++ task
   (:meth:`AndroidWorldSetup.answers_episode_over`), once the capability has
   waited for a still screen, asks ``GET /task/episode_over``, the endpoint the
   launcher adds, whether the page ended its episode; every other task answers
   ``False`` without a wait or a call.
3. **stop**, first at teardown: ``/task/tear_down`` (best effort, since the
   device's changes are thrown away with the run; a failure is logged), then
   the service's process group.

The task is judged against the same service at the scoring point
(``scoring.py``). Until AndroidWorld's live verifier lands, this component
hands its identity and its judgment to the capability's ``oracle_identity`` and
``read_score`` (``TaskOracle``), and the environment keeps them as the run's
live verdict: the scoring path is as it was when the capability's task oracle
held both halves.

The service makes the task again from its address, so a task whose parameters
it cannot rebuild cannot be the listed one: ``MarkorCreateFolder`` names its
folder after the host's clock when it is made (the only one of the pinned
commit's 116 tasks that reads the clock). Such tasks are named in
``UNREPRODUCIBLE``: the loader leaves them out and :meth:`AndroidWorldSetup.check_setup`
refuses them, so they never reach a booted device.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from pydantic import Field, ValidationError

from spectragent.benchmarks._setup_registrar import BenchmarkSetupBase
from spectragent.benchmarks.androidworld import scoring
from spectragent.benchmarks.androidworld.service import (
    FORWARDER_APK,
    PINNED_COMMIT,
    AndroidWorldError,
    AndroidWorldService,
    AndroidWorldSetupError,
    Installation,
    default_root,
    goal_after_initialize,
)
from spectragent.benchmarks.androidworld.tasks import MINIWOB, UNREPRODUCIBLE, AndroidWorldTask
from spectragent.core.capabilities.android.backends.base import AndroidProviderSession, DeviceNeeds
from spectragent.core.capabilities.android.benchmark_setup import OracleTask
from spectragent.core.capabilities.android.processes import finish
from spectragent.schemas.verdict import Judgment, VerifierIdentity

logger = logging.getLogger(__name__)


@dataclass
class AndroidWorldSetup(BenchmarkSetupBase):
    """AndroidWorld's tasks, prepared and ended on the device by AndroidWorld's own service."""

    __registry_key__ = "androidworld"
    __hosted_by__ = "android"

    root: Annotated[
        str | None,
        Field(
            description=(
                "The AndroidWorld installation ('~' expanded): its venv (.venv/bin/python, "
                "Python 3.11), its checkout at the commit the launcher is written against "
                "(src/), and the accessibility forwarder its one-time setup cached "
                f"(cache/a11y/{FORWARDER_APK}). The AVD the provider boots must be the one that "
                "setup prepared, and ffmpeg must be on Spectragent's PATH, as AndroidWorld's "
                "README installs it: its music tasks make their mp3 files with it. None (the "
                "default): android/androidworld/installation under the benchmark data root, "
                "$SPECTRAGENT_BENCHMARK_DATA/android/androidworld/installation, whatever the "
                "task loader's data_dir says."
            )
        ),
    ] = None
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
                "goal, initialize, reset, tear-down), and each question after a step whether "
                "a MiniWoB++ episode is over. Initialize took up to 8 s, except OsmAndMarker: "
                "46 s on a fresh boot, where the launcher twice waits up to 30 s for OsmAnd "
                "to make the table AndroidWorld clears. Reset took up to 27 s when "
                "AndroidWorld reinstalled its accessibility forwarder. The score is limited "
                "by the run's verify_timeout instead."
            ),
        ),
    ] = 120.0

    _service: AndroidWorldService | None = field(default=None, init=False, repr=False)
    _stopping: asyncio.Future[None] | None = field(default=None, init=False, repr=False)
    _task: AndroidWorldTask | None = field(default=None, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)
    #: The goal the device made at initialize, for a task whose goal is made then.
    _device_goal: str | None = field(default=None, init=False, repr=False)

    @property
    def installation(self) -> Installation:
        root = default_root() if self.root is None else Path(self.root).expanduser()
        return Installation(root)

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
        """Refuse, naming every problem, an installation the service cannot start from, and a task
        :meth:`start` would refuse.

        With no task (``config check --benchmark androidworld`` checks the
        configuration alone) only the installation is checked: :meth:`start`
        refuses a missing task, at setup.
        """
        problems = self.installation.problems()
        if task is not None:
            try:
                self._parse(task)
            except AndroidWorldSetupError as exc:
                problems.append(str(exc))
        if problems:
            raise AndroidWorldSetupError(
                "the androidworld setup cannot start: " + "; ".join(problems)
            )

    async def start(self, device: AndroidProviderSession, task: OracleTask | None) -> None:
        parsed = self._parse(task)
        emulator = device.emulator
        if emulator is None or emulator.grpc is None:
            raise AndroidWorldError(
                f"AndroidWorld drives an emulator over its gRPC endpoint, and the device "
                f"{device.provider_id!r} brought up offers none"
            )
        self._service = await AndroidWorldService.start(
            self.installation, device, emulator, emulator.grpc, self.start_timeout_s
        )
        self._task, self._device_goal = parsed, None
        await self._prepare(self._service, parsed)

    async def _prepare(self, service: AndroidWorldService, task: AndroidWorldTask) -> None:
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
        # Before initialize, the goal is the one the list records unless it is
        # made then (MiniWoB++: the service has none to give yet).
        if task.goal_at_initialize != "made":
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
        if task.goal_at_initialize != "unchanged":
            goal = (
                await service.call("GET", "/task/goal", address, timeout_s=self.call_timeout_s)
            ).get("goal")
            self._device_goal = goal_after_initialize(task, goal)

    def makes_goal(self, task: OracleTask | None) -> bool:
        """Whether the task's goal is made or reworded at initialize; ``False`` with no task to start."""
        return task is not None and self._parse(task).goal_at_initialize != "unchanged"

    def made_goal(self) -> str | None:
        return self._device_goal

    def answers_episode_over(self) -> bool:
        """Whether the started task is a MiniWoB++ task: AndroidWorld's runner ends only its episodes."""
        return self._task is not None and self._task.family == MINIWOB

    async def episode_over(self) -> bool:
        """Whether the page ended a MiniWoB++ task's episode; ``False`` for every other task, with no call.

        AndroidWorld's runner ends only a MiniWoB++ episode itself, when the
        page has reported a reward. The service answers through the endpoint
        the launcher adds (``GET /task/episode_over``), within ``call_timeout_s``.

        Raises:
            AndroidWorldError: No task was initialized, or the service failed or
                answered no ``over``.
            TimeoutError: The service gave no answer in time.
        """
        service, task = self._started()
        if not self.answers_episode_over():
            return False
        reply = await service.call(
            "GET",
            "/task/episode_over",
            {"task_type": task.task, "task_idx": task.index},
            timeout_s=self.call_timeout_s,
        )
        over = reply.get("over")
        if not isinstance(over, bool):
            raise AndroidWorldError(f"GET /task/episode_over answered no over: {reply!r}")
        return over

    def oracle_identity(self) -> VerifierIdentity | None:
        """Who judges the started task: AndroidWorld at its pinned commit (``TaskOracle``, through the capability)."""
        return scoring.identity()

    async def read_score(self, *, answer: str | None) -> Judgment:
        """The started task's judgment, given the run's *answer* (``TaskOracle``, through the capability).

        The scoring path stays as it was until AndroidWorld's live verifier
        lands and judges the task itself (``scoring.py``).

        Raises:
            AndroidWorldError: No task was initialized, or the service refused
                the answer or gave no finite score.
        """
        service, task = self._started()
        return await scoring.read_score(service, task, answer=answer, device_goal=self._device_goal)

    def _started(self) -> tuple[AndroidWorldService, AndroidWorldTask]:
        """The running service and the task initialized on it.

        Raises:
            AndroidWorldError: No task was initialized on this device.
        """
        service, task = self._service, self._task
        if service is None or task is None or not self._initialized:
            raise AndroidWorldError("no AndroidWorld task was initialized on this device")
        return service, task

    async def stop(self) -> None:
        service = self._service
        if service is None:
            return
        if self._stopping is None or self._stopping.done():
            self._stopping = asyncio.ensure_future(self._stop(service))
        await finish(self._stopping)
        self._service = None
        self._task = None

    async def _stop(self, service: AndroidWorldService) -> None:
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
        """*task* as an AndroidWorld task this component can start.

        Raises:
            AndroidWorldSetupError: There is none, it is no AndroidWorld task,
                it comes from another commit, or the service cannot build it
                again from its address.
        """
        if task is None:
            raise AndroidWorldSetupError(
                "the task carries no AndroidWorld task (oracle_task); run tasks from the "
                "androidworld benchmark's task loader"
            )
        try:
            parsed = AndroidWorldTask.model_validate(task)
        except ValidationError as exc:
            raise AndroidWorldSetupError(
                f"the task's oracle_task is not an AndroidWorld task: {exc}"
            ) from None
        if parsed.commit != PINNED_COMMIT:
            raise AndroidWorldSetupError(
                f"the task list was made with AndroidWorld {parsed.commit}, and this setup runs "
                f"{PINNED_COMMIT}: generate the list again with the pinned checkout"
            )
        if parsed.task in UNREPRODUCIBLE:
            raise AndroidWorldSetupError(f"{parsed.task} cannot run: {UNREPRODUCIBLE[parsed.task]}")
        return parsed


__all__ = ["AndroidWorldSetup"]
