"""Where an export reads benchmark data (arch §4.15 "基准数据"): decontamination's task
lists and the reference-leak scan's answers.

Through the loaders the benchmarks the export names configure (``spectragent
export --benchmark``, an alias or a path, §4.10.1) — each benchmark's task
loader and its ``eval_loader`` block, composed with every knob (``data_dir``,
``subset``) — matched to a benchmark by ``benchmark_name``. A benchmark with no
configured loader of a kind is read by its registered loader built with its
defaults (a task loader through ``for_reading``, which gives it no setting only
a run uses): the rule a benchmark resolves its own reference data by (its block
first, then the registry). A configured loader is the only one read — never a
default-built instance beside it, which would ignore the knobs its YAML set.
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
        eval_loaders: The named benchmarks' reference loaders, in the order named.
        task_loaders: The named benchmarks' task loaders, in the order named.
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
        """The loaders the benchmarks a composed export names configure: task and eval loaders.

        A named benchmark without an ``eval_loader`` block configures no eval
        loader here: its references come from the registered default, built
        when first needed (:meth:`registered_default`).
        """
        benchmarks = run.instances("benchmark")
        return cls(
            eval_loaders=[b.eval_loader for b in benchmarks if b.eval_loader is not None],
            task_loaders=[b.task_loader for b in benchmarks],
        )

    def configured(self, kind: LoaderKind, benchmark: str) -> tuple[Any, ...]:
        """The *kind* loaders the named benchmarks configure for *benchmark*, in the order named."""
        return tuple(
            loader
            for loader in self._configured[kind]
            if getattr(loader, "benchmark_name", None) == benchmark
        )

    def registered_default(self, kind: LoaderKind, benchmark: str, subset: str | None) -> Any:
        """*benchmark*'s registered *kind* loader, built once with its defaults (and *subset*).

        For a benchmark the export names no *kind* loader for — a
        configured one is never shadowed by a default-built second instance.

        Returns:
            The loader, or ``None`` when no *kind* loader is registered under
            *benchmark*.

        Raises:
            ValueError: A named benchmark configures a *kind* loader for *benchmark*.
            Exception: Whatever the loader raises when its data does not load.
        """
        if self.configured(kind, benchmark):
            raise ValueError(
                f"a benchmark the export names configures the {kind} loader of {benchmark!r}: "
                f"read it through that loader, not through a default-built one"
            )
        key = (kind, benchmark, subset)
        if key not in self._defaults:
            loader_cls = _REGISTRARS[kind].get_or_none(benchmark) if benchmark else None
            if loader_cls is None:
                self._defaults[key] = None
            elif kind == "task":
                # Built only to read its tasks: never given run settings (step_budget).
                self._defaults[key] = loader_cls.for_reading(subset)
            else:
                self._defaults[key] = loader_cls(subset=subset) if subset else loader_cls()
        return self._defaults[key]


__all__ = ["BenchmarkLoaders", "LoaderKind"]
