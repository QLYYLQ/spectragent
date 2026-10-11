"""Where an export reads benchmark data (arch §4.15 "基准数据"): decontamination's task
lists and the reference-leak scan's answers.

Through the loaders the export document configures — its own ``eval_loader:`` /
``task_loader:`` layers, the run root's layers, composed with every knob
(``data_dir``, ``subset``) — matched to a benchmark by ``benchmark_name``. A
benchmark the document configures no loader for is read by its registered
loader built with its defaults: the rule ``spectragent-bench`` resolves its
eval loader by (config first, then the registry). A benchmark it does
configure is read only through those loaders — never by a default-built
instance beside a configured one, which would ignore the knobs the document
set.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from spectragent.core.composer import ComposedRun
from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar

#: ``eval``: references and task ids (``eval_loader``); ``task``: task texts
#: and setups (``task_loader``).
LoaderKind = Literal["eval", "task"]

_REGISTRARS = {"eval": EvalLoaderRegistrar, "task": TaskLoaderRegistrar}


class BenchmarkLoaders:
    """The benchmark loaders one export reads through (module docstring).

    Args:
        eval_loaders: The document's configured eval loaders, in document order.
        task_loaders: The document's configured task loaders, in document order.
    """

    def __init__(
        self, *, eval_loaders: Sequence[Any] = (), task_loaders: Sequence[Any] = ()
    ) -> None:
        self._configured: dict[LoaderKind, tuple[Any, ...]] = {
            "eval": tuple(eval_loaders),
            "task": tuple(task_loaders),
        }
        self._defaults: dict[tuple[LoaderKind, str, str | None], Any] = {}

    @classmethod
    def from_run(cls, run: ComposedRun) -> BenchmarkLoaders:
        """The loaders a composed export document configures (``eval_loader`` / ``task_loader``)."""
        return cls(
            eval_loaders=run.instances("eval_loader"),
            task_loaders=run.instances("task_loader"),
        )

    def configured(self, kind: LoaderKind, benchmark: str) -> tuple[Any, ...]:
        """The *kind* loaders the document configures for *benchmark*, in document order."""
        return tuple(
            loader
            for loader in self._configured[kind]
            if getattr(loader, "benchmark_name", None) == benchmark
        )

    def registered_default(self, kind: LoaderKind, benchmark: str, subset: str | None) -> Any:
        """*benchmark*'s registered *kind* loader, built once with its defaults (and *subset*).

        For a benchmark the document configures no *kind* loader for — a
        configured one is never shadowed by a default-built second instance.

        Returns:
            The loader, or ``None`` when no *kind* loader is registered under
            *benchmark*.

        Raises:
            ValueError: The document configures a *kind* loader for *benchmark*.
            Exception: Whatever the loader raises when its data does not load.
        """
        if self.configured(kind, benchmark):
            raise ValueError(
                f"the export document's {kind}_loader blocks configure {benchmark!r}: read it "
                f"through them, not through a default-built loader"
            )
        key = (kind, benchmark, subset)
        if key not in self._defaults:
            loader_cls = _REGISTRARS[kind].get_or_none(benchmark) if benchmark else None
            if loader_cls is None:
                self._defaults[key] = None
            else:
                self._defaults[key] = loader_cls(subset=subset) if subset else loader_cls()
        return self._defaults[key]


__all__ = ["BenchmarkLoaders", "LoaderKind"]
