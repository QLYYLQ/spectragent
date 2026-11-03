"""The branch sweep's task source (``docs/architecture_design.md`` §4.10.1 "分叉 sweep 是一种任务来源").

Not a benchmark of its own: every branch task is a task of its source run's
benchmark, whose benchmark, subset and task id it carries
(``BranchProvenance``), so its restored start, its replayed prefix and how it
is scored are its source benchmark's. A branches YAML holds only this block:
the ``trace_branches`` task loader and its knobs (which recorded runs, which
steps, how many branches per step). The branch Loop, the restorer and the hooks
that change what the agent sees stay in the run configuration.
"""

from __future__ import annotations

from typing import Any, ClassVar

from spectragent.benchmarks._registrar import BenchmarkBase


class TraceBranches(BenchmarkBase):
    """Branch tasks from recorded runs (the ``trace_branches`` task loader, §4.10).

    It never takes a part of a capability's configuration of its own: a branch
    task's are its source benchmark's (§4.10.1).
    """

    __registry_key__ = "trace_branches"
    __wiring__: ClassVar[dict[str, Any]] = {
        "task_loader": ("task_loader", ["trace_branches"]),
    }

    def __init__(self) -> None:
        super().__init__()
