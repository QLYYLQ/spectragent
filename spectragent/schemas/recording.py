"""Declared recording scope and observed coverage, independent of execution outcome."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RecordingPlan(BaseModel):
    """Select facts before capture; every selected channel is persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    metrics: bool = True
    errors: bool = True
    trajectory: bool = True
    observations: bool = True


class RecordingProblem(BaseModel):
    """A historical recorder failure; recovery does not erase that it happened."""

    step: int | None = None
    segment_id: str | None = None
    call_ids: list[str] = Field(default_factory=list)
    channel: str
    phase: str
    error: str
    detail: str
    occurred_at: float
    recovered: bool = False


class UsageCoverage(BaseModel):
    """Actual calls measured, distinct from provider usage availability."""

    calls: int = Field(default=0, ge=0)
    reported_calls: int = Field(default=0, ge=0)
    missing_call_ids: list[str] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.calls == self.reported_calls


class RecordingHealth(BaseModel):
    """Current persisted coverage, separate from the Agent's result."""

    status: Literal["writing", "complete", "incomplete"] = "writing"
    expected_steps: int = Field(default=0, ge=0)
    committed_steps: int = Field(default=0, ge=0)
    complete_through: int = Field(default=-1, ge=-1)
    missing_steps: list[int] = Field(default_factory=list)
    problems: list[RecordingProblem] = Field(default_factory=list)


class RecordingTiming(BaseModel):
    """Measured recording overhead attributed after the step's immutable commit."""

    step: int = Field(ge=0)
    recording_ms: float = Field(ge=0)


class ExecutionError(BaseModel):
    """An observed execution failure; no guessed exception type or action cause."""

    phase: str
    detail: str
    occurred_at: float
    error_type: str | None = None
    action_index: int | None = None
    action_name: str | None = None
    call_id: str | None = None
