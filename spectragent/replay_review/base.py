"""Consumers depend on the review contract rather than a concrete decision provider."""

from typing import Protocol, runtime_checkable

from spectragent.core.llm.base import BaseDecisionModel
from spectragent.core.llm.lifecycle import ModelClientLifecycle
from spectragent.replay_review.models import (
    ReviewAssessment,
    ReviewCase,
    ReviewEvidenceOptions,
    ReviewPolicy,
    ReviewQuestionSet,
    ReviewSettings,
)


@runtime_checkable
class BaseReplayReviewer(ModelClientLifecycle, Protocol):
    decision_model: BaseDecisionModel
    questions: ReviewQuestionSet
    policy: ReviewPolicy
    evidence: ReviewEvidenceOptions

    @property
    def reviewer_id(self) -> str: ...

    async def review(self, case: ReviewCase) -> ReviewAssessment: ...

    def settings(self) -> ReviewSettings: ...
