"""Task loader protocol.

``BaseTaskLoader`` is the structural-typing contract every task loader
must satisfy. It is environment-agnostic — browser, docker, tool sandbox
and other capabilities all consume the same protocol.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from spectragent.schemas.data_version import DataVersion
from spectragent.schemas.task import TaskContext, TaskDefinition


@dataclass(frozen=True)
class StepBudget:
    """The step budget a benchmark's public protocol gives each of its tasks, and where it says so.

    A task loader that declares one (``TaskLoaderBase.__step_budget__``) hands
    each task's budget to the Loop as the Loop input ``max_steps``; the
    configured ``max_steps`` stays the ceiling. ``config template`` names it
    beside the Loop's ``max_steps`` (``docs/architecture_design.md`` §4.10.1
    "基准的步数预算").
    """

    #: What each task gets, in a few words: ``25 steps``.
    steps: str
    #: Where the public protocol states it: a paper's section, or a file and lines at a pinned commit.
    source: str


@runtime_checkable
class BaseTaskLoader(Protocol):
    """Protocol for stateful task data loaders.

    Loaders normalize raw benchmark data files into ``TaskDefinition``
    instances at construction time, then serve queries by ``task_id``.
    """

    #: The step budget its benchmark's public protocol gives each task, which
    #: :meth:`get_task_context` hands the Loop as ``max_steps``; ``None``: the
    #: protocol states none, and the configured ``max_steps`` applies.
    __step_budget__: ClassVar[StepBudget | None]

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
        declares: a benchmark task's step budget ``max_steps`` when its
        benchmark gives one (:attr:`__step_budget__`), a branch task's
        ``checkpoint``. ``branch`` is the provenance of a branch task, empty
        for a benchmark task. An unknown ``task_id`` yields an empty
        ``TaskContext()``.
        """
        ...


def data_version_of(loaders: Iterable[BaseTaskLoader]) -> DataVersion | None:
    """The data version a run records, from its benchmarks' task *loaders*: the one that has one.

    A run has at most one benchmark that reads pinned data: a branch task's
    run also holds its task source, which reads recorded runs and has no
    version, beside the benchmark it inherited. ``None`` when none has one.
    """
    return next((v for loader in loaders if (v := loader.data_version) is not None), None)
