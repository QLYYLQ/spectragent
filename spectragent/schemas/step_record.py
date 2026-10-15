"""Step record data contracts — per-step execution trace."""

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.prompt import PromptContribution, RestoredHistory
from spectragent.schemas.recording import ExecutionError, UsageCoverage
from spectragent.schemas.restoration import RestorationCheck


class TokenUsage(BaseModel):
    """Token usage for a single step. Allows provider-specific extra fields."""

    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_cached_tokens: int = 0
    prompt_cache_creation_tokens: int = 0
    prompt_image_tokens: int = 0

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
        reported_total = getattr(usage, "total_tokens", None)
        total = prompt + completion if reported_total is None else int(reported_total)
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            prompt_cached_tokens=int(getattr(usage, "prompt_cached_tokens", 0) or 0),
            prompt_cache_creation_tokens=int(
                getattr(usage, "prompt_cache_creation_tokens", 0) or 0
            ),
            prompt_image_tokens=int(getattr(usage, "prompt_image_tokens", 0) or 0),
        )

    @classmethod
    def combine(cls, usages: Iterable[Any]) -> "TokenUsage":
        """Known subtotal only; callers separately retain missing-call coverage."""
        total = cls()
        for usage in usages:
            item = cls.from_llm_usage(usage)
            if item is not None:
                for name in cls.model_fields:
                    setattr(total, name, getattr(total, name) + getattr(item, name))
        return total


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
    prompt_contributions: list[PromptContribution] = Field(default_factory=list)
    source_history: RestoredHistory | None = None
    action_count: int | None = None
    proposed_action_count: int | None = None
    token_usage: TokenUsage | None = None
    usage_coverage: UsageCoverage | None = None
    recording_ms: float | None = None
    # Number of actions the LLM emitted that the loop discarded because
    # they exceeded ``max_actions_per_step``. Non-zero means the model
    # believes it executed steps that never ran.
    truncated_actions: int = 0
    # Which phase failed, when a step ended in an exception ("observe",
    # "llm", "act"). Empty on healthy steps.
    error_phase: str = ""
    error: str | None = None
    execution_errors: list[ExecutionError] = Field(default_factory=list)
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
    # Public execution-step entry to surrender, excluding capture work
    # attributed to this step. It does not include setup or prior commits.
    duration_ms: int | None = None
    # Of that, the part spent waiting on the model — summed across every
    # LLM call the step made, including calls that raised (a refused
    # structured reply costs real seconds and the loop retries past it).
    llm_latency_ms: int | None = None
    # Where the step came from. ``policy``: the run's own agent decided it
    # (every step of every trace written before this field existed reads
    # back as ``policy``, which is right for them). ``restored``: a branch
    # run's re-executed prefix — a copy of the source run's step, verified
    # by ``restoration``. ``terminal``: the observation taken after the run
    # ended (no LLM call, no action). Only ``policy`` steps count towards
    # ``AgentMetrics.steps`` and ``max_steps``.
    origin: Literal["policy", "restored", "terminal"] = "policy"
    # The restorer's check of this step: on a restored step its own, on a
    # branch's checkpoint step (the first policy step) the check of the
    # restored observation it decided on. ``None`` everywhere else.
    # Neither field is rendered into a prompt: agents render a history
    # item's actions, results and reply only.
    restoration: RestorationCheck | None = None
