"""AndroidWorld task loader: a task list made offline in AndroidWorld's own venv.

``docs/architecture_design.md`` §11.2 ("任务裁判与 ``AndroidSetup``"). The list
(format ``androidworld-tasks-1``,
``core/capabilities/android/oracle/androidworld_tasks.py``) is a split file in
the usual data layout: ``data_dir`` (default
``$SPECTRAGENT_BENCHMARK_DATA/android/androidworld``) holds ``data_info.json``,
whose ``split`` maps the ``subset`` to the file. ``task_list.py`` beside the
oracle's launcher writes both.

Each listed task becomes one Android task: its id ``<task>/<suite seed>/<index>``,
its text the goal, and its record, unchanged, the setup's ``oracle_task``, which
the ``androidworld`` oracle reads. The list is read strictly at construction:
a file that cannot be read, is not JSON, declares another format, misses or
adds a field, or lists one task twice raises ``BenchmarkDataError`` and the
loader publishes nothing.

**Left out, with the reason in** :attr:`AndroidWorldTaskLoader.skipped`, and
counted in one log line: the tasks AndroidWorld's service cannot build again
from their address (``UNREPRODUCIBLE``, with its reason), which the oracle
would refuse. Information-retrieval tasks (family ``information_retrieval``,
25 of the 116) are kept like any other: they are scored on the agent's answer,
which the Runtime hands the oracle at the scoring point (§4.2).

AndroidWorld's own runner gives a task ``int(10 * complexity)`` steps; the
framework has no per-task step limit (the Loop's ``max_steps`` holds for every
task of a sweep), so the complexity is only kept in the task's ``metadata``.

The list's model is the Android package's, imported where the file is read:
every run imports this loader with the framework, and a run that names no
Android imports no Android module (§4.2.1).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from pydantic import ValidationError

from spectragent.core.data_loader._common import (
    BenchmarkDataError,
    resolve_data_dir,
    resolve_split_file,
)
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.core.registries import setup_class_for_env
from spectragent.schemas.task import TaskDefinition

logger = logging.getLogger(__name__)


@dataclass
class AndroidWorldTaskLoader(TaskLoaderBase):
    """AndroidWorld's tasks, scored on the device by the ``androidworld`` task oracle."""

    __registry_key__ = "androidworld"

    env_type: str = "android"

    _skipped: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    @property
    def skipped(self) -> Mapping[str, str]:
        """The listed tasks left out, by task id, and why."""
        return MappingProxyType(self._skipped)

    def _load(self) -> None:
        from spectragent.core.capabilities.android.oracle.androidworld_tasks import (
            UNREPRODUCIBLE,
            AndroidWorldTaskList,
        )

        path = resolve_split_file(
            resolve_data_dir(self.env_type, self.__registry_key__, self.data_dir), self.subset
        )
        try:
            listing = AndroidWorldTaskList.model_validate_json(path.read_bytes())
        except OSError as exc:
            raise BenchmarkDataError(
                f"cannot read the AndroidWorld task list {path}: {exc}"
            ) from exc
        except ValidationError as exc:
            raise BenchmarkDataError(
                f"{path} is not an AndroidWorld task list (format androidworld-tasks-1, written "
                f"by task_list.py): {exc}"
            ) from exc
        setup_type = setup_class_for_env(self.env_type).__registry_key__
        tasks: dict[str, TaskDefinition] = {}
        skipped: dict[str, str] = {}
        for task in listing.tasks:
            task_id = f"{task.task}/{task.suite_seed}/{task.index}"
            if task_id in tasks or task_id in skipped:
                raise BenchmarkDataError(f"{path} lists {task_id} more than once")
            if task.task in UNREPRODUCIBLE:
                skipped[task_id] = UNREPRODUCIBLE[task.task]
                continue
            tasks[task_id] = TaskDefinition.model_validate(
                {
                    "task_id": task_id,
                    "benchmark": self.__registry_key__,
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
        if not tasks:
            raise BenchmarkDataError(
                f"{path} has no task this loader runs: "
                + ("; ".join(f"{key}: {why}" for key, why in skipped.items()) or "it lists none")
            )
        if skipped:
            logger.info(
                "androidworld: left out %d of the %d listed tasks; the reasons are in the "
                "loader's skipped",
                len(skipped),
                len(listing.tasks),
            )
        self._tasks.update(tasks)
        self._skipped.update(skipped)
