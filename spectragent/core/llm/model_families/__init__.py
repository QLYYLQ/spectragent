"""Model family detection — maps model names to capability metadata."""

from spectragent.core.llm.model_families._base import ModelFamily
from spectragent.core.llm.model_families.claude import ClaudeFamily
from spectragent.core.llm.model_families.cohere import CohereFamily
from spectragent.core.llm.model_families.deepseek import DeepSeekFamily
from spectragent.core.llm.model_families.gemini import GeminiFamily
from spectragent.core.llm.model_families.generic import GenericFamily
from spectragent.core.llm.model_families.gpt import GPTFamily
from spectragent.core.llm.model_families.grok import GrokFamily
from spectragent.core.llm.model_families.llama import LlamaFamily
from spectragent.core.llm.model_families.mistral import MistralFamily
from spectragent.core.llm.model_families.qwen import QwenFamily

_ALL_FAMILIES: tuple[ModelFamily, ...] = (
    ClaudeFamily,
    GPTFamily,
    GeminiFamily,
    LlamaFamily,
    DeepSeekFamily,
    MistralFamily,
    QwenFamily,
    GrokFamily,
    CohereFamily,
)


def detect_model_family(model_name: str) -> ModelFamily:
    """Detect model family from a model name string.

    Supports ``vendor/model`` format (e.g. ``anthropic/claude-sonnet-4``)
    and direct model names (e.g. ``claude-sonnet-4-20250514``).
    """
    lower = model_name.lower()
    for family in _ALL_FAMILIES:
        for pattern in family.name_patterns:
            if pattern.lower() in lower:
                return family
    return GenericFamily


__all__ = [
    "ModelFamily",
    "detect_model_family",
    "ClaudeFamily",
    "GPTFamily",
    "GeminiFamily",
    "LlamaFamily",
    "DeepSeekFamily",
    "MistralFamily",
    "QwenFamily",
    "GrokFamily",
    "CohereFamily",
    "GenericFamily",
]
