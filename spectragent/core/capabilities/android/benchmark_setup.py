"""What the Android capability asks of a benchmark setup component it hosts.

``docs/architecture_design.md`` §4.9 "基准组件由 capability 承载", §11.2. A
benchmark that prepares each of its tasks on the device itself (AndroidWorld:
its own service initializes the task) writes a setup component under the
benchmark, naming ``android`` as its host. ``AgentRuntime.prepare()`` routes it
to ``AndroidCapability``, which checks it (``check_setup``) and binds it last
(``bind_setup``); from then on the capability gives it the booted device and
calls it in this order: :meth:`AndroidBenchmarkSetup.check_setup` and
:meth:`~AndroidBenchmarkSetup.makes_goal` in ``prepare()`` (``check_setup``
again in the capability's own ``setup()``), :meth:`~AndroidBenchmarkSetup.start`
once the device is up, :meth:`~AndroidBenchmarkSetup.made_goal` after setup
when it makes the goal, :meth:`~AndroidBenchmarkSetup.answers_episode_over`
after each of the agent's steps and, when it does,
:meth:`~AndroidBenchmarkSetup.episode_over` once the screen is still, and
:meth:`~AndroidBenchmarkSetup.stop` first at teardown.

The component also says what it needs of the device beyond ADB
(:attr:`AndroidBenchmarkSetup.device_needs`); the capability hands that to the
provider's ``open()``, so a benchmark's needs never become a provider setting
the user must keep in step with it (the browser's isolation, derived from its
network source, is the precedent, §4.14). Without a component the device
boots with none.

A component that resets the device between tasks
(``resets_between_tasks``) lets a sweep set the next task up on the same
device (§4.10.1 "环境跨任务复用"): the capability then asks
:meth:`~AndroidBenchmarkSetup.stop` to keep what the component runs on the
device, and hands that to the next task's component in
:meth:`~AndroidBenchmarkSetup.start`.

An Android task's data for the component (``AndroidSetup.oracle_task``) is
opaque to the capability and handed over unread.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import JsonValue

if TYPE_CHECKING:
    from spectragent.core.capabilities.android.backends.base import (
        AndroidProviderSession,
        DeviceNeeds,
    )
    from spectragent.core.environment_reuse import KeptEnvironment

#: An Android task's data for the benchmark setup component that prepares it,
#: opaque to the capability (``AndroidSetup.oracle_task``).
OracleTask = Mapping[str, JsonValue]


@runtime_checkable
class AndroidBenchmarkSetup(Protocol):
    """Prepares and ends one task of a benchmark on the device the Android capability boots."""

    @property
    def setup_id(self) -> str: ...

    @property
    def capability_id(self) -> str: ...

    @property
    def device_needs(self) -> DeviceNeeds:
        """What the device must offer beyond ADB for this component to work."""
        ...

    def check_setup(self, task: OracleTask | None) -> None:
        """Raise, naming every problem, if :meth:`start` would refuse this task here.

        Reads configuration, files and *task* only: starts nothing.
        """
        ...

    async def start(
        self,
        device: AndroidProviderSession,
        task: OracleTask | None,
        *,
        kept: KeptEnvironment | None = None,
    ) -> None:
        """Prepare *task* on *device*, which the provider booted meeting :attr:`device_needs`.

        *kept* is what a component of this kind kept running on *device* when
        it ended the task before (:meth:`stop` with ``keep``), when the device
        was kept from that task; ``None`` on a device of its own. It is this
        component's from the call on: a start that fails leaves it for
        :meth:`stop` to release.
        """
        ...

    def makes_goal(self, task: OracleTask | None) -> bool:
        """Whether :meth:`start` makes *task*'s goal on the device, which completes the run's task text.

        Reads *task* only: ``prepare()`` asks it through the capability's
        ``sets_task_text`` (``docs/architecture_design.md`` §4.10). A benchmark
        whose goal is made as the task starts (AndroidWorld's MiniWoB++)
        answers ``True``.
        """
        ...

    def made_goal(self) -> str | None:
        """After :meth:`start`: the goal it made on the device, as the benchmark hands it to an agent.

        ``None`` when it made none: the task's goal is the listed one.
        """
        ...

    def answers_episode_over(self) -> bool:
        """After :meth:`start`: whether the benchmark ends this task's episode itself, so :meth:`episode_over` asks it.

        Reads the started task only. The capability waits for a still screen
        before it asks :meth:`episode_over`, and only for a task this answers
        ``True`` for (AndroidWorld: a MiniWoB++ task); every other task pays
        nothing (``docs/architecture_design.md`` §11.2).
        """
        ...

    async def episode_over(self) -> bool:
        """Whether the task's episode is over, as the benchmark decides; ``False`` for one it never ends.

        Asked after each of the agent's steps (``EpisodeEnd``,
        ``docs/architecture_design.md`` §4.1), so a task whose benchmark ends
        no episode answers without a call. Turns its own transport failures
        into ordinary exceptions and lets no ``CancelledError`` of its own escape.
        """
        ...

    async def stop(self, *, keep: bool = False) -> KeptEnvironment | None:
        """End the task and release what :meth:`start` acquired; safe after a partial start, and again.

        With *keep*, asked only of a component that resets the device between
        tasks and only after a start that completed, the device is kept for
        the next task: end the task with the reset, and return what it keeps
        running on the device for the next task's :meth:`start` (``None``:
        nothing) instead of releasing it. When the reset fails it releases
        what it acquired and raises, and the device is released too. Without
        *keep* it returns ``None``.
        """
        ...


__all__ = ["AndroidBenchmarkSetup", "OracleTask"]
