"""The ``browsecomp`` benchmark (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

from typing import Any, ClassVar

from spectragent.benchmarks._registrar import BenchmarkBase


class BrowseCompBenchmark(BenchmarkBase):
    """BrowseComp: questions answered by browsing, with encrypted reference answers."""

    __registry_key__ = "browsecomp"
    __wiring__: ClassVar[dict[str, Any]] = {
        "task_loader": ("task_loader", ["browsecomp"]),
        "eval_loader": ("eval_loader", ["browsecomp"]),
        # Its grader, and exact matching for a short answer the reference spells out.
        "verifiers": ("verifier", ["browsecomp", "exact_match"]),
    }
