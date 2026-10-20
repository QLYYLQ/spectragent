"""Review execution, native answers, machine facts and routing remain separate facts."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.core.llm.decision import (
    DecisionImage,
    DecisionQuestion,
    DecisionRequest,
    DecisionResult,
)
from spectragent.core.trace.redaction import trace_safe
from spectragent.schemas.action import Action

ReviewStatus = Literal[
    "assessed",
    "missing_source",
    "scope_unrecorded",
    "missing_evidence",
    "corrupt_evidence",
    "alignment_error",
    "unsupported_input",
    "input_limit",
    "http_error",
    "timeout",
    "transport_error",
    "invalid_response",
    "cancelled",
    "review_error",
]
ReviewRoute = Literal["candidate_acceptable", "investigate_difference", "further_review"]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def content_sha256(value: Any) -> str:
    """SHA-256 of canonical JSON: sorted keys, compact separators, UTF-8."""
    text = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(text.encode()).hexdigest()


class ReviewQuestionSet(BaseModel):
    """The questions sent to the decision model; the label is for people, the digest decides."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = trace_safe(
        "question set label", min_length=1, description="Human-readable question set label"
    )
    items: dict[str, DecisionQuestion] = Field(
        min_length=1, description="Question ID to native Decision question, sent as written"
    )

    @model_validator(mode="after")
    def check_ids(self) -> ReviewQuestionSet:
        if any(not key.strip() for key in self.items):
            raise ValueError("Review question IDs must be nonempty")
        return self

    def logical_items(self) -> dict[str, Any]:
        """The questions as sent: omitted fields stay omitted, explicit nulls stay null."""
        return {
            key: {
                **question.model_dump(mode="json", by_alias=True, exclude_unset=True),
                "type": question.type,
            }
            for key, question in self.items.items()
        }

    def content(self) -> dict[str, Any]:
        return {"version": self.version, "items": self.logical_items()}

    def sha256(self) -> str:
        return content_sha256(self.content())


class ReviewEvidenceOptions(BaseModel):
    """What the request carries beyond the aligned observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    done_answer: Literal["keep", "withhold"] = Field(
        description="withhold removes the source done answer text from the request; the action type stays"
    )

    def sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json"))


class ThresholdFit(BaseModel):
    """Where one threshold pair came from; thresholds are re-fitted as labelled data grows."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset: str = trace_safe(
        "fit provenance, not a credential",
        min_length=1,
        description="The labelled pairs the thresholds were fitted on",
    )
    dataset_sha256: str = trace_safe(
        "fit provenance, not a credential",
        pattern=r"^[0-9a-f]{64}$",
        description="Digest identifying that labelled data set",
    )
    fitted_on: date = trace_safe("fit provenance, not a credential", description="Date of the fit")
    policy_version: str = trace_safe(
        "fit provenance, not a credential",
        min_length=1,
        description="Policy version the fit was made for",
    )


class InformationThresholds(BaseModel):
    """Provisional triage thresholds on the information question's native Noul."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    accept_information_min: float = Field(
        ge=0, le=1, allow_inf_nan=False, description="Minimum for candidate_acceptable"
    )
    investigate_information_max: float = Field(
        ge=0, le=1, allow_inf_nan=False, description="Maximum that sends a pair to investigation"
    )
    fit: ThresholdFit

    @model_validator(mode="after")
    def check_order(self) -> InformationThresholds:
        if not self.investigate_information_max < self.accept_information_min:
            raise ValueError("investigate_information_max must be below accept_information_min")
        return self


class TaskIdentity(BaseModel):
    """The source run's dataset key, exactly as recorded in its trace configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    benchmark: str
    subset: str
    task_id: str


class HostThresholds(InformationThresholds):
    """Thresholds for pairs whose reference observation is on this host."""

    scope: Literal["host"]
    host: str = trace_safe(
        "threshold scope, not a credential",
        min_length=1,
        description="Lower-case host name, matched exactly",
    )

    @model_validator(mode="after")
    def check_host(self) -> HostThresholds:
        if self.host != self.host.strip().lower() or any(c in self.host for c in "/:@?# "):
            raise ValueError(f"Threshold host must be a bare lower-case host name: {self.host!r}")
        return self


class TaskThresholds(InformationThresholds):
    """Thresholds for pairs of one source task, matched on benchmark, subset and task ID."""

    scope: Literal["task"]
    benchmark: str = trace_safe("threshold scope, not a credential", min_length=1)
    subset: str = trace_safe(
        "threshold scope, not a credential",
        description="Recorded subset; an empty string matches an empty subset",
    )
    task_id: str = trace_safe("threshold scope, not a credential", min_length=1)

    def identity(self) -> TaskIdentity:
        return TaskIdentity(benchmark=self.benchmark, subset=self.subset, task_id=self.task_id)


ThresholdOverride = Annotated[HostThresholds | TaskThresholds, Field(discriminator="scope")]


