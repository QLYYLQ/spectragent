"""``last`` — fork at each source run's last branchable step (M33).

The metrics chain's same-path replay (arch §4.16) restores every source run
to its last candidate and lets the teacher decide that step once more: every
earlier step is re-executed and verified. ``explicit`` cannot say "the last
step of each source" when the sources differ in length; this can.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from spectragent.core.data_loader.task.step_selector import StepSelectorBase


@dataclass
class LastStep(StepSelectorBase):
    """The latest branchable step of the source run (none when it has none)."""

    __registry_key__ = "last"

    def select(self, candidates: Sequence[int]) -> list[int]:
        return [max(candidates)] if candidates else []
