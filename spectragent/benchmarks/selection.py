"""``--benchmark <alias|path.yaml>``: which benchmark a run uses (``docs/architecture_design.md`` §4.10.1).

A benchmark YAML is one more document of the run root, holding only a
``benchmark:`` section with one block. An alias names a shipped one,
``configs/benchmarks/<alias>.yaml`` in a source checkout (an installed wheel
ships none); a path names the user's own. The selection is a path when it ends
in ``.yaml`` / ``.yml`` or holds a path separator, and an alias otherwise. The
run configuration never holds a benchmark: a ``benchmark:`` section in it is
refused when the run is composed (:func:`run_root`), whether or not
``--benchmark`` was given.

``--subset`` selects which of the benchmark's tasks run, and is the one knob
the command line sets on a benchmark: it is written into the selected block's
task loader before the run is composed (:func:`read_benchmark`), so the
resolved configuration and every run's identity record it, as if the YAML had
said it.

Every run of a benchmark's task records how its benchmark was selected
(:func:`spec_of`, ``TraceConfig.benchmark_spec``). A branch task belongs to its
source run's benchmark and finds it again by that record (:func:`read_spec`):
its run root holds the branch sweep's task source and the source's benchmark
block (``docs/architecture_design.md`` §4.10.1 "分叉 sweep 是一种任务来源").
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from spectragent import REPO_ROOT
from spectragent.schemas.benchmark_spec import BenchmarkSpec

#: The run root section a benchmark YAML holds, and the only one.
BENCHMARK_SECTION = "benchmark"
#: Where the shipped benchmark YAMLs are, one per alias.
ALIAS_DIR = REPO_ROOT / "configs" / "benchmarks"


class BenchmarkSelectionError(ValueError):
    """A ``--benchmark`` selection that names no readable benchmark YAML, or a run root that holds one."""


def is_path(selection: str) -> bool:
    """Whether *selection* names a file rather than a shipped alias."""
    return selection.endswith((".yaml", ".yml")) or "/" in selection or os.sep in selection


def aliases() -> tuple[str, ...]:
    """The shipped aliases, sorted."""
    return tuple(sorted(path.stem for path in ALIAS_DIR.glob("*.yaml")))


def read_benchmark(selection: str, *, subset: str | None = None) -> dict[str, Any]:
    """The one ``benchmark:`` block of the benchmark YAML *selection* names (module docstring).

    With *subset* (``--subset``), the block's task loader takes that subset.

    Raises:
        BenchmarkSelectionError: No such alias or file, a file that does not
            read as YAML, a document that is not exactly one block under
            ``benchmark:``, or a *subset* for a block with no task loader.
    """
    if is_path(selection):
        path = Path(selection).expanduser()
        if not path.is_file():
            raise BenchmarkSelectionError(f"--benchmark {selection}: no such file")
    else:
        path = ALIAS_DIR / f"{selection}.yaml"
        if not path.is_file():
            shipped = aliases()
            where = (
                f"aliases: {', '.join(shipped)}, the YAMLs in {ALIAS_DIR}"
                if shipped
                else f"this installation has no benchmark YAMLs in {ALIAS_DIR}: aliases are "
                f"the YAMLs under configs/benchmarks/ of a source checkout"
            )
            raise BenchmarkSelectionError(
                f"--benchmark {selection}: no shipped benchmark has this alias ({where}); "
                f"otherwise pass the path of a benchmark YAML"
            )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BenchmarkSelectionError(
            f"--benchmark {selection}: {path} does not read: {exc}"
        ) from exc
    if not isinstance(raw, Mapping) or set(raw) != {BENCHMARK_SECTION}:
        found = sorted(map(str, raw)) if isinstance(raw, Mapping) else type(raw).__name__
        raise BenchmarkSelectionError(
            f"--benchmark {selection}: {path} must hold only a '{BENCHMARK_SECTION}:' section "
            f"(found {found}); the agent, its environment and its hooks are the run "
            f"configuration's"
        )
    blocks = raw[BENCHMARK_SECTION]
    if not (isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], Mapping)):
        raise BenchmarkSelectionError(
            f"--benchmark {selection}: {path} must hold exactly one benchmark block under "
            f"'{BENCHMARK_SECTION}:'"
        )
    block = dict(blocks[0])
    if subset is not None:
        loader = block.get("task_loader")
        if not isinstance(loader, Mapping):
            raise BenchmarkSelectionError(
                f"--subset {subset}: the benchmark block of {path} has no task_loader to take it"
            )
        block["task_loader"] = {**loader, "subset": subset}
    return block


def spec_of(selection: str, *, benchmark_id: str, subset: str | None = None) -> BenchmarkSpec:
    """How the benchmark *benchmark_id* was selected: *selection* (``--benchmark``) and *subset*.

    A path is recorded absolute, so a branch sweep run from another directory
    finds the same file; an alias as it was given.
    """
    recorded = str(Path(selection).expanduser().resolve()) if is_path(selection) else selection
    return BenchmarkSpec(benchmark_id=benchmark_id, selection=recorded, subset=subset)


def read_spec(spec: BenchmarkSpec) -> dict[str, Any]:
    """The benchmark block *spec* records the selection of, read again as :func:`read_benchmark` reads it.

    What a branch task of a run that recorded *spec* composes with: its source
    run's benchmark, as its YAML says now.

    Raises:
        BenchmarkSelectionError: The selection no longer reads (what
            :func:`read_benchmark` refuses), or its block is another
            benchmark's.
    """
    block = read_benchmark(spec.selection, subset=spec.subset)
    found = block.get("benchmark_id")
    if found != spec.benchmark_id:
        raise BenchmarkSelectionError(
            f"--benchmark {spec.selection}: its block is the benchmark {found!r}, not "
            f"{spec.benchmark_id!r}, the one the run selected with it"
        )
    return block


def run_root(raw: object, *benchmarks: dict[str, Any]) -> dict[str, Any]:
    """The run root a run composes: *raw*'s sections, and the *benchmarks* blocks under ``benchmark:``.

    One block, the one ``--benchmark`` selected; for a branch task, the branch
    sweep's task source and the benchmark the task belongs to (:func:`read_spec`).

    Raises:
        ValueError: *raw* is not a mapping.
        BenchmarkSelectionError: *raw* writes a ``benchmark:`` section itself —
            a benchmark is selected with ``--benchmark``, never written into
            the configuration.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("The configuration must be a YAML mapping")
    if BENCHMARK_SECTION in raw:
        raise BenchmarkSelectionError(
            f"The configuration writes a '{BENCHMARK_SECTION}:' section, and a run configuration "
            f"never holds a benchmark: select it with --benchmark <alias|path.yaml> (shipped "
            f"aliases: {', '.join(aliases()) or 'none'})"
        )
    root = dict(raw)
    if benchmarks:
        root[BENCHMARK_SECTION] = list(benchmarks)
    return root


__all__ = [
    "ALIAS_DIR",
    "BENCHMARK_SECTION",
    "BenchmarkSelectionError",
    "aliases",
    "is_path",
    "read_benchmark",
    "read_spec",
    "run_root",
    "spec_of",
]