class MachineFacts(BaseModel):
    """Recorded machine facts the policy reads; none of them come from the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    site_host: str | None = Field(description="Host of the reference observation URL")
    task: TaskIdentity | None = Field(description="Source task identity; None when unrecorded")
    restoration_warning: bool = Field(description="The restoration check did not pass")
    viewport_image_not_served: bool | None = Field(
        description="An image the replay did not serve is referenced in the viewport; None when not measurable"
    )


class AppliedThresholds(BaseModel):
    """The threshold pair one assessment used, with its scope and fit provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Literal["default", "host", "task"]
    host: str | None = None
    task: TaskIdentity | None = None
    accept_information_min: float
    investigate_information_max: float
    fit: ThresholdFit


class ReviewPolicy(BaseModel):
    """Triage policy; the version fixes the gates, YAML gives the question and thresholds."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["replay-triage-2"]
    information_question: str = trace_safe(
        "question ID, not a credential",
        min_length=1,
        description="ID of the Noul question that routes; other questions are recorded",
    )
    thresholds: InformationThresholds = Field(description="Global default thresholds")
    overrides: list[ThresholdOverride] = Field(
        description="Site or task thresholds; a task match wins over a site match"
    )

    @model_validator(mode="after")
    def check_scopes(self) -> ReviewPolicy:
        seen: set[tuple[str, ...]] = set()
        for override in self.overrides:
            key = (
                ("host", override.host)
                if isinstance(override, HostThresholds)
                else ("task", override.benchmark, override.subset, override.task_id)
            )
            if key in seen:
                raise ValueError(f"Threshold scope appears twice: {key}")
            seen.add(key)
        return self

    def select(self, facts: MachineFacts) -> AppliedThresholds:
        """Task override, else site override, else the global default."""
        for override in self.overrides:
            if isinstance(override, TaskThresholds) and override.identity() == facts.task:
                return AppliedThresholds(
                    scope="task",
                    task=override.identity(),
                    accept_information_min=override.accept_information_min,
                    investigate_information_max=override.investigate_information_max,
                    fit=override.fit,
                )
        for override in self.overrides:
            if isinstance(override, HostThresholds) and override.host == facts.site_host:
                return AppliedThresholds(
                    scope="host",
                    host=override.host,
                    accept_information_min=override.accept_information_min,
                    investigate_information_max=override.investigate_information_max,
                    fit=override.fit,
                )
        return AppliedThresholds(
            scope="default",
            accept_information_min=self.thresholds.accept_information_min,
            investigate_information_max=self.thresholds.investigate_information_max,
            fit=self.thresholds.fit,
        )

    def sha256(self) -> str:
        return content_sha256(self.model_dump(mode="json"))


class QuestionSetRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    sha256: Sha256


class ReviewSettings(BaseModel):
    """Questions, policy and evidence options of a review, each with its content digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    questions: QuestionSetRef
    policy: ReviewPolicy
    policy_sha256: Sha256
    evidence: ReviewEvidenceOptions
    evidence_sha256: Sha256

    @classmethod
    def of(
        cls,
        questions: ReviewQuestionSet,
        policy: ReviewPolicy,
        evidence: ReviewEvidenceOptions,
    ) -> ReviewSettings:
        return cls(
            questions=QuestionSetRef(version=questions.version, sha256=questions.sha256()),
            policy=policy,
            policy_sha256=policy.sha256(),
            evidence=evidence,
            evidence_sha256=evidence.sha256(),
        )


class EvidenceRef(BaseModel):
    run_id: str
    step: int = Field(ge=0)
    trace: str
    manifest_sha256: str = Field(
        description="SHA-256 of the validated manifest's compact model_dump_json serialization"
    )
    config_hash: str
    files: dict[str, str] = Field(default_factory=dict)


class ReviewObservation(BaseModel):
    available: bool = True
    url: str
    text: str
    links: list[dict[str, Any]] = Field(default_factory=list)
    geometry: list[dict[str, Any]] = Field(default_factory=list)
    viewport: list[int] = Field(default_factory=list)
    captured_at: float | None = None
    cycle: int | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    image: DecisionImage | None = None


class ReviewCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1)
    next_actions: list[Action]
    reference: ReviewObservation
    replay: ReviewObservation
    source_ref: EvidenceRef
    replay_ref: EvidenceRef
    facts: MachineFacts
    machine_evidence: dict[str, Any] = Field(default_factory=dict)
    visual_required: bool = False


class ReviewAssessment(BaseModel):
    status: ReviewStatus
    route: ReviewRoute = "further_review"
    reasons: list[str] = Field(default_factory=list)
    information: float | None = Field(default=None, description="The routed question's native Noul")
    thresholds: AppliedThresholds | None = None
    facts: MachineFacts | None = None
    request: DecisionRequest | None = None
    result: DecisionResult | None = None
    settings: ReviewSettings
    detail: str | None = None


class ReviewPair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    replay: str
    source_step: int = Field(ge=0)
    replay_step: int = Field(ge=0)
    source_config: str
    visual_required: bool = False


class ReviewInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["af-replay-review-input-1"]
    pairs: list[ReviewPair] = Field(min_length=1)


class ReviewEvidenceError(ValueError):
    def __init__(self, status: ReviewStatus, message: str) -> None:
        super().__init__(message)
        self.status: ReviewStatus = status
