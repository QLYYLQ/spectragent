"""``explicit`` — fork at the steps named."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from spectragent.core.data_loader.task.step_selector import StepSelectorBase


@dataclass
class ExplicitSteps(StepSelectorBase):
    """The named steps, whether or not every source has them.

    Returns every named step (ascending, once); the loader skips a named step
    that is not a candidate of a given source — past its end, a restored or
    terminal step, a step whose observation failed — and records why in
    ``loader.skipped``, so a sweep over sources of different lengths says
    what it left out rather than shrinking silently.

    ``steps`` must name at least one step. The generated config enforces
    ``min_length``; construction checks it too for direct Python callers.
    """

    __registry_key__ = "explicit"

    steps: Annotated[
        list[int],
        Field(
            min_length=1,
            description=(
                "The step numbers to fork at (at least one). A step that is not "
                "branchable in some source is skipped for that source, with the "
                "reason recorded."
            ),
        ),
    ]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("step_selector 'explicit' needs at least one step in 'steps'")

    def select(self, candidates: Sequence[int]) -> list[int]:
        return sorted(set(self.steps))
