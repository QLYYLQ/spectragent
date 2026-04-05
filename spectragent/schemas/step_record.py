"""Step record data contracts — per-step execution trace."""

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas.action import Action, ActionResult


class TokenUsage(BaseModel):
    """Token usage for a single step. Allows provider-specific extra fields."""

    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class StepRecord(BaseModel):
    """Single step in an agent's execution trace."""

    step: int
    observation_summary: str = ""
    llm_output: str = ""
    actions: list[Action] = Field(default_factory=list)
    action_results: list[ActionResult] = Field(default_factory=list)
    timestamp: float = 0.0
    token_usage: TokenUsage | None = None
