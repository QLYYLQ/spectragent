"""A setup component that prepares and ends each task through AndroidWorld's own service.

``docs/architecture_design.md`` §4.10.1, §11.2 ("任务裁判拆成两个基准组件",
"VenusBench-Mobile 的接入方案"). :class:`AndroidWorldServiceSetup` is the
part AndroidWorld's setup component (``setup.py``) and VenusBench's share: the
service's lifecycle, the task's start and end on it, what the device must
offer, and :meth:`~AndroidWorldServiceSetup.check_setup`. It is abstract and
this module registers nothing, so a benchmark that subclasses it imports none
of AndroidWorld's registered components (§4.2.1). Each subclass registers
under its own ``setup_id`` and names its installation (:attr:`__installation__`,
which holds the pinned code) and the tasks it never starts
(:attr:`__left_out__`); AndroidWorld's adds what only its own tasks do (a
goal made or reworded at initialize, a MiniWoB++ episode the page ends).

Hosted by the Android capability (``AndroidBenchmarkSetup``), which gives it
the booted device. One task is one run, and a sweep may run several tasks on
one device (below):

1. **start**, once the provider booted the device meeting :attr:`device_needs`
   (the emulator's gRPC behind a token, adbd as root, the guest's Wi-Fi up,
   and a fresh device whose changes are thrown away with the run): the
   service starts (``service.py``), then the task: the suite its seed makes, a
   check that the service built the task the list records,
   ``/task/initialize`` and ``/reset`` onto the home screen or not as the
   initialized task says (``/task/start_on_home_screen``, which the launcher
   adds: a task may change it in its initialize, and AndroidWorld's runner
   reads it only then), then what the subclass does after the reset
   (:meth:`AndroidWorldServiceSetup._after_reset`).
2. **stop**, first at teardown: ``/task/tear_down`` (best effort, since the
   device's changes are thrown away with it; a failure is logged), then the
   service's process group.

**It resets the device between tasks** (``__resets_between_tasks__``), as
AndroidWorld's own runner does (``suite_utils._run_task`` of the pinned
commit, which VenusBench's fork keeps): one device and one service serve
every task, each task's ``initialize_task`` restores the snapshots of its own
apps and sets the device's clock, and its ``tear_down`` restores them again
and closes the recent apps. So a sweep may keep the device for the next task
(§4.10.1 "环境跨任务复用"). Then :meth:`AndroidWorldServiceSetup.stop` is
asked to keep the service: ``/task/tear_down`` must succeed (else the service
is stopped, the error raised and the device released), and the service stays
up on the device for the next task's component, which
:meth:`AndroidWorldServiceSetup.start` receives instead of starting another.
Keeping the service is what makes the reuse the runner's: a new service takes
every app's snapshot again from the device it starts on, which after a task
is that task's leftovers. What the reset leaves as the task left it carries
over, as in the runner: the apps outside the task's own, system settings it
does not set, and apps still running (warm starts).

The task is judged against the same service at the scoring point, by the
benchmark's live verifier (``scoring.py``'s ``AndroidWorldServiceVerifier``):
the Android capability hands it this component, whose
:meth:`AndroidWorldServiceSetup.started` gives the running service and the
task initialized on it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, ClassVar

from pydantic import Field, ValidationError

from spectragent.benchmarks._setup_registrar import BenchmarkSetupBase
from spectragent.benchmarks.androidworld.service import (
    FORWARDER_APK,
    AndroidWorldError,
    AndroidWorldService,
    AndroidWorldSetupError,
    Installation,
)
from spectragent.benchmarks.androidworld.tasks import AndroidWorldTask
from spectragent.core.capabilities.android.backends.base import AndroidProviderSession, DeviceNeeds
from spectragent.core.capabilities.android.benchmark_setup import OracleTask
from spectragent.core.capabilities.android.processes import finish
from spectragent.core.data_loader._common import validation_summary
from spectragent.core.environment_reuse import KeptEnvironment

logger = logging.getLogger(__name__)


@dataclass
class AndroidWorldServiceSetup(BenchmarkSetupBase):
    """A benchmark's tasks, prepared and ended on the device by AndroidWorld's own service."""

    __abstract__ = True
    __hosted_by__ = "android"
    __resets_between_tasks__ = True
    #: The installation it runs the service from; its ``code`` is the pinned code.
    __installation__: ClassVar[type[Installation]]
    #: Tasks of the pinned code it never starts: task key → why. Its task
    #: loader leaves them out with the same words.
    __left_out__: ClassVar[Mapping[str, str]]

    root: Annotated[
        str | None,
        Field(
            description=(
                "The benchmark's installation ('~' expanded): its venv (.venv/bin/python, "
                "Python 3.11), its checkout at the commit the benchmark pins (src/), the "
                f"accessibility forwarder its service installs (cache/a11y/{FORWARDER_APK}), and "
                "what else its service needs; spectragent benchmark setup installs it where this "
                "says. The AVD the provider boots must be the one its one-time setup prepared, "
                "and ffmpeg must be on Spectragent's PATH: AndroidWorld's music tasks make their "
                "mp3 files with it. None (the default): android/<benchmark_id>/installation "
                "under the benchmark data root ($SPECTRAGENT_BENCHMARK_DATA, else "
                "~/.cache/spectragent/benchmarks), whatever the task loader's data_dir says."
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
                "a MiniWoB++ episode is over. Initialize took up to 8 s on AndroidWorld, except "
                "OsmAndMarker: 46 s on a fresh boot, where the launcher twice waits up to 30 s "
                "for OsmAnd to make the table AndroidWorld clears; up to 55 s on VenusBench's "
                "SMS tasks, which wait in their own initialize. Reset took up to 27 s when "
                "AndroidWorld reinstalled its accessibility forwarder. The score is limited by "
                "the run's verify_timeout instead."
            ),
        ),
    ] = 120.0

    _service: AndroidWorldService | None = field(default=None, init=False, repr=False)
    _stopping: asyncio.Future[AndroidWorldService | None] | None = field(
        default=None, init=False, repr=False
    )
    _task: AndroidWorldTask | None = field(default=None, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)

    @property
    def installation(self) -> Installation:
        return self.__installation__.at(self.root)

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

        With no task (``config check --benchmark <alias>`` checks the
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
                f"the {self.setup_id} setup cannot start: " + "; ".join(problems)
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
        self._task = parsed
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
                    f"{self.__installation__.code.name} built {task.task} #{task.index} (suite "
                    f"seed {task.suite_seed}) with the goal {goal!r}, and the task list records "
                    f"{task.goal!r}: either its parameters cannot be rebuilt from its address, "
                    f"or the checkout or its dependencies differ from those the list was made with"
                )
        await service.call("POST", "/task/initialize", address, timeout_s=self.call_timeout_s)
        self._initialized = True
        # Where the episode starts, as the initialized task says: a task may
        # change it in its initialize, and the runner reads it only then.
        reply = await service.call(
            "GET", "/task/start_on_home_screen", address, timeout_s=self.call_timeout_s
        )
        go_home = reply.get("start_on_home_screen")
        if not isinstance(go_home, bool):
            raise AndroidWorldError(
                f"GET /task/start_on_home_screen answered no start_on_home_screen: {reply!r}"
            )
        await service.call("POST", "/reset", {"go_home": go_home}, timeout_s=self.call_timeout_s)
        await self._after_reset(service, task)

    async def _after_reset(self, service: AndroidWorldService, task: AndroidWorldTask) -> None:
        """What the subclass does once the task is initialized and reset; nothing here."""

    def makes_goal(self, task: OracleTask | None) -> bool:
        """Whether the task's goal is made on the device as it starts; ``False`` here."""
        return False

    def made_goal(self) -> str | None:
        """The goal the device made at initialize; ``None`` here: the listed goal is the goal."""
        return None

    def answers_episode_over(self) -> bool:
        """Whether the benchmark ends the started task's episode itself; ``False`` here."""
        return False

    async def episode_over(self) -> bool:
        """Whether the started task's episode is over; ``False`` here, with no call."""
        return False

    def started(self) -> tuple[AndroidWorldService, AndroidWorldTask]:
        """The running service and the task initialized on it: what the benchmark's verifier judges.

        Raises:
            AndroidWorldError: No task was initialized on this device.
        """
        service, task = self._service, self._task
        if service is None or task is None or not self._initialized:
            raise AndroidWorldError(
                f"no {self.__installation__.code.name} task was initialized on this device"
            )
        return service, task

    async def stop(self, *, keep: bool = False) -> AndroidWorldService | None:
        """End the task with ``/task/tear_down`` and stop the service; with *keep*, keep the service.

        Without *keep* the tear-down is best effort (the device is released
        with it). With *keep* the device serves the next task, so the
        tear-down, AndroidWorld's reset, must succeed: the service is
        returned, still running, for the next task's :meth:`start`; when the
        tear-down fails or no task was initialized, the service is stopped
        and the error raised. A cancellation that arrives while it ends the
        task waits for that, then stops a service it kept, since nobody takes
        it. Safe to repeat: a second call finds nothing.

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
        except asyncio.CancelledError:
            # The reset finished and kept the service, and nobody takes it now.
            if not stopping.cancelled() and stopping.exception() is None:
                left = stopping.result()
                if left is not None:
                    await finish(asyncio.ensure_future(left.close()))
            raise
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
        name = self.__installation__.code.name
        if keep:
            try:
                if tear_down is None:
                    raise AndroidWorldError(f"no {name} task was initialized on this device")
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
                        "%s's tear-down of %s #%s failed: %s",
                        name,
                        tear_down["task_type"],
                        tear_down["task_idx"],
                        exc,
                    )
        finally:
            await service.close()
        return None

    def _parse(self, task: OracleTask | None) -> AndroidWorldTask:
        """*task* as a task of the pinned code this component can start.

        Raises:
            AndroidWorldSetupError: There is none, it is no AndroidWorld task,
                it comes from another commit, or it is one of
                :attr:`__left_out__`.
        """
        code = self.__installation__.code
        fetch = f"fetch the task lists again with spectragent benchmark fetch {code.benchmark}"
        if task is None:
            raise AndroidWorldSetupError(
                f"the task carries no {code.name} task (oracle_task); run tasks from the "
                f"{code.benchmark} benchmark's task loader"
            )
        try:
            parsed = AndroidWorldTask.model_validate(task)
        except ValidationError as exc:
            raise AndroidWorldSetupError(
                f"the task's oracle_task is not an AndroidWorld task ({validation_summary(exc)}): "
                f"{fetch}"
            ) from None
        if parsed.commit != code.commit:
            raise AndroidWorldSetupError(
                f"the task list was made with {code.name} {parsed.commit}, and this setup runs "
                f"{code.commit}: {fetch}"
            )
        if parsed.task in self.__left_out__:
            raise AndroidWorldSetupError(
                f"{parsed.task} cannot run: {self.__left_out__[parsed.task]}"
            )
        return parsed


__all__ = ["AndroidWorldServiceSetup"]
