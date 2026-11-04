"""The ``androidworld`` setup component: AndroidWorld's own service prepares and ends each task.

``docs/architecture_design.md`` §4.10.1, §11.2 ("任务裁判拆成两个基准组件").
Written in the AndroidWorld benchmark's ``setup`` block and hosted by the
Android capability (``AndroidBenchmarkSetup``), which gives it the booted
device. One task is one run, and a sweep may run several tasks on one device
(below):

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
   device's changes are thrown away with it; a failure is logged), then the
   service's process group.

**It resets the device between tasks** (``__resets_between_tasks__``), as
AndroidWorld's own runner does (``suite_utils._run_task`` of the pinned
commit): one device and one service serve every task, each task's
``initialize_task`` restores the snapshots of its own apps and sets the
device's clock, and its ``tear_down`` restores them again and closes the recent
apps. So a sweep may keep the device for the next task (§4.10.1
"环境跨任务复用"). Then :meth:`AndroidWorldSetup.stop` is asked to keep the
service: ``/task/tear_down`` must succeed (else the service is stopped, the
error raised and the device released), and the service stays up on the device
for the next task's component, which :meth:`AndroidWorldSetup.start` receives
instead of starting another. Keeping the service is what makes the reuse
AndroidWorld's: a new service takes every app's snapshot again from the device
it starts on, which after a task is that task's leftovers. What AndroidWorld's
reset leaves as the task left it carries over, as in its own runner: the apps
outside the task's own, system settings it does not set, and apps still
running (warm starts).

The task is judged against the same service at the scoring point, by
AndroidWorld's live verifier (``verifier.py``): the Android capability hands it
this component, whose :meth:`AndroidWorldSetup.started` gives the running
service and the task initialized on it.

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
from spectragent.core.environment_reuse import KeptEnvironment

logger = logging.getLogger(__name__)


@dataclass
class AndroidWorldSetup(BenchmarkSetupBase):
    """AndroidWorld's tasks, prepared and ended on the device by AndroidWorld's own service."""

    __registry_key__ = "androidworld"
    __hosted_by__ = "android"
    __resets_between_tasks__ = True

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
    _stopping: asyncio.Future[AndroidWorldService | None] | None = field(
        default=None, init=False, repr=False
    )
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
        # tasks restore their apps from those: changes kept in the AVD would
        # become every later device's start.
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

    async def start(
        self,
        device: AndroidProviderSession,
        task: OracleTask | None,
        *,
        kept: KeptEnvironment | None = None,
    ) -> None:
        """Start the service on *device*, or take the one *kept* there, and prepare *task* on it.

        Raises:
            TypeError: *kept* is no AndroidWorld service.
            AndroidWorldSetupError: As :meth:`check_setup`, for *task*.
            AndroidWorldError: The device offers no emulator gRPC endpoint, or
                the service failed to prepare the task.
        """
        if kept is not None:
            if not isinstance(kept, AndroidWorldService):
                raise TypeError(f"what the device kept for AndroidWorld is no service: {kept!r}")
            self._service = kept
        parsed = self._parse(task)
        if self._service is None:
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
        service, task = self.started()
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

    def started(self) -> tuple[AndroidWorldService, AndroidWorldTask]:
        """The running service and the task initialized on it: what AndroidWorld's verifier judges.

        Raises:
            AndroidWorldError: No task was initialized on this device.
        """
        service, task = self._service, self._task
        if service is None or task is None or not self._initialized:
            raise AndroidWorldError("no AndroidWorld task was initialized on this device")
        return service, task

    async def stop(self, *, keep: bool = False) -> AndroidWorldService | None:
        """End the task with ``/task/tear_down`` and stop the service; with *keep*, keep the service.

        Without *keep* the tear-down is best effort (the device is released
        with it). With *keep* the device serves the next task, so the
        tear-down, AndroidWorld's reset, must succeed: the service is
        returned, still running, for the next task's :meth:`start`; when the
        tear-down fails or no task was initialized, the service is stopped
        and the error raised. Safe to repeat: a second call finds nothing.

        Raises:
            AndroidWorldError: With *keep*: no task was initialized on the
                device, or its tear-down failed.
            TimeoutError: With *keep*: the tear-down gave no answer in time.
        """
        service = self._service
        if service is None:
            return None
        stopping = self._stopping
        if stopping is None or stopping.done():
            self._stopping = stopping = asyncio.ensure_future(self._stop(service, keep=keep))
        try:
            kept = await finish(stopping)
        finally:
            self._service = None
            self._task = None
        return kept

    async def _stop(
        self, service: AndroidWorldService, *, keep: bool
    ) -> AndroidWorldService | None:
        task, initialized = self._task, self._initialized
        self._initialized = False
        tear_down = (
            {"task_type": task.task, "task_idx": task.index}
            if initialized and task is not None
            else None
        )
        if keep:
            try:
                if tear_down is None:
                    raise AndroidWorldError("no AndroidWorld task was initialized on this device")
                await service.call(
                    "POST", "/task/tear_down", tear_down, timeout_s=self.call_timeout_s
                )
            except BaseException:
                await finish(asyncio.ensure_future(service.close()))
                raise
            return service
        try:
            if tear_down is not None:
                try:
                    await service.call(
                        "POST", "/task/tear_down", tear_down, timeout_s=self.call_timeout_s
                    )
                except Exception as exc:  # the device is released with it either way
                    logger.warning(
                        "AndroidWorld's tear-down of %s #%s failed: %s",
                        tear_down["task_type"],
                        tear_down["task_idx"],
                        exc,
                    )
        finally:
            await service.close()
        return None

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
