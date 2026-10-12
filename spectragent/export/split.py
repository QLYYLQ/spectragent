"""Train / validation split and decontamination (arch §4.15 "切分与去污染").

**Never by step.** A sample's split is decided by its run's key — the source
task's ``start_url`` host (``split.by: site``) or
``<benchmark>/<subset>/<task_id>`` (``task``); a branch run uses its source
task, so every sample of one ``branch_group_id`` and of its source run lands
in the same split. The key's ``sha256(salt + "\\0" + key)``, read as a number
in [0, 1) from its first 8 bytes, below ``validation_fraction`` is
``validation``, otherwise ``train`` — the same key always lands in the same
split, whatever else is exported.

**Decontamination.** A source whose task is one of the student's future
evaluation tasks is left out (``decontaminated``). ``against`` lists those
benchmarks explicitly — nothing is inferred from the evaluator or from
verdicts; the default, ``[]``, decontaminates nothing, and the manifest says
``decontaminated: false``. ``match``: ``task_id`` (the same benchmark, a task
id of its eval loader), ``site_instruction`` (the same start-URL host and
normalised instruction as a task of its task loader) or ``either``. Benchmark
data is read through ``benchmarks.py`` — the export document's own loaders
first; a benchmark whose loaders cannot load fails the export:
decontamination that silently checked nothing is worse than none.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from spectragent.core.trace.sources import TaskIdentity
from spectragent.export.benchmarks import BenchmarkLoaders, LoaderKind
from spectragent.export.errors import ExportConfigError
from spectragent.export.settings import BenchmarkRef, Decontamination, SplitSettings

Split = Literal["train", "validation"]


def split_key(identity: TaskIdentity, settings: SplitSettings) -> str | None:
    """The key *identity*'s samples are split by, or ``None`` when it has none."""
    return identity.site if settings.by == "site" else identity.key


def split_of(key: str, settings: SplitSettings) -> Split:
    """``validation`` when the salted hash of *key* falls below the fraction, else ``train``."""
    digest = hashlib.sha256(f"{settings.salt}\0{key}".encode()).digest()
    position = int.from_bytes(digest[:8], "big") / 2**64
    return "validation" if position < settings.validation_fraction else "train"


def normalized_text(text: str) -> str:
    """*text* case-folded with every run of whitespace one space — how instructions compare."""
    return " ".join(text.casefold().split())


@dataclass
class _BenchmarkTasks:
    """One evaluation benchmark's task ids and ``(host, instruction)`` pairs."""

    task_ids: frozenset[str] = frozenset()
    sites: frozenset[tuple[str, str]] = frozenset()


