"""Consumers depend on the review contract rather than a concrete decision provider."""

from typing import Protocol, runtime_checkable

from spectragent.core.llm.base import BaseDecisionModel
from spectragent.core.llm.lifecycle import ModelClientLifecycle
from spectragent.replay_review.models import ReviewAssessment, ReviewCase, ReviewPolicy


@runtime_checkable
class BaseReplayReviewer(ModelClientLifecycle, Protocol):
    decision_model: BaseDecisionModel
    policy: ReviewPolicy
    max_text_chars: int
    max_links: int
    include_screenshots: bool

    @property
    def reviewer_id(self) -> str: ...

    async def review(self, case: ReviewCase) -> ReviewAssessment: ...

    def questions(self) -> dict: ...
