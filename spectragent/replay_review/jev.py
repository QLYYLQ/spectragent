"""Configured task-relative review questions and a reproducible triage policy."""

from __future__ import annotations

import asyncio
import copy
from typing import Annotated, Any, ClassVar, cast

from pydantic import Field

from spectragent.core.llm.base import BaseDecisionModel, require_inference_protocol
from spectragent.core.llm.decision import (
    DecisionRequest,
    DecisionResult,
    NoulAnswer,
    NoulQuestion,
)
from spectragent.core.llm.decision_errors import (
    DecisionHTTPError,
    DecisionInputError,
    DecisionLimitError,
    DecisionResponseError,
    DecisionTimeoutError,
    DecisionTransportError,
)
from spectragent.core.llm.lifecycle import ClientCleanupDiagnostic, close_models
from spectragent.replay_review._registrar import ReplayReviewerBase
from spectragent.replay_review.models import (
    MachineFacts,
    ReviewAssessment,
    ReviewCase,
    ReviewEvidenceOptions,
    ReviewPolicy,
    ReviewQuestionSet,
    ReviewRoute,
    ReviewSettings,
    ReviewStatus,
)


def route_unanswered(
    status: ReviewStatus,
    reasons: list[str],
    settings: ReviewSettings,
    facts: MachineFacts | None,
    **recorded: Any,
) -> ReviewAssessment:
    """No usable model answer: only the deterministic image gate can still route.

    An in-viewport image the replay did not serve sends the pair to
    investigation whether or not the model answered; the execution status
    keeps the failure.
    """
    image = facts is not None and facts.viewport_image_not_served is True
    return ReviewAssessment(
        status=status,
        route="investigate_difference" if image else "further_review",
        reasons=[*reasons, "viewport_image_not_served"] if image else reasons,
        facts=facts,
        settings=settings,
        **recorded,
    )


def route_result(
    result: DecisionResult, settings: ReviewSettings, facts: MachineFacts
) -> ReviewAssessment:
    """Pure routing: re-running it needs no browser, model, or original source files.

    The deterministic image gate comes before the thresholds; a restoration
    warning or an unmeasurable image fact only blocks acceptance.
    """
    policy = settings.policy
    answer = result.answers.get(policy.information_question)
    if not isinstance(answer, NoulAnswer):
        return route_unanswered(
            "invalid_response", ["review_answer_contract"], settings, facts, result=result
        )
    thresholds = policy.select(facts)
    information = answer.noul
    reasons: list[str] = []
    if facts.viewport_image_not_served:
        reasons.append("viewport_image_not_served")
    if information <= thresholds.investigate_information_max:
        reasons.append("information_at_or_below_investigate_max")
    route: ReviewRoute
    if reasons:
        route = "investigate_difference"
    elif information < thresholds.accept_information_min:
        route = "further_review"
        reasons.append("information_between_thresholds")
    else:
        if facts.restoration_warning:
            reasons.append("restoration_warning")
        if facts.viewport_image_not_served is None:
            reasons.append("viewport_images_not_measurable")
        route = "further_review" if reasons else "candidate_acceptable"
    return ReviewAssessment(
        status="assessed",
        route=route,
        reasons=reasons,
        information=information,
        thresholds=thresholds,
        facts=facts,
        result=result,
        settings=settings,
    )


def _is_done(action: dict[str, Any]) -> bool:
    return str(action.get("name", "")).rpartition(".")[2] == "done"


def strip_action_effects(correspondence: list[dict[str, Any]]) -> None:
    """Remove each executed result's measured action effect in place.

    Effects are not review evidence (``docs/architecture_design.md`` §4.18): the
    thresholds were fitted on requests without them, so a request stays
    identical to the fitted evidence. Showing effects would take an explicit
    evidence option, whose digest change invalidates the fit.
    """
    for item in correspondence:
        result = item.get("result")
        if isinstance(result, dict):
            result.pop("effect", None)


def withhold_done_answers(
    next_actions: list[dict[str, Any]], correspondence: list[dict[str, Any]]
) -> None:
    """Remove the done answer text in place; the done action itself stays visible."""
    for action in next_actions:
        if _is_done(action):
            (action.get("params") or {}).pop("extracted_content", None)
    for item in correspondence:
        for side in ("recorded", "executed"):
            action = item.get(side) or {}
            if _is_done(action):
                (action.get("params") or {}).pop("extracted_content", None)
        result = item.get("result") or {}
        if result.get("is_done") and result.get("extracted_content") is not None:
            result["extracted_content"] = None


