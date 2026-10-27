import re
from dataclasses import dataclass
from typing import Literal

from spectragent.core.llm.model_families._base import ModelFamily

#: Every thinking level the Gemini API defines: its ``ThinkingLevel`` enum
#: (``MINIMAL``, ``LOW``, ``MEDIUM``, ``HIGH``) without
#: ``THINKING_LEVEL_UNSPECIFIED``, spelled the way YAML writes it.
#: https://ai.google.dev/api/generate-content#ThinkingLevel
#: Which of them one model accepts is narrower; that is the policy below.
ThinkingLevel = Literal["minimal", "low", "medium", "high"]


@dataclass(frozen=True)
class GeminiThinkingPolicy:
    """The native thinking controls a measured Gemini version accepts.

    Defaults describe the provider's existing request policy. They are not
    a claim about the server's default reasoning level.
    """

    supports_level: bool = False
    accepted_levels: tuple[ThinkingLevel, ...] | None = None
    default_level: ThinkingLevel | None = None
    default_budget: int | None = None
    ignores_budget: bool = False
    fallback_to_low: tuple[ThinkingLevel, ...] = ()


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


#: Google documents 1.0 as the temperature default for every Gemini 3 model
#: and strongly recommends keeping it: "Changing the temperature (setting it
#: below 1.0) may lead to unexpected behavior, such as looping or degraded
#: performance". https://ai.google.dev/gemini-api/docs/gemini-3#temperature
_GEMINI_3_TEMPERATURE = 1.0
#: The provider's long-standing default for every model before Gemini 3.
_EARLIER_TEMPERATURE = 0.5
#: Any Gemini 3 model id, a gateway's ``google/`` prefix included: "gemini-3"
#: followed by a separator or the end, never by another digit.
_GEMINI_3 = re.compile(r"gemini-3(?![0-9])")


def gemini_default_temperature(model: str) -> float:
    """The temperature ``temperature: auto`` sends to *model*.

    Google's guidance covers all Gemini 3 models, so this matches the major
    version rather than a list of measured ids.
    """
    return _GEMINI_3_TEMPERATURE if _GEMINI_3.search(model) else _EARLIER_TEMPERATURE


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
    schema_requires_gemini_fix=True,
    schema_rejects_closed_union_branches=True,
    name_patterns=(
        "gemini-",
        "gemma-",
        "google/gemini-",
        "google/gemma-",
    ),
)
