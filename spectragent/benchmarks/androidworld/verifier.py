"""AndroidWorld's live verifier: its own judgment of each task, on the device, at the scoring point.

``docs/architecture_design.md`` §4.10.1, §4.11, §11.2 ("任务裁判拆成两个基准组件").
Listed in the AndroidWorld benchmark's ``verifiers`` and hosted by the Android
capability (``AndroidLiveVerifier``). It needs the live environment and the
run's answer, so it judges at the scoring point, after the agent's run and
before the teardown, through the service the ``androidworld`` setup component
started: the service that prepared the task holds its state and AndroidWorld's
own success check (``scoring.py``'s shared
:class:`~spectragent.benchmarks.androidworld.scoring.AndroidWorldServiceVerifier`).
Only an information-retrieval task, scored on the agent's answer, gets the
run's answer, as AndroidWorld's own runner hands it over.

Its verdicts name it ``androidworld`` at the pinned AndroidWorld commit, with
no setting that changes a verdict, and are the benchmark's primary verdict
(``AndroidWorldBenchmark.success``).
"""

from __future__ import annotations

from dataclasses import dataclass

from spectragent.benchmarks.androidworld.scoring import AndroidWorldServiceVerifier
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.benchmarks.androidworld.tasks import INFORMATION_RETRIEVAL, AndroidWorldTask


@dataclass
class AndroidWorldVerifier(AndroidWorldServiceVerifier):
    """AndroidWorld's own judgment of a task, read from the service its setup component started."""

    __registry_key__ = "androidworld"
    __setup__ = AndroidWorldSetup

    def hands_over_the_answer(self, task: AndroidWorldTask) -> bool:
        """Only an information-retrieval task is scored on the agent's answer."""
        return task.family == INFORMATION_RETRIEVAL


__all__ = ["AndroidWorldVerifier"]
