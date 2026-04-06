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
    message_input_file: str | None = None
    message_output_file: str | None = None


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


class TraceConfig(BaseModel):
    """Frozen configuration snapshot for a run.

    Captures enough context to reproduce or understand the run.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str = ""
    loop_id: str = ""
    env_type: str = ""
    llm_model: str = ""
    task: str = ""
    task_id: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)
