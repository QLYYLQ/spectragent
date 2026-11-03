"""``--benchmark <alias|path.yaml>``: which benchmark a run uses (``docs/architecture_design.md`` §4.10.1).

A benchmark YAML is one more document of the run root, holding only a
``benchmark:`` section with one block. An alias names a shipped one,
``configs/benchmarks/<alias>.yaml``; a path names the user's own. The selection
is a path when it ends in ``.yaml`` / ``.yml`` or holds a path separator, and
an alias otherwise. The run configuration never holds a benchmark: a
``benchmark:`` section in it is refused when the run is composed
(:func:`run_root`), whether or not ``--benchmark`` was given.

``--subset`` selects which of the benchmark's tasks run, and is the one knob
the command line sets on a benchmark: it is written into the selected block's
task loader before the run is composed (:func:`read_benchmark`), so the
resolved configuration and every run's identity record it, as if the YAML had
said it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from spectragent import REPO_ROOT

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
            raise BenchmarkSelectionError(
                f"--benchmark {selection}: no shipped benchmark has this alias (aliases: "
                f"{', '.join(aliases()) or 'none'}, in {ALIAS_DIR}); a benchmark YAML of your "
                f"own is named by its path"
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


def run_root(raw: object, *benchmarks: dict[str, Any]) -> dict[str, Any]:
    """The run root a run composes: *raw*'s sections, and the *benchmarks* blocks under ``benchmark:``.

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
    "run_root",
]
