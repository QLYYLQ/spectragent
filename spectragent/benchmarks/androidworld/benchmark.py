"""The ``androidworld`` benchmark (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

from typing import Any, ClassVar

from spectragent.benchmarks._registrar import BenchmarkBase
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.verdict import Verdict


class AndroidWorldBenchmark(BenchmarkBase):
    """AndroidWorld: tasks on an emulated phone, judged on the device at the scoring point (§11.2)."""

    __registry_key__ = "androidworld"
    __wiring__: ClassVar[dict[str, Any]] = {
        "task_loader": ("task_loader", ["androidworld"]),
    }
    __primary_verifier__ = "androidworld"

    def success(self, verdict: Verdict, result: AgentResult | None) -> float | None:
        """AndroidWorld's own rule: the device's score counts only when the agent declared the task done.

        Its runner records ``task_successful if done else 0``, partial credit
        included. A MiniWoB++ task's ending comes from its page, so its score
        alone counts (§11.2 "怎样与 AndroidWorld 比"). With no *result* the
        run's ending is unknown, so no success is claimed either way.
        """
        # The family as the task list and the verdict's evidence name it; read
        # where Android's own code defines it, so a rename cannot silently
        # zero every MiniWoB++ result. Imported here: naming the benchmark
        # imports no Android module.
        from spectragent.core.capabilities.android.oracle.androidworld_tasks import MINIWOB

        if verdict.status != "judged" or verdict.score is None:
            return None
        if verdict.evidence.get("family") == MINIWOB:
            return verdict.score
        if result is None:
            return None
        return verdict.score if result.agent_done == AgentDoneStatus.DONE else 0.0
