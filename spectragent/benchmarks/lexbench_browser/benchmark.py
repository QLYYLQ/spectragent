"""The ``lexbench_browser`` benchmark (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

from typing import Any, ClassVar

from spectragent.benchmarks._registrar import BenchmarkBase


class LexBenchBrowserBenchmark(BenchmarkBase):
    """LexBench's browser tasks, on Chinese and international sites."""

    __registry_key__ = "lexbench_browser"
    __primary_verifier__ = "lexbench_browser"
    __wiring__: ClassVar[dict[str, Any]] = {
        "task_loader": ("task_loader", ["lexbench_browser"]),
        "eval_loader": ("eval_loader", ["lexbench_browser"]),
        "verifiers": ("verifier", ["lexbench_browser"]),
    }
