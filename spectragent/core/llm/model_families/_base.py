"""Base ModelFamily dataclass for declaring model capabilities."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelFamily:
    """Declarative metadata for a model family.

    Providers query ModelFamily to decide behaviour (e.g. whether to use
    tool-calling for structured output, whether to inject cache-control
    headers, etc.).
    """

    name: str
    supports_json_schema: bool = True
    supports_tool_calling: bool = True
    prefers_tool_for_structured: bool = False
    supports_strict_mode: bool = False
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_cache_control: bool = False
    schema_requires_gemini_fix: bool = False
    name_patterns: tuple[str, ...] = ()
