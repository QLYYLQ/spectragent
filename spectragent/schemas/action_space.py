"""The effective action contract shared by prompt, execution, and experiment identity."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from spectragent.schemas.action import ActionDescriptor
from spectragent.schemas.observation.geometry import CoordinateSpace


class ActionSpace(BaseModel):
    """The caller's filter and the action space it actually produced.

    ``None`` means no extra filter; an empty list explicitly disables every
    action. Descriptors already include negotiation and capability parameter
    narrowing. Bindings are the actual map used by the guard, including aliases
    whose target a filter removed (those aliases cannot execute).

    ``coordinate_space`` is the grid the run's model writes points on, set by
    the framework from the agent's declaration when the space holds a point
    action (``core/loops/coordinate_frames.py::framed_action_space``); the point
    params' descriptor text then says what the model writes on it, while the
    guard still executes the capability's units. ``None``: the model writes
    no point, or writes the capability's units (``docs/architecture_design.md``
    §4.1 "坐标系归框架").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_filter: list[str] | None = None
    descriptors: list[ActionDescriptor] = Field(default_factory=list)
    allowed_actions: frozenset[str] = frozenset()
    action_bindings: dict[str, str] = Field(default_factory=dict)
    coordinate_space: CoordinateSpace | None = None

    @field_serializer("allowed_actions")
    def serialize_allowed_actions(self, value: frozenset[str]) -> list[str]:
        """Set membership has a deterministic representation in experiment hashes."""
        return sorted(value)

    @model_validator(mode="after")
    def validate_prompt_and_guard(self) -> Self:
        advertised = frozenset(descriptor.name for descriptor in self.descriptors)
        if advertised != self.allowed_actions:
            raise ValueError("prompt descriptors and guard allowed_actions must agree")
        if len(advertised) != len(self.descriptors):
            raise ValueError("prompt descriptors must contain each action once")
        if self.coordinate_space is not None and not any(
            descriptor.point_params for descriptor in self.descriptors
        ):
            raise ValueError("an action space without a point action is taught no coordinate frame")
        return self
