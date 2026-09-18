"""Step record data contracts — per-step execution trace."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas.action import Action, ActionResult


class TokenUsage(BaseModel):
    """Token usage for a single step. Allows provider-specific extra fields."""

    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_llm_usage(cls, usage: Any) -> "TokenUsage | None":
        """Build from a provider ``ChatInvokeUsage``, or ``None`` if unmeasured.

        Duck-typed rather than imported so ``schemas/`` stays independent
        of ``core/llm/``. Returning ``None`` for a ``None`` input keeps the
        "not measured" sentinel intact all the way down to the step level.
        """
        if usage is None:
            return None
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or 0) or (prompt + completion)
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
        )


class StepRecord(BaseModel):
    """Single step in an agent's execution trace.

    This is both the WAL's on-disk step format and the payload of
    ``HistoryObs`` — so it is rendered into prompts. Keep it small and
    keep bulky artifacts (observations, screenshots) out of it.
    """

    step: int
    observation_summary: str = ""
    llm_output: str = ""
    actions: list[Action] = Field(default_factory=list)
    action_results: list[ActionResult] = Field(default_factory=list)
    timestamp: float = 0.0
    token_usage: TokenUsage | None = None
    # Number of actions the LLM emitted that the loop discarded because
    # they exceeded ``max_actions_per_step``. Non-zero means the model
    # believes it executed steps that never ran.
    truncated_actions: int = 0
    # Which phase failed, when a step ended in an exception ("observe",
    # "llm", "act"). Empty on healthy steps.
    error_phase: str = ""
    error: str | None = None
    # Wall clock, in milliseconds. ``None`` means *not measured*, the same
    # honest sentinel ``token_usage`` uses — a zero here would claim a
    # step that took no time.
    #
    # Filled by the persistence hook from AOP ``around`` advice, never by
    # the Loop: a Loop that timed itself would be the one component the
    # AOP layer exists to keep free of cross-cutting concerns, and the
    # timing would then be missing from every Loop nobody remembered to
    # instrument.
    #
    # ``duration_ms`` partitions the run's wall clock across its steps —
    # it is measured from the previous step's commit, so step 0 spans
    # from run start and therefore *includes environment setup* (browser
    # launch, container pull). That is the honest reading: it is what
    # producing step 0 actually cost.
    duration_ms: int | None = None
    # Of that, the part spent waiting on the model — summed across every
    # LLM call the step made, including calls that raised (a refused
    # structured reply costs real seconds and the loop retries past it).
    llm_latency_ms: int | None = None
