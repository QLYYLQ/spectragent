from dataclasses import dataclass

from spectragent.core.llm.model_families._base import ModelFamily


@dataclass(frozen=True)
class GeminiThinkingPolicy:
    """The native thinking controls a measured Gemini version accepts.

    Defaults describe the provider's existing request policy. They are not
    a claim about the server's default reasoning level.
    """

    supports_level: bool = False
    accepted_levels: tuple[str, ...] | None = None
    default_level: str | None = None
    default_budget: int | None = None
    ignores_budget: bool = False
    fallback_to_low: tuple[str, ...] = ()


# Gemini 3.8 Flash's published native contract accepts low/medium/high,
# and rejects minimal. The exact stable id was verified through the native
# route; do not infer support for future versions or gateway aliases.
# https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
_FLASH_38_THINKING = GeminiThinkingPolicy(
    supports_level=True, accepted_levels=("low", "medium", "high")
)

# Native controls and request defaults for the other supported versions.
_THINKING_POLICIES = (
    (
        "gemini-3-pro",
        GeminiThinkingPolicy(
            supports_level=True,
            default_level="low",
            ignores_budget=True,
            fallback_to_low=("minimal", "medium"),
        ),
    ),
    ("gemini-3-flash", GeminiThinkingPolicy(supports_level=True, default_budget=-1)),
    ("gemini-2.5", GeminiThinkingPolicy(default_budget=-1)),
    ("gemini-flash", GeminiThinkingPolicy(default_budget=-1)),
)
_NO_THINKING_DEFAULT = GeminiThinkingPolicy()


def gemini_thinking_policy(model: str) -> GeminiThinkingPolicy:
    """Version-specific native controls, with no guessed future support."""
    if model == "gemini-3.8-flash":
        return _FLASH_38_THINKING
    for name_pattern, policy in _THINKING_POLICIES:
        if name_pattern in model:
            return policy
    return _NO_THINKING_DEFAULT


GeminiFamily = ModelFamily(
    name="gemini",
    supports_json_schema=True,
    supports_tool_calling=True,
    supports_thinking=True,
    schema_requires_gemini_fix=True,
    schema_rejects_closed_union_branches=True,
    name_patterns=(
        "gemini-",
        "gemma-",
        "google/gemini-",
        "google/gemma-",
    ),
)
