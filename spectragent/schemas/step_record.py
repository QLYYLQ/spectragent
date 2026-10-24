"""Step record data contracts — per-step execution trace."""

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation.geometry import PointFrame
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
        """Build from a provider ``ModelInvokeUsage``, or ``None`` if unmeasured.

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


class ActionSpan(BaseModel):
    """When one ``env.act`` call of a step ran, measured by the recording component.

    Wall clock (``time.time()``) on purpose: it joins with the environment
    evidence's own capture times (a network record's ``started_at``, every
    ``ArchiveProvenance.captured_at``), so an offline reader can tell which
    action of a multi-action step a piece of evidence followed.
    """

    # The name ``env.act`` received: fully qualified, as ``guarded_act`` left it.
    action: str
    # Immediately before ``env.act`` proceeded.
    started_at: float
    # When ``env.act`` returned, raised or was cancelled.
    ended_at: float
    outcome: Literal["returned", "raised", "cancelled"]


class StepRecord(BaseModel):
    """Single step in an agent's execution trace.

    This is both the WAL's on-disk step format and the payload of
    ``HistoryObs`` — so it is rendered into prompts. Keep it small and
    keep bulky artifacts (observations, screenshots) out of it.
    """

    step: int
    observation_summary: str = ""
    llm_output: str = ""
    # The frame this step's points were written in: the agent's grid over the
    # screenshot the step showed (``docs/architecture_design.md`` §4.1
    # "坐标系归框架"). ``llm_output`` holds points on it; ``actions`` and every
    # effect hold the capability's units. Set by the Loop on a step the agent
    # decided (and on a restored step whose execution it was shown), so the
    # next step's feedback, a branch and the training export write this step's
    # points on it without asking the agent. ``None``: the run's points are
    # the capability's own units (no grid declared, or no point action), or
    # the step shows the model no point (a terminal observation, a step that
    # failed before its prompt).
    frame: PointFrame | None = None
    # Restored steps contain exact action/returned-result pairs here. Attempts
    # that raised without returning remain in restoration.actions, including
    # their original order and exception; never shift a later result backward.
    # The one addition is a skipped tail (``skipped_actions``): when the source
    # step skipped actions after a failed one and the restoration ran all the
    # source ran and failed at the same action, those actions follow, as
    # recorded.
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
    # How many of ``actions``, at its end, the Loop did not execute because
    # the action before them failed: the last of ``action_results`` has
    # ``success`` False, and the skipped actions after it never ran and have
    # no result (``docs/architecture_design.md`` §4.1, the user's ruling of
    # 2026-10-24). They stay in ``actions`` because they are part of the reply
    # the step accepted: a branch replays that reply and the training export
    # writes it. 0: nothing was skipped. A step recorded before that ruling
    # always reads 0, truthfully: that Loop ran every action after a failed
    # one, so ``action_results`` holds a result for each of them.
    skipped_actions: int = Field(default=0, ge=0)
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
    # One span per ``env.act`` call the step made, in call order, restored
    # steps included; an action that raised or was cancelled has one too, so
    # this can be longer than ``action_results``. ``[]``: the step made no
    # ``env.act`` call (a terminal observation, a step that only parsed, an
    # env-less run). ``None``: not measured, under the same rule as
    # ``duration_ms`` and filled by the same component. Never rendered.
    action_spans: list[ActionSpan] | None = None
    # Where the step came from. ``policy``: this run's agent decided it.
    # ``restored``: actual restored execution, with returned action/result
    # pairs here and all attempts in ``restoration``. Old replies and source
    # actions remain labelled in ``source_history``. ``terminal``: observation after the run
    # ended (no LLM call, no action). Only ``policy`` steps count towards
    # ``AgentMetrics.steps`` and ``max_steps``.
    origin: Literal["policy", "restored", "terminal"] = "policy"
    # The restorer's check of this step: on a restored step its own, on a
    # branch's checkpoint step (the first policy step) the check of the
    # restored observation it decided on. ``None`` everywhere else.
    # Neither field is rendered into a prompt: agents render a history
    # item's actions, results and reply only.
    restoration: RestorationCheck | None = None

    @model_validator(mode="after")
    def _skipped_actions_follow_a_failed_result(self) -> "StepRecord":
        """Skipped actions are exactly the actions after the last result, and it failed."""
        if self.skipped_actions == 0:
            return self
        if not self.action_results or self.action_results[-1].success:
            raise ValueError(
                f"step {self.step}: {self.skipped_actions} skipped action(s) need a failed "
                "action before them, and the step's last result is not a failure"
            )
        if len(self.actions) != len(self.action_results) + self.skipped_actions:
            raise ValueError(
                f"step {self.step}: {len(self.actions)} action(s), "
                f"{len(self.action_results)} result(s) and {self.skipped_actions} skipped "
                "action(s): the skipped ones must be exactly the actions after the last result"
            )
        return self

    def skipped(self) -> list[Action]:
        """The actions the Loop did not execute because the action before them failed, in order."""
        return (
            self.actions[len(self.actions) - self.skipped_actions :] if self.skipped_actions else []
        )
