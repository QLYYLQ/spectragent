"""Online-Mind2Web benchmark task loader.

**The step budget.** Each task hands the Loop the steps Online-Mind2Web's
authors gave the open-source agents they ran on it, 25, as the Loop input
``max_steps``: "The open-source agents, including SeeAct, Browser Use, and
Agent-E, are evaluated online using Playwright, with a maximum step limit of
25 to control the cost of repeated actions." (the Online-Mind2Web paper,
arXiv:2504.01382v4, Appendix E). The 50 the same appendix gives Claude
Computer Use 3.7, run through another tool, is a cost choice too: "To reduce
cost, we set the maximum step limit to 50 for Claude Computer Use 3.7 and
disable the thinking mode." The paper ran only SeeAct, Browser Use and Agent-E
at 25; every other agent here (Skyvern, UI-TARS) gets it by extension, not
because the paper ran them. The configured ``max_steps`` stays the ceiling
(``docs/architecture_design.md`` §4.10.1 "基准的步数预算").
"""

from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal

from pydantic import Field

from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.core.data_loader.task.base import StepBudget
from spectragent.schemas.task import TaskContext

#: The steps Online-Mind2Web's own runs give an open-source agent on each task.
STEP_BUDGET = 25


@dataclass
class OnlineMind2WebTaskLoader(TaskLoaderBase):
    """Loads Online-Mind2Web benchmark tasks."""

    __registry_key__ = "online_mind2web"
    __step_budget__: ClassVar[StepBudget | None] = StepBudget(
        steps=f"{STEP_BUDGET} steps",
        source=(
            "the Online-Mind2Web paper, Appendix E, for open-source agents: "
            "https://arxiv.org/abs/2504.01382v4"
        ),
    )

    subset: Annotated[
        Literal["All", "Hard"],
        Field("All", description="Online-Mind2Web data subset"),
    ] = "All"

    def get_task_context(self, task_id: str) -> TaskContext:
        """The task's start page, and its step budget as the Loop input ``max_steps``.

        An unknown ``task_id`` yields an empty ``TaskContext()``, as for every loader.
        """
        context = super().get_task_context(task_id)
        if task_id in self._tasks:
            context.loop["max_steps"] = STEP_BUDGET
        return context
