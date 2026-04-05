"""Browser task loader protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from spectragent.schemas.browser_task import BrowserTask


@runtime_checkable
class BaseBrowserTaskLoader(Protocol):
    """Protocol for stateful browser task data loaders."""

    @property
    def benchmark_name(self) -> str: ...

    def __len__(self) -> int: ...

    def __getitem__(self, task_id: str) -> BrowserTask: ...

    def get(self, task_id: str) -> BrowserTask | None: ...

    def task_ids(self) -> list[str]: ...
