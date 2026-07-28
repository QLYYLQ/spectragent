"""Evaluation result schema — standardized output for all benchmark evaluations."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas._types import UTCDatetime
from spectragent.schemas.agent_result import AgentMetrics
from spectragent.schemas.prompt import PromptSnapshot

#: Where a verdict came from.
#:
#: ``graded`` is the only value that means an evaluator actually read the
#: trace and judged it. The other two are **synthetic** records written by
#: the coverage policy so that a task with no verdict still occupies the
#: denominator — and they are separate values, not one, because their
#: causes are unrelated:
#:
#: * ``not_evaluated`` — the *agent* produced no scorable trace. Counting
#:   it as a failure is a real statement about the agent.
#: * ``evaluation_error`` — the *evaluator* failed: the judge API was
#:   unreachable, the trace could not be read, the reference set
#:   disagreed. This says nothing whatsoever about the agent, and the
#:   ``predicted_label=0`` attached to it is a placeholder, not a
#:   measurement. Re-run the scoring pass before quoting the number.
#:
#: Collapsing the two — which is what the ported BrowseComp grader did
#: when it returned ``"correct: no"`` on retry exhaustion — turns an API
#: outage into a benchmark result.
VerdictSource = Literal["graded", "not_evaluated", "evaluation_error"]


class EvalUsage(BaseModel):
    """Token usage from the evaluation LLM call."""

    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    non_cached_prompt: int = 0
    costs: dict[str, float] | None = None


class EvalDetails(BaseModel):
    """Evaluation details.

    ``agent_metrics`` is stored as a nested object — never flat-merged — which
    fixes the ``usage`` key-override bug that occurred with ``dict.update()``.
    """

    model_config = ConfigDict(extra="allow")

    # Evaluator prompt snapshots (v1.1 — typed discriminated union)
    system_prompt: PromptSnapshot | None = None
    user_prompt: PromptSnapshot | None = None
    additional_prompts: dict[str, PromptSnapshot] | None = None

    # Evaluator output
    response: str = ""
    score: int | None = None
    is_correct: bool | None = None
    reasoning: str | None = None
    eval_usage: EvalUsage | None = None

    # Agent metrics (nested, NOT flat-merged)
    agent_metrics: AgentMetrics | None = None

    # Benchmark-specific fields
    benchmark_details: dict[str, Any] = Field(default_factory=dict)


class FailureClassification(BaseModel):
    """Failure analysis classification."""

    category: str
    reasoning: str = ""
    raw_response: str | None = None


class AgentResultRef(BaseModel):
    """Reference back to the original AgentResult for traceability."""

    task_id: str
    timestamp: UTCDatetime
    result_dir: str
    model_id: str = ""
    browser_id: str = ""


class EvalResult(BaseModel):
    """Standardized evaluation result — the framework's only verdict.

    ``AgentResult.agent_reported_success`` is the agent's *self-report*
    and some agents hardcode it to ``True``. The verdict lives here, is
    produced by a registered Evaluator from a recorded ``RunTrace``, and
    carries enough identity — which benchmark, which subset, which run,
    which evaluator — to survive being one file among thousands.

    Top-level fields use ``extra="forbid"`` to catch unexpected keys.
    """

    model_config = ConfigDict(extra="forbid")

    # Version
    # 2.0 added (evaluator_id, benchmark, subset, run_id): before the
    # EvaluatorRegistrar existed there was exactly one grader per
    # benchmark, invoked by a hand-run script, so "which evaluator
    # produced this?" had a single implicit answer. It no longer does.
    # 2.1 added (verdict_source, evaluator_config). Both close gaps that
    # made a stored verdict unreadable on its own: a 2.0 record cannot
    # say whether its predicted_label was measured or manufactured, and
    # cannot say which configuration of the named evaluator produced it.
    # Reading a 2.0 record back defaults verdict_source to "graded",
    # which is why this is a version bump and not a silent field add.
    schema_version: Literal["1.0", "1.1", "2.0", "2.1"] = "2.1"

    # Identity
    task_id: str
    task: str = ""

    # Provenance — which grader, over which dataset slice, of which run.
    evaluator_id: str = ""
    benchmark: str = ""
    subset: str = ""
    run_id: str = ""

    # Which *configuration* of that grader. ``evaluator_id`` alone is not
    # identity: exact_match with allow_substring on and off are different
    # scorers under one name, and an LLM judge is a different scorer when
    # the judge model changes. A verdict that cannot name the settings
    # that produced it cannot be compared against another verdict.
    evaluator_config: dict[str, Any] = Field(default_factory=dict)

    # Whether this verdict was measured or manufactured. See VerdictSource.
    verdict_source: VerdictSource = "graded"

    # Timestamp
    timestamp: UTCDatetime

    # Agent result traceability
    agent_result_ref: AgentResultRef

    # Verdict
    predicted_label: int  # 1 = success, 0 = failure

    # Shortcut fields
    model_id: str = ""
    browser_id: str = ""

    # Evaluation details
    evaluation_details: EvalDetails

    # Failure analysis
    failure_category: str | None = None
    failure_classification: FailureClassification | None = None

    # Benchmark-specific top-level fields
    task_type: str | None = None
    correct_answer: str | None = None
    agent_response: str | None = None
