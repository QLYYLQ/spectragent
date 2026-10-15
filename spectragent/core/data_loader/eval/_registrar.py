"""Eval loader registry using conscribe.

Stateful loaders: data is loaded into memory at ``__post_init__``,
queries accept only ``task_id``. Subclasses register automatically via
the ``EvalLoaderRegistrar`` metaclass.
"""

from dataclasses import dataclass, field
from typing import Annotated, Any

from conscribe import create_registrar
from pydantic import Field

from spectragent.core.data_loader._common import (
    parse_file,
    resolve_data_dir,
    resolve_split_file,
    resolve_task_id,
)
from spectragent.core.data_loader.eval.base import BaseEvalLoader
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.task import TaskReference

EvalLoaderRegistrar = create_registrar(
    "eval_loader",
    BaseEvalLoader,
    discriminator_field="benchmark_name",
)


@dataclass
class EvalLoaderBase(metaclass=EvalLoaderRegistrar.Meta):
    """Stateful eval loader base.

    Subclasses auto-register via conscribe. Override ``subset`` type
    annotation with a ``Literal[...]`` for precise config stub generation.

    Subclasses should override ``env_type`` when the benchmark's data
    lives outside ``benchmark_data/browser/``, and may override
    ``normalize_raw()`` to map an upstream record shape into
    ``EvalReference`` — the mirror of the task loader's hook.
    """

    __abstract__ = True
    __config_annotated_only__ = True

    subset: Annotated[str, Field("All", description="Data subset")] = "All"
    data_dir: Annotated[
        str | None,
        Field(
            None,
            description=(
                "Data dir for this loader ('~' expanded). None (the default): "
                "$SPECTRAGENT_BENCHMARK_DATA/<env_type>/<benchmark>."
            ),
        ),
    ] = None

    # internal storage — not a config field
    _refs: dict[str, EvalReference] = field(default_factory=dict, init=False, repr=False)

    # Default environment type — selects the data directory segment.
    env_type: str = "browser"

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
        root = resolve_data_dir(
            self.env_type,
            self.__registry_key__,  # type: ignore[attr-defined]
            self.data_dir,
        )
        for raw in parse_file(resolve_split_file(root, self.subset)):
            ref = self.normalize_raw(raw)
            self._refs[ref.task_id] = ref

    def normalize_raw(self, raw: dict[str, Any]) -> EvalReference:
        """Map a raw record into an ``EvalReference``.

        Extension point for loader authors. Return one reference without
        adding it to the loader's index; loading owns that bookkeeping.

        Identity is resolved through the same key precedence as the task
        side (``task_id`` > ``id`` > ``instance_id``), so upstream files
        that never adopted ``task_id`` load rather than raising.

        Subclasses targeting upstream record shapes should override.
        """
        reference = raw.get("reference")
        return EvalReference(
            task_id=resolve_task_id(raw),
            benchmark=raw.get("benchmark", self.__registry_key__),  # type: ignore[attr-defined]
            expected_answer=raw.get("expected_answer"),
            reference=TaskReference(**reference) if reference else None,
            difficulty=raw.get("difficulty"),
            tags=raw.get("tags", []),
            metadata=raw.get("metadata", {}),
        )
