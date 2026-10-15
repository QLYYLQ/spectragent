"""Declared recording scope and observed coverage, independent of execution outcome."""

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
    channel: str
    phase: str
    error: str
    detail: str
    occurred_at: float
    recovered: bool = False


class UsageCoverage(BaseModel):
    """Actual calls measured, distinct from provider usage availability."""

    calls: int = 0
    reported_calls: int = 0
    missing_call_ids: list[str] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.calls == self.reported_calls


class RecordingHealth(BaseModel):
    """Current persisted coverage, separate from the Agent's result."""

    status: str = "writing"
    expected_steps: int = 0
    committed_steps: int = 0
    complete_through: int = -1
    missing_steps: list[int] = Field(default_factory=list)
    problems: list[RecordingProblem] = Field(default_factory=list)
