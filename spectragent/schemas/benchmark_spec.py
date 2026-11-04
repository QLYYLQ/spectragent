"""How a run's benchmark was selected (``docs/architecture_design.md`` §5.3 "实验身份")."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkSpec(BaseModel):
    """How a run's benchmark was selected: its id, the ``--benchmark`` selection and ``--subset``.

    Recorded as ``TraceConfig.benchmark_spec`` by every run of a benchmark's
    task (``spectragent bench`` and ``spectragent run --task-id``). A branch run
    records its source run's, copied from the source trace: a branch sweep is a
    task source, not a benchmark, and each branch task belongs to its source
    run's benchmark, which it finds again by this record
    (``docs/architecture_design.md`` §4.10.1 "分叉 sweep 是一种任务来源").

    What the benchmark block held (its task loader, its setup component) is in
    the run's ``resolved``; this says only where the block came from, so it is
    not part of the configuration digest, as the configuration's source file
    is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark_id: str = Field(min_length=1, description="The selected benchmark's benchmark_id.")
    selection: str = Field(
        min_length=1,
        description=(
            "How it was selected: a shipped alias (configs/benchmarks/<alias>.yaml), or the "
            "absolute path of the benchmark YAML."
        ),
    )
    subset: str | None = Field(
        default=None,
        description=(
            "The --subset written into its task loader before the run was composed; null: the "
            "benchmark YAML's own."
        ),
    )

    def describe(self) -> str:
        """The command-line selection this record says: ``--benchmark X [--subset Y]``."""
        subset = f" --subset {self.subset}" if self.subset is not None else ""
        return f"--benchmark {self.selection}{subset}"


__all__ = ["BenchmarkSpec"]
