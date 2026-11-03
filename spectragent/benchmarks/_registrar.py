"""The benchmark registry: one registered component per benchmark (``docs/architecture_design.md`` §4.10.1).

A benchmark owns where its tasks come from: its task loader, and the eval
loader that gives verdicts their reference answers. Both are written inside
its block, never as top-level sections, and a benchmark YAML holds that one
block under ``benchmark:``. A run selects it with ``--benchmark <alias|path>``;
the run configuration never holds one (:mod:`spectragent.benchmarks.selection`).

Each benchmark is a class in its own package, ``spectragent/benchmarks/<benchmark_id>/``,
imported only when a configuration names it (§4.2.1), and writes its complete
``__wiring__``: the task loader keys, and the eval loader keys, it takes.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol, runtime_checkable

from conscribe import create_registrar

from spectragent.core.data_loader.eval._registrar import EvalLoaderBase, EvalLoaderRegistrar
from spectragent.core.data_loader.eval.base import BaseEvalLoader
from spectragent.core.data_loader.task._registrar import TaskLoaderBase
from spectragent.core.data_loader.task.base import BaseTaskLoader


@runtime_checkable
class Benchmark(Protocol):
    """A benchmark, as the entry points that run and score its tasks see it."""

    @property
    def benchmark_id(self) -> str: ...

    @property
    def task_loader(self) -> BaseTaskLoader: ...

    def reference_loader(self) -> BaseEvalLoader | None:
        """The reference data its verdicts are scored against, or ``None`` when it has none."""
        ...


BenchmarkRegistrar = create_registrar(
    "benchmark",
    Benchmark,
    discriminator_field="benchmark_id",
)


class BenchmarkBase(metaclass=BenchmarkRegistrar.Meta):
    """Common base of every benchmark: its task loader and, optionally, its eval loader.

    A concrete benchmark sets ``__registry_key__`` and writes its complete
    ``__wiring__``, naming the task loader key it takes and, when it has
    reference data, the eval loader key. ``task_loader`` is required: the
    block says where the tasks come from and with which knobs (``subset``,
    ``data_dir``). ``eval_loader`` may be left out (:meth:`reference_loader`).
    """

    __abstract__ = True
    #: The ``benchmark_id`` this benchmark registers under.
    __registry_key__: ClassVar[str]
    __wiring__: ClassVar[dict[str, Any]] = {}

    # Receptors, annotation only: conscribe installs a ``WiredField`` and the
    # Composer injects the configured instances. ``__init__`` sets the eval
    # loader's in-Python default.
    task_loader: TaskLoaderBase
    eval_loader: EvalLoaderBase | None

    def __init__(self) -> None:
        self.eval_loader = None

    @property
    def benchmark_id(self) -> str:
        return self.__registry_key__

    def reference_loader(self) -> EvalLoaderBase | None:
        """The reference data this benchmark's verdicts are scored against.

        Its ``eval_loader`` block when it has one. Without one, the eval
        loader registered under its task loader's key, built for the task
        loader's subset: the reference data of the very tasks it runs, with
        that loader's defaults (``$SPECTRAGENT_BENCHMARK_DATA``). ``None``
        when no eval loader is registered under that key (a branch sweep's
        task source has none, §4.10). Built on each call: only a caller that
        scores asks, and it asks once.
        """
        configured = self.eval_loader
        if configured is not None:
            return configured
        tasks = self.task_loader
        loader_cls = EvalLoaderRegistrar.get_or_none(tasks.benchmark_name)
        return None if loader_cls is None else loader_cls(subset=tasks.subset)


__all__ = ["Benchmark", "BenchmarkBase", "BenchmarkRegistrar"]
