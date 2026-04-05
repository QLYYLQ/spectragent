"""Action-related data contracts.

See docs/architecture_design.md §4.5 for the full Action design.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionResult(BaseModel):
    """Action execution result."""

    success: bool
    error: str | None = None
    is_done: bool = False
    extracted_content: str | None = None


class ActionDescriptor(BaseModel):
    """Describes an available action for LLM prompt building. Immutable.

    Action ownership is determined by which Capability provides the
    descriptor — the descriptor itself does not carry this knowledge.

    Two parameter description modes coexist:
    - ``params`` (simple): ``{"url": "string", "index": "integer"}``
    - ``params_schema`` (rich): full JSON Schema for complex/dynamic tools
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    params: dict[str, str] = Field(default_factory=dict)
    params_schema: dict[str, Any] | None = None

    def with_prefix(self, prefix: str) -> "ActionDescriptor":
        """Return a copy with the name prefixed (e.g. ``click`` -> ``browser.click``).

        Used by CompositeEnvironment to disambiguate actions from
        multiple capabilities.
        """
        return self.model_copy(update={"name": f"{prefix}.{self.name}"})

    def to_tool_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function calling format.

        If ``params_schema`` is set, it is used directly as the
        ``parameters`` field. Otherwise a JSON Schema is synthesised
        from the simple ``params`` dict.
        """
        if self.params_schema is not None:
            schema = self.params_schema
        else:
            schema = {
                "type": "object",
                "properties": {k: {"type": v} for k, v in self.params.items()},
            }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


class Action(BaseModel):
    """Parsed action from LLM output."""

    name: str
    params: dict[str, Any] = Field(default_factory=dict)
