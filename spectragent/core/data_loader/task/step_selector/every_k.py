"""``every_k`` — fork at every k-th step, counted by step number from an offset.

No ``from __future__ import annotations``: conscribe reads the knobs'
``Annotated`` metadata at class creation.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from spectragent.core.data_loader.task.step_selector import StepSelectorBase


@dataclass
class EveryKSteps(StepSelectorBase):
    """Steps ``offset``, ``offset + k``, ``offset + 2k``, … that are candidates.

    Counted by step number, not by position among the candidates: with
    ``k: 2`` a source whose step 4 is not branchable forks at 0, 2 and 6 —
    the step numbers a reader of the sweep expects — rather than shifting
    every later choice by one.
    """

    __registry_key__ = "every_k"

    k: Annotated[
        int,
        Field(default=1, ge=1, description="Fork at every k-th step number (1 = every step)."),
    ] = 1
    offset: Annotated[
        int,
        Field(
            default=0, ge=0, description="The first step number counted; earlier steps are skipped."
        ),
    ] = 0

    def select(self, candidates: Sequence[int]) -> list[int]:
        return sorted(
            {t for t in candidates if t >= self.offset and (t - self.offset) % self.k == 0}
        )
