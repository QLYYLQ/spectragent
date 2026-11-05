"""VenusBench's live verifier: each task's own success check, on the device, at the scoring point.

``docs/architecture_design.md`` §4.10.1, §11.2 "VenusBench-Mobile 的接入方案".
The same code as AndroidWorld's live verifier
(``androidworld/scoring.py``'s ``AndroidWorldServiceVerifier``): it judges
through the service the ``venusbench`` setup component started
(``/task/score``). The run's answer goes to the service whenever the run has
one, as VenusBench's own runner hands every agent's ``answer`` action to the
environment whatever the task; each task decides whether to read it.

Its verdicts name it ``venusbench`` at the pinned VenusBench commit, with no
setting that changes a verdict, and are the benchmark's primary verdict.
The first version has no LLM judge.
"""

from __future__ import annotations

from dataclasses import dataclass

from spectragent.benchmarks.androidworld.scoring import AndroidWorldServiceVerifier
from spectragent.benchmarks.androidworld.tasks import AndroidWorldTask
from spectragent.benchmarks.venusbench.setup import VenusBenchSetup


@dataclass
class VenusBenchVerifier(AndroidWorldServiceVerifier):
    """VenusBench's own judgment of a task, read from the service its setup component started."""

    __registry_key__ = "venusbench"
    __setup__ = VenusBenchSetup

    def hands_over_the_answer(self, task: AndroidWorldTask) -> bool:
        """Every task gets the run's answer; the task decides whether to read it."""
        return True


__all__ = ["VenusBenchVerifier"]
