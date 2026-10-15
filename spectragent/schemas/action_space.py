"""The effective action contract shared by prompt, execution, and experiment identity."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.schemas.action import ActionDescriptor


class ActionSpace(BaseModel):
    """The caller's filter and the action space it actually produced.

    ``None`` means no extra filter; an empty list explicitly disables every
    action. Descriptors already include negotiation and capability parameter
    narrowing. Bindings are the actual map used by the guard, including aliases
    whose target a filter removed (those aliases cannot execute).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_filter: list[str] | None = None
    descriptors: list[ActionDescriptor] = Field(default_factory=list)
    allowed_actions: frozenset[str] = frozenset()
    action_bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_prompt_and_guard(self) -> Self:
        advertised = frozenset(descriptor.name for descriptor in self.descriptors)
        if advertised != self.allowed_actions:
            raise ValueError("prompt descriptors and guard allowed_actions must agree")
        if len(advertised) != len(self.descriptors):
            raise ValueError("prompt descriptors must contain each action once")
        return self
