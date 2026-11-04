"""Task loader protocol.

``BaseTaskLoader`` is the structural-typing contract every task loader
must satisfy. It is environment-agnostic — browser, docker, tool sandbox
and other capabilities all consume the same protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from spectragent.schemas.data_version import DataVersion
from spectragent.schemas.task import TaskContext, TaskDefinition


@runtime_checkable
class BaseTaskLoader(Protocol):
    """Protocol for stateful task data loaders.

    Loaders normalize raw benchmark data files into ``TaskDefinition``
    instances at construction time, then serve queries by ``task_id``.
    """

    @property
    def benchmark_name(self) -> str: ...

    @property
    def subset(self) -> str:
        """The subset its tasks are from; with :attr:`benchmark_name`, what every run of them records."""
        ...

    @property
    def env_type(self) -> str: ...

    @property
    def data_version(self) -> DataVersion | None:
        """The version of the data its tasks were read from; ``None`` when no fetch recorded one.

        Every run records it (``TraceConfig.data_version``,
        ``docs/architecture_design.md`` §4.10.1 "数据").
        """
        ...

    def __len__(self) -> int: ...

    def __getitem__(self, task_id: str) -> TaskDefinition: ...

    def get(self, task_id: str) -> TaskDefinition | None: ...

    def task_ids(self) -> list[str]: ...

    def get_task_context(self, task_id: str) -> TaskContext:
        """Return what this task hands to the run, in its three parts.

        ``env`` is the environment setup context — for a browser task
        typically ``{"start_url": ...}``; for ToolSandbox/tau-bench
        scenarios, per-task tool schemas. ``AgentRuntime`` routes it to the
        capabilities' ``setup()``. ``loop`` holds the inputs the run's Loop
        declares (``checkpoint`` for a branch task) and ``branch`` the
        provenance of a branch task; both are empty for an ordinary
        benchmark task. An unknown ``task_id`` yields an empty
        ``TaskContext()``.
        """
        ...
