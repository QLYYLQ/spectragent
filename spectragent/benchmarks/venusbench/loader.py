"""VenusBench's task loader: AndroidWorld's, over VenusBench's task lists.

``docs/architecture_design.md`` §11.2 "VenusBench-Mobile 的接入方案". Its lists
have AndroidWorld's format (``spectragent benchmark fetch venusbench`` makes
them with AndroidWorld's ``task_list.py`` in VenusBench's venv), its data
directory follows its key (``<data root>/android/venusbench``), and each task
gets the step budget VenusBench's runner gives it, ``int(10 * complexity)``,
as AndroidWorld's loader gives it. It leaves out the tasks the first version
does not run (``tasks.py``'s ``UNSUPPORTED``), with their reasons in
``skipped``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from spectragent.benchmarks.venusbench.tasks import UNSUPPORTED
from spectragent.core.data_loader.task.androidworld import AndroidWorldTaskLoader
from spectragent.core.data_loader.task.base import StepBudget


@dataclass
class VenusBenchTaskLoader(AndroidWorldTaskLoader):
    """VenusBench's tasks, prepared and scored on the device by AndroidWorld's service on its checkout."""

    __registry_key__ = "venusbench"
    __step_budget__: ClassVar[StepBudget | None] = StepBudget(
        steps="int(10 x complexity) steps, 10 to 100 on the pinned commit",
        source=(
            "VenusBench-Mobile's _allocate_step_budget: https://github.com/inclusionAI/UI-Venus/"
            "blob/5b2c618ef146ea38890ea35dca8b07ec2d0284dd/android_world/suite_utils.py#L718-L729"
        ),
    )

    def _left_out(self) -> Mapping[str, str]:
        return UNSUPPORTED


__all__ = ["VenusBenchTaskLoader"]
