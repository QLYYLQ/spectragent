"""Decision providers have their own axis; they cannot enter a Chat receptor."""

from typing import Literal

from conscribe import create_registrar

from spectragent.core.llm.base import BaseDecisionModel
from spectragent.core.llm.model import ModelBase

DecisionModelRegistrar = create_registrar(
    "decision_model",
    BaseDecisionModel,
    discriminator_fields=["model_type", "provider"],
    key_separator=".",
)


class DecisionModelBase(ModelBase, metaclass=DecisionModelRegistrar.Meta):
    __abstract__ = True
    __config_annotated_only__ = True

    @property
    def inference_kind(self) -> Literal["decision"]:
        return "decision"
