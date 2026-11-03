"""AndroidWorld's live verifier: its own judgment of each task, on the device, at the scoring point.

``docs/architecture_design.md`` §4.10.1, §4.11, §11.2 ("任务裁判拆成两个基准组件").
Listed in the AndroidWorld benchmark's ``verifiers`` and hosted by the Android
capability (``AndroidLiveVerifier``). It needs the live environment and the
run's answer, so it judges at the scoring point, after the agent's run and
before the teardown, through the service the ``androidworld`` setup component
started: the service that prepared the task holds its state and AndroidWorld's
own success check (``scoring.read_score``).

Its verdicts name it ``androidworld`` at the pinned AndroidWorld commit, with
no setting that changes a verdict, and are the benchmark's primary verdict
(``AndroidWorldBenchmark.success``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from spectragent.benchmarks.androidworld import scoring
from spectragent.benchmarks.androidworld.service import PINNED_COMMIT, AndroidWorldError
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.core.capabilities.android.backends.base import AndroidProviderSession
from spectragent.core.capabilities.android.benchmark_setup import AndroidBenchmarkSetup
from spectragent.schemas.verdict import Judgment, VerifierIdentity
from spectragent.verifiers._registrar import VerifierBase
from spectragent.verifiers.base import VerifierNeed

#: The capability that hosts it: the one whose device it judges.
_ANDROID = "android"


@dataclass
class AndroidWorldVerifier(VerifierBase):
    """AndroidWorld's own judgment of a task, read from the service its setup component started."""

    __registry_key__ = "androidworld"
    __needs__: ClassVar[frozenset[VerifierNeed]] = frozenset({"live_environment", "answer"})

    @property
    def capability_id(self) -> str:
        return _ANDROID

    def identity(self) -> VerifierIdentity:
        """AndroidWorld at the pinned commit its service runs; no setting of its changes a verdict."""
        return VerifierIdentity(
            verifier_id=self.verifier_id,
            version=PINNED_COMMIT,
            config_signature=self.config_signature(),
        )

    async def judge(
        self,
        device: AndroidProviderSession,
        setup: AndroidBenchmarkSetup | None,
        *,
        answer: str | None,
    ) -> Judgment:
        """The task's judgment from the service *setup* started, given the run's *answer*.

        *device* is not read: AndroidWorld's own check reads the device through
        its service.

        Raises:
            AndroidWorldError: *setup* is not AndroidWorld's, no task was
                initialized, or the service refused the answer or gave no
                finite score.
        """
        if not isinstance(setup, AndroidWorldSetup):
            hosted = "none" if setup is None else repr(setup.setup_id)
            raise AndroidWorldError(
                f"the androidworld verifier judges through the service the androidworld setup "
                f"component started, and the android capability hosts {hosted}"
            )
        service, task = setup.started()
        return await scoring.read_score(service, task, answer=answer, device_goal=setup.made_goal())


__all__ = ["AndroidWorldVerifier"]
