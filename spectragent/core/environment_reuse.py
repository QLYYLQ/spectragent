"""Reusing an environment across the tasks of a sweep.

``docs/architecture_design.md`` §4.10.1 "环境跨任务复用". Some capabilities bring
up something that carries one task's state into the next: a device, later a
container. By default every task gets a new one. A sweep sets a task up on the
one the task before it used only when both hold (the user's ruling of
2026-11-03):

- **the same environment identity**: each such capability says what its setup
  brings up (``ReusableEnvironment.environment_identity``: Android's is the
  provider, its settings and what the device must offer), and the next task's
  identities are those of the kept environment;
- **a setup that resets it**: the benchmark's setup component each such
  capability hosts declares that it resets the state between tasks
  (``BenchmarkSetup.resets_between_tasks``).

A task's environment is kept for the next only when its setup completed and
everything that ended the task, the setup component's reset included,
succeeded; the capability decides that at teardown, and otherwise releases it
as it always did. The browser's ``lifetime: experiment`` is not this: a fresh,
isolated context per task carries no task state over, so it stays a setting of
the browser's own (§4.9.x).

:class:`EnvironmentReuse` is owned by one sweep (``BenchmarkRunner``), never a
capability's private state, and lives in the sweep's
:class:`~spectragent.core.experiment.ExperimentScope`, which closes whatever is
still kept when the sweep ends. Before each task it decides, binds an
:class:`EnvironmentHandover` into the task's environment and says how many
tasks the environment served before this one, which the task's trace records
(``TraceConfig.earlier_tasks_on_environment``); after the task it takes back
what the teardown handed over.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from spectragent.benchmarks._setup_registrar import BenchmarkSetup
    from spectragent.core.environments.base import Environment

logger = logging.getLogger(__name__)


class KeptEnvironment(Protocol):
    """What a capability handed over at teardown instead of releasing it, for the next task's setup."""

    async def close(self) -> None:
        """Release it: what the capability's teardown would have released."""
        ...


@dataclass
class EnvironmentHandover:
    """What one task's environment takes from the task before it, and hands to the task after it.

    Keyed by ``capability_id``. A capability takes its part at setup
    (:meth:`take`) and, when :attr:`keep` says so and the task ended cleanly,
    hands it over at teardown (:meth:`hand_over`) instead of releasing it.

    Attributes:
        kept: What the task before handed over, for setup to take.
        keep: Whether this task's teardown may hand its environment over.
        handed_over: What this task's teardown handed over.
    """

    kept: dict[str, KeptEnvironment]
    keep: bool
    handed_over: dict[str, KeptEnvironment] = field(default_factory=dict)

    def take(self, capability_id: str) -> KeptEnvironment | None:
        """The environment kept for *capability_id*, which the caller now owns; ``None``: bring up a new one."""
        return self.kept.pop(capability_id, None)

    def hand_over(self, capability_id: str, kept: KeptEnvironment) -> None:
        """Keep *kept* for the next task instead of releasing it."""
        self.handed_over[capability_id] = kept


class EnvironmentReuse:
    """The environment one sweep keeps from a task for the next, and how many tasks it served."""

    def __init__(self) -> None:
        self._identities: dict[str, str] = {}
        self._kept: dict[str, KeptEnvironment] = {}
        #: How many tasks the kept environment has served.
        self._served = 0
        self._handover: EnvironmentHandover | None = None
        #: How many tasks the environment of the task under way served before it.
        self._served_before = 0

    async def before_task(self, env: Environment | None, setups: Sequence[BenchmarkSetup]) -> int:
        """Decide whether the next task runs on the kept environment, and bind the handover into *env*.

        *setups* are the setup components the task's benchmark has, about to
        be bound. A kept environment that cannot serve this task is closed
        first. Acquires nothing else.

        Returns:
            How many tasks the task's environment served before it: 0 for a
            new one.

        Raises:
            ValueError: *setups* cannot be routed to *env*'s capabilities.
        """
        if self._handover is not None:
            raise RuntimeError("the previous task's environment was not taken back (after_task)")
        identities = env.environment_identities(setups) if env is not None else {}
        resets = bool(identities) and _all_reset(identities, setups)
        if self._kept:
            why = (
                "its environment keeps nothing between tasks"
                if not identities
                else "its benchmark setup declares no reset between tasks"
                if not resets
                else "its environment identity differs"
                if identities != self._identities
                else None
            )
            if why is not None:
                logger.info("Environment reuse: a new environment for the next task: %s", why)
                await self.close()
        self._served_before = self._served if self._kept else 0
        if env is None or not identities:
            return 0
        self._identities = identities
        self._handover = EnvironmentHandover(kept=dict(self._kept), keep=resets)
        self._kept = {}
        env.bind_handover(self._handover)
        return self._served_before

    async def after_task(self) -> None:
        """Take back what the task's teardown handed over; close what it left half kept.

        The whole environment is kept or none of it: every capability that
        carries task state handed its part over, or setup never took any (the
        task stopped before setup), else every part still held is closed.
        """
        handover, self._handover = self._handover, None
        if handover is None:
            return
        expected = set(self._identities)
        if handover.handed_over and set(handover.handed_over) == expected and not handover.kept:
            self._kept, self._served = dict(handover.handed_over), self._served_before + 1
            return
        if handover.kept and set(handover.kept) == expected and not handover.handed_over:
            self._kept = dict(handover.kept)
            return
        self._served = 0
        await _close_each({**handover.kept, **handover.handed_over})

    async def close(self) -> None:
        """Close whatever is kept; the next task gets a new environment."""
        kept, self._kept, self._served = self._kept, {}, 0
        await _close_each(kept)


def _all_reset(identities: Mapping[str, str], setups: Sequence[BenchmarkSetup]) -> bool:
    """Whether every capability that carries task state hosts a setup component that resets it."""
    hosted = {setup.capability_id: setup for setup in setups}
    return all(cap in hosted and hosted[cap].resets_between_tasks for cap in identities)


async def _close_each(kept: Mapping[str, KeptEnvironment]) -> None:
    """Close every kept part, each even when another fails; a failure is logged, never raised.

    A part that cannot be closed must not fail the next task, nor a sweep
    whose tasks all ran.
    """
    for capability_id, part in kept.items():
        try:
            await part.close()
        except Exception:  # noqa: PERF203 — per-part isolation
            logger.exception(
                "Environment reuse: closing the kept %r environment failed", capability_id
            )


__all__ = [
    "EnvironmentHandover",
    "EnvironmentReuse",
    "KeptEnvironment",
]
