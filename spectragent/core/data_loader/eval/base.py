"""Browser eval loader protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from spectragent.schemas.eval_reference import EvalReference


@runtime_checkable
class BaseBrowserEvalLoader(Protocol):
    """Protocol for stateful browser eval data loaders."""

    @property
    def benchmark_name(self) -> str: ...

    def __len__(self) -> int: ...

    def __getitem__(self, task_id: str) -> EvalReference: ...

    def get(self, task_id: str) -> EvalReference | None: ...

    def task_ids(self) -> list[str]: ...
