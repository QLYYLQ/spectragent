"""``all`` — fork at every candidate step (the default selector)."""

from collections.abc import Sequence
from dataclasses import dataclass

from spectragent.core.data_loader.task.step_selector import StepSelectorBase


@dataclass
class AllSteps(StepSelectorBase):
    """Every branchable step of the source run."""

    __registry_key__ = "all"

    def select(self, candidates: Sequence[int]) -> list[int]:
        return sorted(set(candidates))
