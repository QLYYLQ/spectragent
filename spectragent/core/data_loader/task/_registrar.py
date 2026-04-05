"""Browser task loader registry using conscribe.

Stateful loaders: data is loaded into memory at ``__post_init__``,
queries accept only ``task_id``.
"""

import logging
from dataclasses import dataclass, field
from typing import Annotated

from conscribe import create_registrar
from pydantic import Field

from spectragent.core.data_loader._common import (
    load_split_map,
    parse_file,
    resolve_data_dir,
)
from spectragent.core.data_loader.task.base import BaseBrowserTaskLoader
from spectragent.schemas.browser_task import BrowserTask

logger = logging.getLogger(__name__)

TaskLoaderRegistrar = create_registrar(
    "browser_task_loader",
    BaseBrowserTaskLoader,
    discriminator_field="benchmark_name",
)


@dataclass
class TaskLoaderBase(metaclass=TaskLoaderRegistrar.Meta):
    """Stateful browser task loader base.

    Subclasses auto-register via conscribe. Override ``subset`` type
    annotation with a ``Literal[...]`` for precise config stub generation.
    """

    __abstract__ = True
    __config_annotated_only__ = True

    subset: Annotated[str, Field("All", description="Data subset")] = "All"
    data_dir: Annotated[str | None, Field(None, description="Custom data dir")] = None

    # internal storage — not a config field
    _tasks: dict[str, BrowserTask] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._load()

    # ── Protocol implementation ──────────────────────────────────────

    @property
    def benchmark_name(self) -> str:
        return self.__registry_key__  # type: ignore[attr-defined]

    def __len__(self) -> int:
        return len(self._tasks)

    def __getitem__(self, task_id: str) -> BrowserTask:
        return self._tasks[task_id]

    def get(self, task_id: str) -> BrowserTask | None:
        return self._tasks.get(task_id)

    def task_ids(self) -> list[str]:
        return list(self._tasks.keys())

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
            task = BrowserTask.model_validate(raw)
            self._tasks[task.task_id] = task
