"""Online-Mind2Web benchmark task loader.

**The step budget.** Each task hands the Loop the steps its benchmark YAML sets
in ``step_budget`` as the Loop input ``max_steps``, or none when that is null.
The field is required, so whoever writes the YAML decides; the shipped
``configs/benchmarks/online_mind2web.yaml`` sets the protocol's own 25 for
every agent. The paper says: "The open-source agents, including SeeAct,
Browser Use, and Agent-E, are evaluated online using Playwright, with a
maximum step limit of 25 to control the cost of repeated actions." (the
Online-Mind2Web paper, arXiv:2504.01382v4, Appendix E). The 50 the same
appendix gives Claude Computer Use 3.7, run through another tool, is a cost
choice too: "To reduce cost, we set the maximum step limit to 50 for Claude
Computer Use 3.7 and disable the thinking mode." The paper ran only SeeAct,
Browser Use and Agent-E at 25; every other agent here (Skyvern, UI-TARS) gets
it by extension, not because the paper ran them. The benchmark holds no
per-agent number; a run that wants the paper's 50-step computer-use setting
uses a copy of the benchmark YAML with ``step_budget: 50``. The configured
``max_steps`` stays the ceiling (``docs/architecture_design.md`` §4.10.1
"基准的步数预算").
"""

from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Literal, Self

from pydantic import Field

from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.core.data_loader.task.base import StepBudget
from spectragent.schemas.task import TaskContext

#: The steps Online-Mind2Web's own runs give an open-source agent on each task.
OFFICIAL_STEP_BUDGET = 25


@dataclass
class OnlineMind2WebTaskLoader(TaskLoaderBase):
    """Loads Online-Mind2Web benchmark tasks."""

    __registry_key__ = "online_mind2web"
    __step_budget__: ClassVar[StepBudget | None] = StepBudget(
        steps=f"{OFFICIAL_STEP_BUDGET} steps",
        source=(
            "the Online-Mind2Web paper, Appendix E, for open-source agents: "
            "https://arxiv.org/abs/2504.01382v4"
        ),
        value=OFFICIAL_STEP_BUDGET,
    )

    subset: Annotated[
        Literal["All", "Hard"],
        Field("All", description="Online-Mind2Web data subset"),
    ] = "All"
    step_budget: Annotated[
        int | None,
        Field(
            ge=1,
            strict=True,
            description=(
                "The steps each task gets, handed to the Loop as the Loop input max_steps (the "
                "configured max_steps stays the ceiling); null gives none. Required, so the "
                "benchmark YAML decides it; Online-Mind2Web's own runs gave 25."
            ),
        ),
    ] = field(kw_only=True)

    @classmethod
    def for_reading(cls, subset: str | None = None) -> Self:
        """Its tasks to read, given no step budget: only a run of them uses one."""
        if not subset:
            return cls(step_budget=None)
        if subset in ("All", "Hard"):
            return cls(subset=subset, step_budget=None)
        raise BenchmarkDataError(f"Online-Mind2Web has no subset {subset!r}: All or Hard")

    def get_task_context(self, task_id: str) -> TaskContext:
        """The task's start page, and ``step_budget`` as the Loop input ``max_steps`` when set.

        An unknown ``task_id`` yields an empty ``TaskContext()``, as for every loader.
        """
        context = super().get_task_context(task_id)
        if task_id in self._tasks and self.step_budget is not None:
            context.loop["max_steps"] = self.step_budget
        return context
