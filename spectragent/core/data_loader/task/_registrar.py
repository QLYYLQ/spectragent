"""Task loader registry using conscribe.

Stateful loaders: data is loaded into memory at ``__post_init__``,
queries accept only ``task_id``. Subclasses register automatically via
the ``TaskLoaderRegistrar`` metaclass.
"""

from dataclasses import dataclass, field
from typing import Annotated, Any

from conscribe import create_registrar
from pydantic import Field

from spectragent.core.data_loader._common import (
    BenchmarkDataError,
    parse_file,
    resolve_data_dir,
    resolve_split_file,
    resolve_task_id,
)
from spectragent.core.data_loader.task.base import BaseTaskLoader
from spectragent.schemas.task import TaskContext, TaskDefinition, setup_class_for_env

TaskLoaderRegistrar = create_registrar(
    "task_loader",
    BaseTaskLoader,
    discriminator_field="benchmark_name",
)


@dataclass
class TaskLoaderBase(metaclass=TaskLoaderRegistrar.Meta):
    """Stateful task loader base.

    Subclasses auto-register via conscribe. Override ``subset`` type
    annotation with a ``Literal[...]`` for precise config stub generation.

    Subclasses should override ``env_type`` if the loader produces tasks
    for a non-browser environment — it selects both the data directory
    (``benchmark_data/<env_type>/<benchmark>``) and the ``TaskSetup``
    variant. They may override ``_normalize_raw()`` to control how raw
    data is mapped into ``TaskDefinition``.
    """

    __abstract__ = True
    __config_annotated_only__ = True

    subset: Annotated[str, Field("All", description="Data subset")] = "All"
    data_dir: Annotated[
        str | None,
        Field(
            None,
            description=(
                "Data dir for this loader ('~' expanded). Overrides "
                "$SPECTRAGENT_BENCHMARK_DATA/<env_type>/<benchmark>."
            ),
        ),
    ] = None

    # internal storage — not a config field
    _tasks: dict[str, TaskDefinition] = field(default_factory=dict, init=False, repr=False)

    # Default environment type — subclasses targeting other envs override.
    env_type: str = "browser"

    def __post_init__(self) -> None:
        self._load()

    # ── Protocol implementation ──────────────────────────────────────

    @property
    def benchmark_name(self) -> str:
        return self.__registry_key__  # type: ignore[attr-defined]

    def __len__(self) -> int:
        return len(self._tasks)

    def __getitem__(self, task_id: str) -> TaskDefinition:
        return self._tasks[task_id]

    def get(self, task_id: str) -> TaskDefinition | None:
        return self._tasks.get(task_id)

    def task_ids(self) -> list[str]:
        return list(self._tasks.keys())

    def get_task_context(self, task_id: str) -> TaskContext:
        """Return what this task hands to the run — here, its environment part only.

        The ``env`` part delegates to the task's own setup variant, so each
        domain carries its own context forward: a browser task surfaces
        ``start_url``, a docker task surfaces its image / checkout commit /
        test node-ids. A dataset task has no loop inputs and no branch
        provenance. An unknown ``task_id`` yields an empty ``TaskContext()``.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return TaskContext()
        return TaskContext(env=task.setup.to_context())

    # ── Data loading ─────────────────────────────────────────────────

    def _load(self) -> None:
        root = resolve_data_dir(
            self.env_type,
            self.__registry_key__,  # type: ignore[attr-defined]
            self.data_dir,
        )
        for raw in parse_file(resolve_split_file(root, self.subset)):
            task = self._normalize_raw(raw)
            self._tasks[task.task_id] = task

    def _normalize_raw(self, raw: dict[str, Any]) -> TaskDefinition:
        """Map a raw record into a ``TaskDefinition``.

        The setup variant is selected by ``self.env_type`` (never
        hardcoded), and each variant declares via ``__raw_keys__`` which
        top-level raw keys belong to it — ``start_url`` for browser,
        ``base_commit`` / ``FAIL_TO_PASS`` / ... for docker. Those are
        moved into ``setup`` so they arrive typed instead of landing in
        ``TaskDefinition``'s ``extra="allow"`` catch-all. A nested
        ``setup`` dict in the raw data is merged in and wins.

        Everything else passes through, so unknown fields are preserved.
        Subclasses targeting upstream record shapes should override.
        """
        raw_copy = dict(raw)
        setup_cls = setup_class_for_env(self.env_type)

        # Identity first: resolving it must see the raw keys before any
        # of them are grafted onto the setup (SWE-bench's ``instance_id``
        # is both the identity and a setup field).
        task_id = resolve_task_id(raw_copy)

        setup_data: dict[str, Any] = dict(raw_copy.pop("setup", {}) or {})
        for key in setup_cls.__raw_keys__:
            if key in raw_copy:
                setup_data.setdefault(key, raw_copy.pop(key))

        declared = setup_data.get("setup_type")
        expected = setup_cls.setup_type_value()
        if declared is not None and declared != expected:
            raise BenchmarkDataError(
                f"Task {task_id!r}: record declares setup_type={declared!r} but "
                f"loader env_type={self.env_type!r} selects {expected!r}."
            )
        setup_data["setup_type"] = expected

        raw_copy["task_id"] = task_id
        raw_copy.pop("id", None)  # folded into task_id above
        raw_copy["setup"] = setup_data
        raw_copy["env_type"] = self.env_type  # the loader owns this identity
        raw_copy.setdefault(
            "benchmark",
            self.__registry_key__,  # type: ignore[attr-defined]
        )

        return TaskDefinition.model_validate(raw_copy)
