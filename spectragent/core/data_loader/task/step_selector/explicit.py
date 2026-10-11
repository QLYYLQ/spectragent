"""``explicit`` — fork at the steps named.

No ``from __future__ import annotations``: conscribe reads the knobs'
``Annotated`` metadata at class creation.
"""

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

    ``steps`` must name at least one step. That is checked here, at
    construction: the generated config does not carry a ``min_length``
    (conscribe drops it), so ``steps: []`` would otherwise validate and fork
    nowhere.
    """

    __registry_key__ = "explicit"

    steps: Annotated[
        list[int],
        Field(
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
