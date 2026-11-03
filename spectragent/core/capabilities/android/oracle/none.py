"""The ``none`` task oracle: the device is not prepared, judged or ended for any benchmark.

The default. A run on it is not judged at the scoring point and leaves no
verdict (``docs/architecture_design.md`` §11.2). A task that carries data for
an oracle is refused: it would run unprepared and unjudged, and nothing would
say so.
"""

from __future__ import annotations

from spectragent.core.capabilities.android.backends.base import (
    AndroidProviderSession,
    DeviceNeeds,
)
from spectragent.core.capabilities.android.oracle._registrar import (
    AndroidTaskOracleBase,
    OracleTask,
)
from spectragent.schemas.verdict import Judgment, VerifierIdentity


class NoTaskOracle(AndroidTaskOracleBase):
    """No benchmark: the device boots as configured and nothing scores the run."""

    __registry_key__ = "none"

    @property
    def device_needs(self) -> DeviceNeeds:
        return DeviceNeeds()

    def check_setup(self, task: OracleTask | None) -> None:
        if task is not None:
            raise ValueError(
                "the task carries data for a task oracle (oracle_task), but the Android "
                "capability's task_oracle is 'none': the task would run unprepared and "
                "unscored. Select the oracle the task was made for, such as "
                "task_oracle: {oracle_id: androidworld, root: ...}."
            )

    async def start(self, device: AndroidProviderSession, task: OracleTask | None) -> None:
        return None

    def makes_goal(self, task: OracleTask | None) -> bool:
        return False

    def made_goal(self) -> str | None:
        return None

    def answers_episode_over(self) -> bool:
        return False

    async def episode_over(self) -> bool:
        return False

    def identity(self) -> VerifierIdentity | None:
        return None

    async def read_score(self, *, answer: str | None) -> Judgment:
        raise RuntimeError("no task oracle judges this run: its identity is None")

    async def stop(self) -> None:
        return None
