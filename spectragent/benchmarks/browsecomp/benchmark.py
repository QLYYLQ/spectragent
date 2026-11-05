"""The ``browsecomp`` benchmark (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from spectragent.benchmarks._registrar import BenchmarkBase
from spectragent.benchmarks.browsecomp.data import BrowseCompData


class BrowseCompBenchmark(BenchmarkBase):
    """BrowseComp: questions answered by browsing, with encrypted reference answers."""

    __registry_key__ = "browsecomp"
    __primary_verifier__ = "browsecomp"
    __wiring__: ClassVar[dict[str, Any]] = {
        "task_loader": ("task_loader", ["browsecomp"]),
        "eval_loader": ("eval_loader", ["browsecomp"]),
        # Its grader, and exact matching for a short answer the reference spells out.
        "verifiers": ("verifier", ["browsecomp", "exact_match"]),
    }

    @classmethod
    def pinned_data(cls, config: BaseModel) -> BrowseCompData:
        """Its tasks at the pinned version (``data.py``), whatever the block configures."""
        return BrowseCompData()
