"""Eval loader protocol.

``BaseEvalLoader`` is the structural-typing contract every eval data
loader must satisfy. Loaders return ``EvalReference`` instances keyed
by ``task_id``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from spectragent.schemas.eval_reference import EvalReference


@runtime_checkable
class BaseEvalLoader(Protocol):
    """Protocol for stateful eval data loaders."""

    @property
    def benchmark_name(self) -> str: ...

    def __len__(self) -> int: ...

    def __getitem__(self, task_id: str) -> EvalReference: ...

    def get(self, task_id: str) -> EvalReference | None: ...

    def task_ids(self) -> list[str]: ...
