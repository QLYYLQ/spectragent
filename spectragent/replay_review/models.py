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
    """What the request carries: the observation budget and what is withheld."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    done_answer: Literal["keep", "withhold"] = Field(
        description="withhold removes the source done answer text from the request; the action type stays"
    )
    max_text_chars: int = Field(
        ge=1,
        strict=True,
        description="Maximum clean DOM characters from each observation; truncation is recorded",
    )
    max_links: int = Field(
        ge=1,
        strict=True,
        description="Maximum visible links and geometry entries from each observation; omissions are recorded",
    )
    include_screenshots: bool = Field(
        strict=True, description="Include raw recorded screenshots as native Decision assets"
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
    questions_sha256: str = trace_safe(
        "fit provenance, not a credential",
        pattern=r"^[0-9a-f]{64}$",
        description="Digest of the question set the information answers came from",
    )
    evidence: ReviewEvidenceOptions = Field(
        description="Evidence options, budget included, the information answers came from"
    )
    evidence_sha256: str = trace_safe(
        "fit provenance, not a credential",
        pattern=r"^[0-9a-f]{64}$",
        description="Digest of those evidence options",
    )

    @model_validator(mode="after")
    def check_evidence_digest(self) -> ThresholdFit:
        if self.evidence.sha256() != self.evidence_sha256:
            raise ValueError(
                f"Fit evidence_sha256 {self.evidence_sha256} is not the digest of its own "
                f"evidence options ({self.evidence.sha256()})"
            )
        return self


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


class SiteThresholds(InformationThresholds):
    """Thresholds for pairs whose reference host is this domain or one of its subdomains."""

    scope: Literal["site"]
    domain: str = trace_safe(
        "threshold scope, not a credential",
        min_length=1,
        description="Lower-case domain, written explicitly; matches itself and its subdomains",
    )

    @model_validator(mode="after")
    def check_domain(self) -> SiteThresholds:
        if (
            self.domain != self.domain.strip().lower()
            or self.domain.startswith(".")
            or self.domain.endswith(".")
            or ".." in self.domain
            or any(c in self.domain for c in "/:@?# ")
        ):
            raise ValueError(f"Threshold domain must be a bare lower-case domain: {self.domain!r}")
        return self

    def matches(self, host: str | None) -> bool:
        return host is not None and (host == self.domain or host.endswith("." + self.domain))


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


ThresholdOverride = Annotated[SiteThresholds | TaskThresholds, Field(discriminator="scope")]


class ViewportImageReadings(BaseModel):
    """What each source read for the in-viewport image fact; None when it could not measure."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    replay_log: bool | None = Field(
        description="An image request the replay log records as a miss is referenced by a "
        "main-document element in the viewport"
    )
    capture_state: bool | None = Field(
        description="The replay capture's capture_state counted a visible image in the viewport "
        "that failed to load"
    )

    def combined(self) -> bool | None:
        """True when either source reads true, False when one measured and neither reads true,
        None when neither could measure."""
        readings = (self.replay_log, self.capture_state)
        if any(reading is True for reading in readings):
            return True
        if any(reading is False for reading in readings):
            return False
        return None


class MachineFacts(BaseModel):
    """Recorded machine facts the policy reads; none of them come from the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    site_host: str | None = Field(description="Host of the reference observation URL")
    task: TaskIdentity | None = Field(description="Source task identity; None when unrecorded")
    restoration_warning: bool = Field(description="The restoration check did not pass")
    viewport_image_not_served: bool | None = Field(
        description="An image in the replay's viewport was not served or failed to load, by "
        "either source; None when neither source could measure"
    )
    viewport_image_sources: ViewportImageReadings = Field(
        description="Each source's own reading; the sources that read the fact's value supplied it"
    )

    @model_validator(mode="after")
    def check_viewport_image_sources(self) -> MachineFacts:
        if self.viewport_image_not_served != self.viewport_image_sources.combined():
            raise ValueError(
                "viewport_image_not_served must combine its sources: true when either reads "
                "true, None when neither measured"
            )
        return self


class AppliedThresholds(BaseModel):
    """The threshold pair one assessment used, with its scope and fit provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Literal["default", "site", "task"]
    domain: str | None = None
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
                ("site", override.domain)
                if isinstance(override, SiteThresholds)
                else ("task", override.benchmark, override.subset, override.task_id)
            )
            if key in seen:
                raise ValueError(f"Threshold scope appears twice: {key}")
            seen.add(key)
        return self

    def check_fitted_on(
        self, questions: ReviewQuestionSet, evidence: ReviewEvidenceOptions
    ) -> None:
        """Refuse thresholds fitted on other questions or evidence options; re-fit them instead."""
        questions_sha256, evidence_sha256 = questions.sha256(), evidence.sha256()
        sets: list[tuple[str, InformationThresholds]] = [("default", self.thresholds)]
        for override in self.overrides:
            name = (
                f"site {override.domain}"
                if isinstance(override, SiteThresholds)
                else f"task {override.benchmark}/{override.subset}/{override.task_id}"
            )
            sets.append((name, override))
        for name, thresholds in sets:
            fit = thresholds.fit
            if fit.questions_sha256 != questions_sha256 or fit.evidence_sha256 != evidence_sha256:
                raise ValueError(
                    f"The {name} thresholds were fitted on question set {fit.questions_sha256} "
                    f"and evidence options {fit.evidence_sha256}, but the configured question "
                    f"set is {questions_sha256} and the evidence options are {evidence_sha256}. "
                    "Re-fit the thresholds or write a new fit entry for these questions and "
                    "evidence options."
                )

    def select(self, facts: MachineFacts) -> AppliedThresholds:
        """Task override, else the longest matching site domain, else the global default."""
        for override in self.overrides:
            if isinstance(override, TaskThresholds) and override.identity() == facts.task:
                return AppliedThresholds(
                    scope="task",
                    task=override.identity(),
                    accept_information_min=override.accept_information_min,
                    investigate_information_max=override.investigate_information_max,
                    fit=override.fit,
                )
        sites = [
            override
            for override in self.overrides
            if isinstance(override, SiteThresholds) and override.matches(facts.site_host)
        ]
        if sites:
            site = max(sites, key=lambda override: len(override.domain))
            return AppliedThresholds(
                scope="site",
                domain=site.domain,
                accept_information_min=site.accept_information_min,
                investigate_information_max=site.investigate_information_max,
                fit=site.fit,
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
