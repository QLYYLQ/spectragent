"""Action-related data contracts."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionResult(BaseModel):
    """Action execution result."""

    success: bool
    error: str | None = None
    is_done: bool = False
    extracted_content: str | None = None


class ActionDescriptor(BaseModel):
    """Describes an available action for LLM prompt building. Immutable."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    params: dict[str, str]
    env_types: list[str]


class Action(BaseModel):
    """Parsed action from LLM output."""

    name: str
    params: dict[str, Any] = Field(default_factory=dict)