@dataclass
class Decontaminator:
    """Which sources an evaluation benchmark rules out (module docstring).

    Attributes:
        against: The benchmarks checked — exactly the configured list.
        match: How a source's task is matched.
        loaders: Where the benchmarks' tasks are read (``benchmarks.py``).
    """

    against: tuple[BenchmarkRef, ...]
    match: Literal["task_id", "site_instruction", "either"]
    loaders: BenchmarkLoaders
    _tasks: dict[tuple[str, str | None], _BenchmarkTasks] = field(default_factory=dict)

    @classmethod
    def configure(cls, settings: Decontamination, *, loaders: BenchmarkLoaders) -> Decontaminator:
        """The checks *settings* name.

        Args:
            settings: The ``decontamination`` knobs.
            loaders: Where the benchmarks' tasks are read.
        """
        return cls(against=tuple(settings.against), match=settings.match, loaders=loaders)

    @property
    def decontaminated(self) -> bool:
        """Whether any benchmark is checked: ``against`` names at least one."""
        return bool(self.against)

    def reason(self, identity: TaskIdentity) -> str | None:
        """Why *identity*'s task is an evaluation task, or ``None``.

        Raises:
            ExportConfigError: A configured benchmark's loaders cannot load.
        """
        for ref in self.against:
            tasks = self._load(ref)
            label = ref.benchmark + (f"/{ref.subset}" if ref.subset else "")
            if (
                self.match in ("task_id", "either")
                and identity.benchmark == ref.benchmark
                and identity.task_id in tasks.task_ids
            ):
                return f"decontaminated: task {identity.task_id!r} is an evaluation task of {label}"
            if (
                self.match in ("site_instruction", "either")
                and identity.site is not None
                and (identity.site, normalized_text(identity.task)) in tasks.sites
            ):
                return (
                    f"decontaminated: site {identity.site} and instruction match an "
                    f"evaluation task of {label}"
                )
        return None

    def _load(self, ref: BenchmarkRef) -> _BenchmarkTasks:
        key = (ref.benchmark, ref.subset)
        if key not in self._tasks:
            self._tasks[key] = _BenchmarkTasks(
                task_ids=self._eval_task_ids(ref)
                if self.match != "site_instruction"
                else frozenset(),
                sites=self._task_sites(ref) if self.match != "task_id" else frozenset(),
            )
        return self._tasks[key]

    def _loaders_of(self, kind: LoaderKind, ref: BenchmarkRef) -> tuple[Any, ...]:
        """The *kind* loaders *ref* is read through: the document's, else the registered default.

        A configured loader is used whatever ``subset`` it was given, unless
        *ref* names another subset — two settings that disagree are an
        error, never a default-built loader beside the configured one.
        """
        configured = self.loaders.configured(kind, ref.benchmark)
        if configured:
            chosen = tuple(
                loader
                for loader in configured
                if ref.subset is None or getattr(loader, "subset", None) == ref.subset
            )
            if not chosen:
                subsets = sorted({str(getattr(loader, "subset", None)) for loader in configured})
                raise ExportConfigError(
                    f"decontamination against {ref.benchmark!r} subset {ref.subset!r}: the "
                    f"export document configures its {kind}_loader for subset(s) "
                    f"{', '.join(subsets)} only — configure one with subset {ref.subset!r} or "
                    f"drop the subset from the reference"
                )
            return chosen
        try:
            loader = self.loaders.registered_default(kind, ref.benchmark, ref.subset)
        except Exception as exc:  # the benchmark's data is not here, or does not load
            raise _cannot_load(ref, kind, exc) from exc
        if loader is None:
            raise ExportConfigError(
                f"decontamination against {ref.benchmark!r}: no {kind} loader is registered "
                f"under that key and the export document configures none (set "
                f"decontamination.against to the benchmarks you evaluate on, or [] for none)"
            )
        return (loader,)

    def _eval_task_ids(self, ref: BenchmarkRef) -> frozenset[str]:
        loaders = self._loaders_of("eval", ref)
        try:
            return frozenset(task_id for loader in loaders for task_id in loader.task_ids())
        except Exception as exc:
            raise _cannot_load(ref, "eval", exc) from exc

    def _task_sites(self, ref: BenchmarkRef) -> frozenset[tuple[str, str]]:
        loaders = self._loaders_of("task", ref)
        try:
            pairs: set[tuple[str, str]] = set()
            for loader in loaders:
                for task_id in loader.task_ids():
                    task = loader.get(task_id)
                    start_url = loader.get_task_context(task_id).env.get("start_url")
                    host = urlsplit(start_url).hostname if isinstance(start_url, str) else None
                    if task is not None and host:
                        pairs.add((host.lower(), normalized_text(task.task_text)))
            return frozenset(pairs)
        except Exception as exc:
            raise _cannot_load(ref, "task", exc) from exc


def _cannot_load(ref: BenchmarkRef, kind: LoaderKind, exc: Exception) -> ExportConfigError:
    return ExportConfigError(
        f"decontamination against {ref.benchmark!r}: its {kind} loader could not load "
        f"({type(exc).__name__}: {exc}); point it at the data with a {kind}_loader block in "
        f"the export document (benchmark_name: {ref.benchmark}, data_dir: ...) — "
        f"decontamination never checks nothing silently"
    )


__all__ = [
    "Decontaminator",
    "Split",
    "normalized_text",
    "split_key",
    "split_of",
]
