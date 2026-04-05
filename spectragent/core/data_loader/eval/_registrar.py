"""Browser eval loader registry using conscribe.

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
from spectragent.core.data_loader.eval.base import BaseBrowserEvalLoader
from spectragent.schemas.browser_task import TaskReference
from spectragent.schemas.eval_reference import EvalReference

logger = logging.getLogger(__name__)

EvalLoaderRegistrar = create_registrar(
    "browser_eval_loader",
    BaseBrowserEvalLoader,
    discriminator_field="benchmark_name",
)


@dataclass
class EvalLoaderBase(metaclass=EvalLoaderRegistrar.Meta):
    """Stateful browser eval loader base.

    Subclasses auto-register via conscribe. Override ``subset`` type
    annotation with a ``Literal[...]`` for precise config stub generation.
    """

    __abstract__ = True
    __config_annotated_only__ = True

    subset: Annotated[str, Field("All", description="Data subset")] = "All"
    data_dir: Annotated[str | None, Field(None, description="Custom data dir")] = None

    # internal storage — not a config field
    _refs: dict[str, EvalReference] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._load()

    # ── Protocol implementation ──────────────────────────────────────

    @property
    def benchmark_name(self) -> str:
        return self.__registry_key__  # type: ignore[attr-defined]

    def __len__(self) -> int:
        return len(self._refs)

    def __getitem__(self, task_id: str) -> EvalReference:
        return self._refs[task_id]

    def get(self, task_id: str) -> EvalReference | None:
        return self._refs.get(task_id)

    def task_ids(self) -> list[str]:
        return list(self._refs.keys())

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
            ref = EvalReference(
                task_id=raw["task_id"],
                benchmark=raw.get("benchmark", self.__registry_key__),  # type: ignore[attr-defined]
                expected_answer=raw.get("expected_answer"),
                reference=(
                    TaskReference(**raw["reference"])
                    if raw.get("reference")
                    else None
                ),
                difficulty=raw.get("difficulty"),
                tags=raw.get("tags", []),
                metadata=raw.get("metadata", {}),
            )
            self._refs[ref.task_id] = ref
