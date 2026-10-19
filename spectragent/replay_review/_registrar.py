"""Optional review layer of the existing composition root."""

from typing import ClassVar

from conscribe import create_registrar

from spectragent.replay_review.base import BaseReplayReviewer

ReplayReviewerRegistrar = create_registrar(
    "replay_reviewer", BaseReplayReviewer, discriminator_field="reviewer_id"
)


class ReplayReviewerBase(metaclass=ReplayReviewerRegistrar.Meta):
    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]

    @property
    def reviewer_id(self) -> str:
        return self.__registry_key__
