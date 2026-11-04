"""The ``lexbench_browser`` benchmark (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from spectragent.benchmarks._registrar import BenchmarkBase
from spectragent.benchmarks.lexbench_browser.data import LexBenchBrowserData


class LexBenchBrowserBenchmark(BenchmarkBase):
    """LexBench-Browser's public tasks, on Chinese and international sites."""

    __registry_key__ = "lexbench_browser"
    __primary_verifier__ = "lexbench_browser"
    __wiring__: ClassVar[dict[str, Any]] = {
        "task_loader": ("task_loader", ["lexbench_browser"]),
        "eval_loader": ("eval_loader", ["lexbench_browser"]),
        "verifiers": ("verifier", ["lexbench_browser"]),
    }

    @classmethod
    def pinned_data(cls, config: BaseModel) -> LexBenchBrowserData:
        """The public dataset at the pinned commit (``data.py``), whatever the block configures."""
        return LexBenchBrowserData()
