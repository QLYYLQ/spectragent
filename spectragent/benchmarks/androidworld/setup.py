"""The ``androidworld`` setup component: AndroidWorld's own service prepares and ends each task.

``docs/architecture_design.md`` §4.10.1, §11.2 ("任务裁判拆成两个基准组件").
Written in the AndroidWorld benchmark's ``setup`` block and hosted by the
Android capability (``AndroidBenchmarkSetup``). The service's lifecycle, the
task's start and end on it, what the device must offer and the reset between
tasks are the part it shares with VenusBench's component
(``service_setup.py``'s :class:`~spectragent.benchmarks.androidworld.service_setup.AndroidWorldServiceSetup`);
this subclass adds what only AndroidWorld's own tasks do:

- **a goal made at initialize**: a task whose goal is made then (MiniWoB++)
  or rewritten (information retrieval naming a date) reads its goal from
  ``/task/goal`` again after the reset, refused when it is no whole goal
  (:func:`~spectragent.benchmarks.androidworld.service.goal_after_initialize`);
  that goal completes the run's task text (:meth:`AndroidWorldSetup.made_goal`);
- **episode_over**, after each of the agent's steps: a MiniWoB++ task
  (:meth:`AndroidWorldSetup.answers_episode_over`), once the capability has
  waited for a still screen, asks ``GET /task/episode_over``, the endpoint the
  launcher adds, whether the page ended its episode; every other task answers
  ``False`` without a wait or a call.

The service makes the task again from its address, so a task whose parameters
it cannot rebuild cannot be the listed one: ``MarkorCreateFolder`` names its
folder after the host's clock when it is made (the only one of the pinned
commit's 116 tasks that reads the clock). Such tasks are named in
``UNREPRODUCIBLE``: the loader leaves them out and :meth:`AndroidWorldSetup.check_setup`
refuses them, so they never reach a booted device.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from spectragent.benchmarks.androidworld.service import (
    AndroidWorldError,
    AndroidWorldService,
    Installation,
    goal_after_initialize,
)
from spectragent.benchmarks.androidworld.service_setup import AndroidWorldServiceSetup
from spectragent.benchmarks.androidworld.tasks import MINIWOB, UNREPRODUCIBLE, AndroidWorldTask
from spectragent.core.capabilities.android.benchmark_setup import OracleTask


@dataclass
class AndroidWorldSetup(AndroidWorldServiceSetup):
    """AndroidWorld's tasks, prepared and ended on the device by AndroidWorld's own service."""

    __registry_key__ = "androidworld"
    __installation__ = Installation
    __left_out__ = UNREPRODUCIBLE

    #: The goal the device made at initialize, for a task whose goal is made then.
    _device_goal: str | None = field(default=None, init=False, repr=False)

    async def _prepare(self, service: AndroidWorldService, task: AndroidWorldTask) -> None:
        self._device_goal = None
        await super()._prepare(service, task)

    async def _after_reset(self, service: AndroidWorldService, task: AndroidWorldTask) -> None:
        """Read the goal again when initialize made or reworded it."""
        if task.goal_at_initialize != "unchanged":
            goal = (
                await service.call(
                    "GET",
                    "/task/goal",
                    {"task_type": task.task, "task_idx": task.index},
                    timeout_s=self.call_timeout_s,
                )
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


__all__ = ["AndroidWorldSetup"]
