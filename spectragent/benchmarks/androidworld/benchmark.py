"""The ``androidworld`` benchmark (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

from typing import Any, ClassVar

from spectragent.benchmarks._registrar import BenchmarkBase
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.verdict import Verdict

#: The task family whose episode its page ends (AndroidWorld's MiniWoB++), as
#: the task list and the verdict's evidence name it (§11.2).
MINIWOB_FAMILY = "miniwob"


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
        alone counts (§11.2 "怎样与 AndroidWorld 比").
        """
        if verdict.status != "judged" or verdict.score is None:
            return None
        if verdict.evidence.get("family") == MINIWOB_FAMILY:
            return verdict.score
        done = result is not None and result.agent_done == AgentDoneStatus.DONE
        return verdict.score if done else 0.0