class JevReplayReviewer(ReplayReviewerBase):
    __registry_key__ = "jev"
    __wiring__: ClassVar[dict[str, Any]] = {"decision_model": "decision_model"}
    decision_model: BaseDecisionModel

    def __init__(
        self,
        questions: Annotated[
            ReviewQuestionSet,
            Field(description="Questions sent to the decision model; their digest is recorded"),
        ],
        policy: Annotated[
            ReviewPolicy,
            Field(
                description="Triage policy with provisional, re-fitted thresholds; not a calibrated correctness guarantee"
            ),
        ],
        evidence: Annotated[
            ReviewEvidenceOptions,
            Field(
                description="What the request carries: observation budget, screenshots and withheld text"
            ),
        ],
    ) -> None:
        if not isinstance(questions, ReviewQuestionSet):
            raise TypeError("Replay reviewer questions must be a ReviewQuestionSet")
        if not isinstance(policy, ReviewPolicy):
            raise TypeError("Replay reviewer policy must be a ReviewPolicy")
        if not isinstance(evidence, ReviewEvidenceOptions):
            raise TypeError("Replay reviewer evidence options must be ReviewEvidenceOptions")
        routed = questions.items.get(policy.information_question)
        if not isinstance(routed, NoulQuestion):
            raise ValueError(
                f"Policy information_question {policy.information_question!r} must name a "
                "Noul question of the configured question set"
            )
        policy.check_fitted_on(questions, evidence)
        self.questions = questions.model_copy(deep=True)
        self.policy = policy.model_copy(deep=True)
        self.evidence = evidence.model_copy(deep=True)

    @property
    def name(self) -> str:
        return self.reviewer_id

    def settings(self) -> ReviewSettings:
        return ReviewSettings.of(self.questions, self.policy, self.evidence)

    def build_request(self, case: ReviewCase) -> DecisionRequest:
        next_actions = [action.model_dump(mode="json") for action in case.next_actions]
        # Preserve retargeting facts without feeding the machine verdict back as
        # a semantic label. A checkpoint may have no executed correspondence yet.
        correspondence = copy.deepcopy(
            (case.machine_evidence.get("restoration") or {}).get("actions", [])
        )
        strip_action_effects(correspondence)
        if self.evidence.done_answer == "withhold":
            withhold_done_answers(next_actions, correspondence)
        state: dict[str, Any] = {
            "task": case.task,
            "next_actions": next_actions,
            "action_correspondence": correspondence,
        }
        assets = {}
        for key, observation in (("reference", case.reference), ("replay", case.replay)):
            state[key] = observation.model_dump(mode="json", exclude={"image"})
            if self.evidence.include_screenshots and observation.image is not None:
                asset = f"af-review-{key}-pixels"
                state[key]["screenshot"] = asset
                assets[asset] = observation.image
        return DecisionRequest.model_validate(
            {"state": state, "questions": self.questions.logical_items(), "assets": assets}
        )

    async def review(self, case: ReviewCase) -> ReviewAssessment:
        require_inference_protocol(
            self.decision_model, "decision", consumer="replay_reviewer[jev].decision_model"
        )
        settings = self.settings()
        missing = not case.reference.available or not case.replay.available
        if case.visual_required and (
            not self.evidence.include_screenshots
            or case.reference.image is None
            or case.replay.image is None
        ):
            return route_unanswered(
                "missing_evidence", ["visual_evidence_required"], settings, case.facts
            )
        if missing:
            return route_unanswered(
                "missing_evidence", ["observation_unavailable"], settings, case.facts
            )
        request = self.build_request(case)
        try:
            result = await self.decision_model.ainvoke(request)
        except asyncio.CancelledError:
            raise
        except (
            DecisionInputError,
            DecisionLimitError,
            DecisionResponseError,
            DecisionTimeoutError,
            DecisionHTTPError,
            DecisionTransportError,
        ) as exc:
            status = {
                DecisionInputError: "unsupported_input",
                DecisionLimitError: "input_limit",
                DecisionResponseError: "invalid_response",
                DecisionTimeoutError: "timeout",
                DecisionHTTPError: "http_error",
                DecisionTransportError: "transport_error",
            }[type(exc)]
            return route_unanswered(
                cast(ReviewStatus, status),
                [status],
                settings,
                case.facts,
                request=request,
                detail=str(exc),
            )
        assessment = route_result(result, settings, case.facts)
        assessment.request = request
        return assessment

    async def aclose(self, *, timeout: float = 5.0) -> tuple[ClientCleanupDiagnostic, ...]:
        return await close_models([self.decision_model], timeout=timeout)
