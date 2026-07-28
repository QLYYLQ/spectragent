"""Provider-neutral tool (function) declarations for native function calling.

Every provider that supports function calling asks for the same three things —
a name, a description, and a JSON Schema for the arguments — and differs only in
where it puts them:

==============  ==========================================================
OpenAI          ``{"type": "function", "function": {name, description,
                parameters, strict}}``
Anthropic       ``{name, description, input_schema}``
Gemini          ``FunctionDeclaration(name, description, parameters)``
Bedrock         ``{"toolSpec": {name, description, inputSchema: {"json": …}}}``
Ollama          the OpenAI shape
==============  ==========================================================

So :class:`ToolDefinition` is that common triple, and each serializer owns the
translation. Nothing here is bespoke.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from spectragent.core.llm.schema.optimizer import SchemaOptimizer

ToolChoice = Literal["auto", "none", "required"]
"""How the model should pick between answering and calling a tool.

``auto``      — the model decides (the mode tau-bench / ToolBench / GAIA use).
``none``      — tools are advertised but must not be called.
``required``  — the model must call at least one tool.

Forcing *one named* tool is deliberately absent: it is what the structured
output path already does (``output_format=``), and admitting a bare tool name
here would make ``tool_choice="auto"`` ambiguous for a tool named ``auto``.
"""

#: An empty-but-valid JSON Schema object — the shape a zero-argument tool needs.
EMPTY_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}


class ToolDefinition(BaseModel):
    """One callable tool, in the form every supported provider can express."""

    name: str
    """The tool name the model will echo back in :class:`ToolCall`."""

    description: str = ""
    """What the tool does. This is the model's only documentation — providers
    pass it through verbatim."""

    parameters: dict[str, Any] = Field(default_factory=lambda: dict(EMPTY_PARAMETERS))
    """JSON Schema for the arguments, as a single flattened object node.

    ``$ref``/``$defs`` must already be inlined: Gemini, Ollama and Bedrock all
    reject or mis-compile references. Build via :meth:`from_model` to get that
    for free.
    """

    strict: bool = False
    """Request OpenAI's strict argument decoding.

    Only meaningful where the provider implements the strict dialect, and only
    valid when :attr:`parameters` satisfies it — see
    :meth:`SchemaOptimizer.is_strict_compatible`.
    """

    @classmethod
    def from_model(
        cls,
        model: type[BaseModel],
        *,
        name: str | None = None,
        description: str | None = None,
        strict: bool = False,
    ) -> "ToolDefinition":
        """Build a tool declaration from a Pydantic model of its arguments.

        The schema is flattened through :class:`SchemaOptimizer`, and *strict*
        is downgraded to ``False`` when the compiled schema cannot satisfy the
        strict dialect — the same rule the structured-output path uses, so a
        model with an open ``dict[str, Any]`` argument degrades to a valid
        non-strict tool instead of a rejected request.
        """
        schema = SchemaOptimizer.create_optimized_json_schema(model, strict=strict)
        schema.pop("title", None)
        return cls(
            name=name or model.__name__,
            description=description if description is not None else (model.__doc__ or "").strip(),
            parameters=schema,
            strict=strict and SchemaOptimizer.is_strict_compatible(schema),
        )


__all__ = ["EMPTY_PARAMETERS", "ToolChoice", "ToolDefinition"]
