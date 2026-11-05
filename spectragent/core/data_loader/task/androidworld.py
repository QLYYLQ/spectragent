"""AndroidWorld task loader: a task list made offline in AndroidWorld's own venv.

``docs/architecture_design.md`` §11.2 ("任务裁判与 ``AndroidSetup``"). The list
(format ``androidworld-tasks-3``, ``spectragent/benchmarks/androidworld/tasks.py``)
is a split file in the usual data layout: ``data_dir`` (default
``<data root>/android/androidworld``, in the shared cache) holds ``data_info.json``,
whose ``split`` maps the ``subset`` to the file. ``task_list.py`` beside the
AndroidWorld launcher (``spectragent/benchmarks/androidworld/androidworld_service/``)
writes both; ``spectragent benchmark fetch androidworld`` runs it.

Each listed task becomes one Android task: its id ``<task>/<suite seed>/<index>``,
its text the goal, and its record, unchanged, the setup's ``oracle_task``, which
the ``androidworld`` benchmark's setup component reads. The list is read strictly at construction:
a file that cannot be read, is not JSON, declares another format, misses or
adds a field, or lists one task twice raises ``BenchmarkDataError`` and the
loader publishes nothing.

**The step budget.** A task's context hands the Loop the steps AndroidWorld's
own runner gives it, ``max_steps = int(10 * complexity)`` (10 to 120 on the
pinned commit), as ReAct's Loop input ``max_steps``. The configured
``max_steps`` stays the ceiling, so a sweep that should match AndroidWorld's
runner on every task configures at least 120 (``docs/architecture_design.md``
§4.1 "Loop 输入").

**Left out, with the reason in** :attr:`AndroidWorldTaskLoader.skipped`, and
counted in one log line: the tasks AndroidWorld's service cannot build again
from their address (``UNREPRODUCIBLE``, with its reason), which the setup
component would refuse. Information-retrieval tasks (family ``information_retrieval``,
25 of the 116) are kept like any other: they are scored on the agent's answer,
which the Runtime hands over at the scoring point (§4.2).

The list's model is the AndroidWorld benchmark's, imported where the file is
read: every run imports this loader with the framework, and a run that names
neither the benchmark nor Android imports neither (§4.2.1).

VenusBench's loader (``spectragent/benchmarks/venusbench/loader.py``) is a
subclass registered in VenusBench's package: its lists have the same format,
its data directory follows its key, and it leaves out the tasks its first
version does not run (:meth:`AndroidWorldTaskLoader._left_out`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ClassVar

from spectragent.core.data_loader._common import (
    BenchmarkDataError,
    read_data_version,
    resolve_data_dir,
    resolve_split_file,
)
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.core.data_loader.task.base import StepBudget
from spectragent.core.registries import setup_class_for_env
from spectragent.schemas.task import TaskContext, TaskDefinition

logger = logging.getLogger(__name__)


@dataclass
class AndroidWorldTaskLoader(TaskLoaderBase):
    """AndroidWorld's tasks, prepared and scored on the device by AndroidWorld's own service."""

    __registry_key__ = "androidworld"
    __step_budget__: ClassVar[StepBudget | None] = StepBudget(
        steps="int(10 x complexity) steps, 10 to 120 on the pinned commit",
        source=(
            "AndroidWorld's _allocate_step_budget: https://github.com/google-research/"
            "android_world/blob/55ddaf5edc6f6c25673aed343907d5b9dca66f84/android_world/"
            "suite_utils.py#L491-L502"
        ),
    )

    env_type: str = "android"

    _skipped: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _step_budgets: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    @property
    def skipped(self) -> Mapping[str, str]:
        """The listed tasks left out, by task id, and why."""
        return MappingProxyType(self._skipped)

    def get_task_context(self, task_id: str) -> TaskContext:
        """The task's setup context, and the step budget AndroidWorld's runner gives it.

        The budget is the Loop input ``max_steps``; an unknown ``task_id``
        yields an empty ``TaskContext()``, as for every loader.
        """
        context = super().get_task_context(task_id)
        if task_id in self._step_budgets:
            context.loop["max_steps"] = self._step_budgets[task_id]
        return context

    def _left_out(self) -> Mapping[str, str]:
        """The listed tasks this loader leaves out, by task key, and why: AndroidWorld's ``UNREPRODUCIBLE``."""
        from spectragent.benchmarks.androidworld.tasks import UNREPRODUCIBLE

        return UNREPRODUCIBLE

    def _load(self) -> None:
        from spectragent.benchmarks.androidworld.tasks import read_task_list

        benchmark = self.__registry_key__
        root = resolve_data_dir(self.env_type, benchmark, self.data_dir)
        path = resolve_split_file(root, self.subset)
        listing = read_task_list(path, benchmark=benchmark)
        left_out = self._left_out()
        setup_type = setup_class_for_env(self.env_type).__registry_key__
        tasks: dict[str, TaskDefinition] = {}
        step_budgets: dict[str, int] = {}
        skipped: dict[str, str] = {}
        for task in listing.tasks:
            task_id = f"{task.task}/{task.suite_seed}/{task.index}"
            if task_id in tasks or task_id in skipped:
                raise BenchmarkDataError(f"{path} lists {task_id} more than once")
            if task.task in left_out:
                skipped[task_id] = left_out[task.task]
                continue
            tasks[task_id] = TaskDefinition.model_validate(
                {
                    "task_id": task_id,
                    "benchmark": benchmark,
                    "env_type": self.env_type,
                    "task_text": task.goal,
                    "setup": {
                        "setup_type": setup_type,
                        "oracle_task": task.model_dump(mode="json"),
                    },
                    "tags": [task.family],
                    "metadata": {"complexity": task.complexity},
                }
            )
            step_budgets[task_id] = task.step_budget
        if not tasks:
            raise BenchmarkDataError(
                f"{path} has no task this loader runs: "
                + ("; ".join(f"{key}: {why}" for key, why in skipped.items()) or "it lists none")
            )
        if skipped:
            logger.info(
                "%s: left out %d of the %d listed tasks; the reasons are in the loader's skipped",
                benchmark,
                len(skipped),
                len(listing.tasks),
            )
        self._tasks.update(tasks)
        self._step_budgets.update(step_budgets)
        self._skipped.update(skipped)
        # After the list, as the base reads it (``TaskLoaderBase._load``).
        self._data_version = read_data_version(root)
