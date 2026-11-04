"""The ``online_mind2web`` benchmark (``docs/architecture_design.md`` §4.10.1)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from spectragent.benchmarks._registrar import BenchmarkBase
from spectragent.benchmarks.online_mind2web.data import OnlineMind2WebData


class OnlineMind2WebBenchmark(BenchmarkBase):
    """Online-Mind2Web: live web tasks, judged by WebJudge on each run's trace."""

    __registry_key__ = "online_mind2web"
    __primary_verifier__ = "online_mind2web"
    __wiring__: ClassVar[dict[str, Any]] = {
        "task_loader": ("task_loader", ["online_mind2web"]),
        "eval_loader": ("eval_loader", ["online_mind2web"]),
        "verifiers": ("verifier", ["online_mind2web"]),
    }

    @classmethod
    def pinned_data(cls, config: BaseModel) -> OnlineMind2WebData:
        """Its tasks at the pinned version (``data.py``), whatever the block configures."""
        return OnlineMind2WebData()
