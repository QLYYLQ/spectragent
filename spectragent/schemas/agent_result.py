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


class AgentUsage(BaseModel):
    """Token usage statistics from the agent's LLM calls.

    ``AgentMetrics.usage is None`` is the "not measured" sentinel. Never
    substitute a zero-filled ``AgentUsage()`` for it: that makes a run
    nobody instrumented indistinguishable from a run that genuinely
    consumed zero tokens, and every downstream ``if usage is not None``
    guard passes while reading zeros.
    """

    model_config = ConfigDict(extra="allow")

    # Token counts
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_prompt_cached_tokens: int = 0
    total_tokens: int = 0

    # Cost breakdown
    total_prompt_cost: float = 0.0
    total_prompt_cached_cost: float = 0.0
    total_completion_cost: float = 0.0
    total_cost: float = 0.0

    # Invocation info
    entry_count: int = 0
    by_model: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _auto_fill_total_tokens(self) -> "AgentUsage":
        if self.total_tokens == 0 and (self.total_prompt_tokens + self.total_completion_tokens) > 0:
            self.total_tokens = self.total_prompt_tokens + self.total_completion_tokens
        return self

    def add_llm_usage(self, usage: Any, *, model: str = "") -> None:
        """Accumulate one LLM call's usage in place.

        *usage* is duck-typed against ``ChatInvokeUsage`` (``prompt_tokens``,
        ``completion_tokens``, ``prompt_cached_tokens``, ``total_tokens``)
        so providers that report a subset — or a different object entirely
        — still contribute what they have. ``None`` is a no-op, which is
        how a provider that reports no usage leaves ``entry_count`` behind
        as evidence that a call happened.
        """
        self.entry_count += 1
        if usage is None:
            return

        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = int(getattr(usage, "prompt_cached_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or 0) or (prompt + completion)

        self.total_prompt_tokens += prompt
        self.total_completion_tokens += completion
        self.total_prompt_cached_tokens += cached
        self.total_tokens += total

        if not model:
            return
        by_model = self.by_model if self.by_model is not None else {}
        entry = by_model.setdefault(
            model,
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
            },
        )
        entry["prompt_tokens"] += prompt
        entry["completion_tokens"] += completion
        entry["cached_tokens"] += cached
        entry["total_tokens"] += total
        entry["calls"] += 1
        self.by_model = by_model


class AgentMetrics(BaseModel):
    """Performance metrics from agent execution.

    ``steps`` and ``llm_calls`` are deliberately separate quantities: a
    loop that reflects, retries or self-consistency-samples issues many
    LLM calls per step, and conflating them silently corrupts every
    per-step average computed from a trace.
    """

    model_config = ConfigDict(extra="allow")

    ttft_ms: int | None = None
    end_to_end_ms: int
    steps: int
    llm_calls: int | None = None
    usage: AgentUsage | None = None


class AgentResult(BaseModel):
    """Standardized result from any agent execution.

    Top-level fields use ``extra="forbid"`` to catch typos and stale keys.
    Agent-specific opaque data should go into ``agent_metadata``.
    """

    model_config = ConfigDict(extra="forbid")

    # Version
    # 2.1 renamed ``agent_success`` -> ``agent_reported_success``. Traces
    # written before that carry 2.0 and the old key; they are legacy.
    schema_version: Literal["1.0", "1.1", "2.0", "2.1"] = "2.1"

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
        if self.env_status == EnvironmentStatus.SUCCESS and self.agent_done == AgentDoneStatus.DONE and self.error is not None:
            raise ValueError("env_status is 'success' and agent_done is 'done' but error is not None")
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
