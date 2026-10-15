"""Agent result schema — standardized output for all agents."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.schemas._types import (
    AgentDoneStatus,
    EnvironmentStatus,
    NormalizedAgentDoneStatus,
    NormalizedEnvironmentStatus,
    UTCDatetime,
)
from spectragent.schemas.prompt import PromptSnapshot
from spectragent.schemas.recording import RecordingHealth, UsageCoverage
from spectragent.schemas.step_record import TokenUsage


class AgentUsage(BaseModel):
    """Token usage statistics from the agent's LLM calls.

    ``AgentMetrics.usage is None`` is the "not measured" sentinel. Never
    substitute a zero-filled ``AgentUsage()`` for it: that makes a run
    nobody instrumented indistinguishable from a run that genuinely
    consumed zero tokens, and every downstream ``if usage is not None``
    guard passes while reading zeros.

    **Tokens, never money.** This class carried four ``*_cost: float`` fields
    that no code ever wrote, so every trace ever recorded claimed the run cost
    $0.00 — the exact confusion the paragraph above exists to prevent, one
    level down, and worse for being a plausible number rather than a missing
    one. Pricing is deliberately out of scope: a rate depends on the model id,
    the service tier, the context-length bracket, the billing mode and (on at
    least one provider) the wall-clock hour, none of which a run can observe
    through a gateway that load-balances across upstreams. Record what the
    provider reported and let whoever holds the invoice do the arithmetic.
    """

    model_config = ConfigDict(extra="allow")

    # Token counts. ``total_prompt_tokens`` is the whole prompt: the two
    # fields below are *breakdowns of it*, not additions to it, so summing all
    # three double-counts. They are kept separate because the three are billed
    # at different rates by every provider that caches at all.
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_prompt_cached_tokens: int = 0
    total_prompt_cache_creation_tokens: int = 0
    total_prompt_image_tokens: int = 0
    total_tokens: int = 0

    # Invocation info
    entry_count: int = 0
    by_model: dict[str, Any] | None = None
    by_purpose: dict[str, Any] | None = None

    def add_llm_usage(
        self, usage: Any, *, model: str = "", purpose: str = "agent_decision"
    ) -> None:
        """Accumulate provider facts using the same rule as step and offline totals."""
        self.entry_count += 1
        item = TokenUsage.from_llm_usage(usage)
        if item is not None:
            self.total_prompt_tokens += item.prompt_tokens
            self.total_completion_tokens += item.completion_tokens
            self.total_prompt_cached_tokens += item.prompt_cached_tokens
            self.total_prompt_cache_creation_tokens += item.prompt_cache_creation_tokens
            self.total_prompt_image_tokens += item.prompt_image_tokens
            self.total_tokens += item.total_tokens
        for field, key in (("by_model", model), ("by_purpose", purpose)):
            if not key:
                continue
            groups = getattr(self, field) or {}
            entry = groups.setdefault(
                key,
                {
                    "calls": 0,
                    "reported_calls": 0,
                    "total_tokens": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "cache_creation_tokens": 0,
                    "image_tokens": 0,
                },
            )
            entry["calls"] += 1
            if item is not None:
                entry["reported_calls"] += 1
                entry["total_tokens"] += item.total_tokens
                entry["prompt_tokens"] += item.prompt_tokens
                entry["completion_tokens"] += item.completion_tokens
                entry["cached_tokens"] += item.prompt_cached_tokens
                entry["cache_creation_tokens"] += item.prompt_cache_creation_tokens
                entry["image_tokens"] += item.prompt_image_tokens
            setattr(self, field, groups)


class AgentMetrics(BaseModel):
    """Performance metrics from agent execution.

    ``steps`` and ``llm_calls`` are deliberately separate quantities: a
    loop that reflects, retries or self-consistency-samples issues many
    LLM calls per step, and conflating them silently corrupts every
    per-step average computed from a trace.
    """

    model_config = ConfigDict(extra="forbid")

    ttft_ms: int | None = None
    execution_ms: int | None = None
    steps: int
    llm_calls: int | None = None
    usage: AgentUsage | None = None
    usage_coverage: UsageCoverage | None = None
    recording_health: RecordingHealth | None = None


class AgentResult(BaseModel):
    """Standardized result from any agent execution.

    Top-level fields use ``extra="forbid"`` to catch typos and stale keys.
    Agent-specific opaque data should go into ``agent_metadata``.
    """

    model_config = ConfigDict(extra="forbid")

    # Only the current execution/recording contract is readable. Version 3
    # separates execution_ms, provider coverage and recording health.
    schema_version: Literal["3.0"] = "3.0"

    # Identity
    task_id: str
    task: str = ""

    # Timestamp
    timestamp: UTCDatetime

    # Execution result
    # env_status: whether the environment/browser is working properly
    env_status: NormalizedEnvironmentStatus
    # agent_done: how the agent finished the task
    agent_done: NormalizedAgentDoneStatus
    # agent_reported_success: the agent's *self-report*, read verbatim out of
    #   ``done(success=...)``. It is NOT a verdict — some agents hardcode
    #   ``True`` — and no scorer should treat it as one. The verdict belongs
    #   to ``EvalResult.predicted_label``, produced by an Evaluator from the
    #   recorded trace.
    #   True  = agent called done(success=true)
    #   False = agent called done(success=false)
    #   None  = agent never called done (timeout, max_steps, error)
    agent_reported_success: bool | None = None
    # answer: the agent's best final output. Valid on *every* termination
    #   path — an agent that produced a correct patch and then ran out of
    #   steps must not lose the patch just because it never called done().
    answer: str = ""
    error: str | None = None

    # Context
    model_id: str = ""
    browser_id: str = ""

    # Trajectory
    action_history: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)

    # Performance
    metrics: AgentMetrics

    # Agent-specific opaque data. Not part of the scoring contract: an
    # Evaluator reads the trace, not this dict.
    agent_metadata: dict[str, Any] = Field(default_factory=dict)

    # Prompt snapshot
    system_prompt: PromptSnapshot | None = None

    # Configuration snapshot
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_result_consistency(self) -> "AgentResult":
        if (
            self.env_status == EnvironmentStatus.SUCCESS
            and self.agent_done == AgentDoneStatus.DONE
            and self.error is not None
        ):
            raise ValueError(
                "env_status is 'success' and agent_done is 'done' but error is not None"
            )
        # A self-report can only exist if the agent actually called done().
        # This constrains the *self-report*, never the ``answer`` — which is
        # exactly why the verdict lives in EvalResult and not here.
        if self.agent_reported_success is not None and self.agent_done != AgentDoneStatus.DONE:
            raise ValueError(
                f"agent_reported_success must be None when agent_done is "
                f"'{self.agent_done}' (the agent never called done); "
                f"got {self.agent_reported_success}. Record the outcome in "
                f"'answer' and let an Evaluator produce the verdict."
            )
        return self
