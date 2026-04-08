"""Task loader protocol.

``BaseTaskLoader`` is the structural-typing contract every task loader
must satisfy. It is environment-agnostic — browser, docker, tool sandbox
and other capabilities all consume the same protocol.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from spectragent.schemas.task import TaskDefinition


@runtime_checkable
class BaseTaskLoader(Protocol):
    """Protocol for stateful task data loaders.

    Loaders normalize raw benchmark data files into ``TaskDefinition``
    instances at construction time, then serve queries by ``task_id``.
    """

    @property
    def benchmark_name(self) -> str: ...

    @property
    def env_type(self) -> str: ...

    def __len__(self) -> int: ...

    def __getitem__(self, task_id: str) -> TaskDefinition: ...

    def get(self, task_id: str) -> TaskDefinition | None: ...

    def task_ids(self) -> list[str]: ...

    def get_task_context(self, task_id: str) -> dict[str, Any]:
        """Return task-specific runtime context.

        For browser tasks this typically returns ``{"start_url": ...}``.
        For ToolSandbox/tau-bench scenarios this is where per-task tool
        schemas live (``{"available_tools": [...]}``). The returned dict
        is forwarded by ``AgentRuntime`` to ``Capability.setup()`` so the
        capability can produce per-task action descriptors.
        """
        ...
