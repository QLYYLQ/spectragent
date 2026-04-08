"""Task loader registry using conscribe.

Stateful loaders: data is loaded into memory at ``__post_init__``,
queries accept only ``task_id``. Subclasses register automatically via
the ``TaskLoaderRegistrar`` metaclass.
"""

import logging
from dataclasses import dataclass, field
from typing import Annotated, Any

from conscribe import create_registrar
from pydantic import Field

from spectragent.core.data_loader._common import (
    load_split_map,
    parse_file,
    resolve_data_dir,
)
from spectragent.core.data_loader.task.base import BaseTaskLoader
from spectragent.schemas.task import BrowserSetup, TaskDefinition

logger = logging.getLogger(__name__)

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
    for a non-browser environment, and may override ``_normalize_raw()``
    to control how raw data is mapped into ``TaskDefinition``.
    """

    __abstract__ = True
    __config_annotated_only__ = True

    subset: Annotated[str, Field("All", description="Data subset")] = "All"
    data_dir: Annotated[str | None, Field(None, description="Custom data dir")] = None

    # internal storage — not a config field
    _tasks: dict[str, TaskDefinition] = field(
        default_factory=dict, init=False, repr=False
    )

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

    def get_task_context(self, task_id: str) -> dict[str, Any]:
        """Return task-specific runtime context.

        Default implementation: for browser tasks, surface ``start_url``
        from the task's ``BrowserSetup`` so capability setup can navigate
        to the right page. Subclasses may override for richer context.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return {}
        if isinstance(task.setup, BrowserSetup) and task.setup.start_url:
            return {"start_url": task.setup.start_url}
        return {}

    # ── Data loading ─────────────────────────────────────────────────

    def _load(self) -> None:
        root = resolve_data_dir(self.__registry_key__, self.data_dir)  # type: ignore[attr-defined]
        split_map = load_split_map(root)
        filename = split_map.get(self.subset)
        if not filename:
            logger.warning(
                "Subset %r not in split map (available: %s)",
                self.subset,
                list(split_map),
            )
            return
        for raw in parse_file(root / filename):
            task = self._normalize_raw(raw)
            self._tasks[task.task_id] = task

    def _normalize_raw(self, raw: dict[str, Any]) -> TaskDefinition:
        """Map a raw record into a ``TaskDefinition``.

        Default implementation handles browser benchmark records:
        ``start_url`` from the top level of the raw record is moved into
        ``setup.start_url`` (a ``BrowserSetup``). A nested ``setup`` dict
        in the raw data is merged in as well. All other raw fields pass
        through to ``TaskDefinition`` via Pydantic's ``extra="allow"``
        config, so unknown fields are preserved.

        Subclasses targeting non-browser benchmarks should override.
        """
        raw_copy = dict(raw)

        # Pull nested setup dict if present, then graft top-level
        # browser-setup fields onto it.
        setup_data: dict[str, Any] = dict(raw_copy.pop("setup", {}) or {})
        if "start_url" in raw_copy:
            setup_data.setdefault("start_url", raw_copy.pop("start_url"))
        setup_data.setdefault("setup_type", "browser")

        raw_copy["setup"] = setup_data
        raw_copy.setdefault("env_type", self.env_type)
        raw_copy.setdefault(
            "benchmark", self.__registry_key__  # type: ignore[attr-defined]
        )

        return TaskDefinition.model_validate(raw_copy)
