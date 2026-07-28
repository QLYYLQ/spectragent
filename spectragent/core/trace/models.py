"""Trace data models — manifest, config snapshot, and run status.

These Pydantic models define the on-disk format for WAL persistence.
The manifest is the atomic commit point; config is a frozen snapshot
of the run's configuration.

See docs/execution_dag.md M10 for design details.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas._types import UTCDatetime


class RunStatus(StrEnum):
    """Status of a trace run."""

    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class StepManifestEntry(BaseModel):
    """Per-step metadata pointing to file paths within the trace directory."""

    model_config = ConfigDict(extra="allow")

    step: int
    step_file: str  # relative path, e.g. "steps/000.json"
    observation_files: list[str] = Field(default_factory=list)
    # Canonical (first) LLM call of the step — kept as scalars so simple
    # readers stay simple.
    message_input_file: str | None = None
    message_output_file: str | None = None
    # Every LLM call of the step, in order. A loop that reflects or
    # retries issues more than one call per step; those extra calls used
    # to overwrite each other on disk.
    message_files: list[str] = Field(default_factory=list)


class TraceManifest(BaseModel):
    """Atomic commit point for WAL persistence.

    Written via write-then-rename to guarantee atomicity.
    Recovery reads this to determine the last complete step.
    """

    model_config = ConfigDict(extra="allow")

    run_id: str
    last_step_completed: int = -1  # -1 means no step completed yet
    status: RunStatus = RunStatus.RUNNING
    created_at: UTCDatetime
    updated_at: UTCDatetime
    config_hash: str = ""
    steps: list[StepManifestEntry] = Field(default_factory=list)
    # LLM calls that belong to a step the Loop never surrendered — a run
    # killed by the timeout or an exception between the completion and
    # ``record_step``. They are not a completed step, so they get no
    # ``StepManifestEntry``; they are listed here so the last exchange
    # before the run died is still findable.
    unfinished_message_files: list[str] = Field(default_factory=list)


class TraceConfig(BaseModel):
    """Frozen configuration snapshot for a run.

    Captures enough context to reproduce or understand the run — and,
    critically, enough to *join* the trace back to the dataset it came
    from. ``(benchmark, subset, task_id)`` is the key an Evaluator needs
    to look up ground truth:
    ``EvalLoaderRegistrar.get(benchmark)(subset=subset)[task_id]``.
    Without all three, a trace directory is an orphan — you cannot even
    say which task it ran.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str = ""
    loop_id: str = ""
    env_type: str = ""
    llm_model: str = ""
    benchmark: str = ""
    subset: str = ""
    task: str = ""
    task_id: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)
