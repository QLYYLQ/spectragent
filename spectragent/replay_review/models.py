"""Review execution, semantic judgment and routing remain separate facts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from spectragent.core.llm.decision import DecisionImage, DecisionRequest, DecisionResult
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
ReviewVerdict = Literal["acceptable", "material_difference", "insufficient_evidence"]
ReviewRoute = Literal["candidate_acceptable", "investigate_difference", "further_review"]


class ReviewPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["replay-triage-1"] = "replay-triage-1"
    confidence_min: float = Field(default=0.9, ge=0, le=1, allow_inf_nan=False)
    equivalence_min: float = Field(default=0.8, ge=0.5, le=1, allow_inf_nan=False)
    difference_max: float = Field(default=0.2, ge=0, le=0.5, allow_inf_nan=False)


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
    machine_evidence: dict[str, Any] = Field(default_factory=dict)
    visual_required: bool = False


class ReviewAssessment(BaseModel):
    status: ReviewStatus
    verdict: ReviewVerdict | None = None
    route: ReviewRoute = "further_review"
    reasons: list[str] = Field(default_factory=list)
    request: DecisionRequest | None = None
    result: DecisionResult | None = None
    question_version: str = "replay-relevance-1"
    policy: ReviewPolicy = Field(default_factory=ReviewPolicy)
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
