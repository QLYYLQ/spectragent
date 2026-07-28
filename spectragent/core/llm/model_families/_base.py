"""Base ModelFamily dataclass for declaring model capabilities."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelFamily:
    """Declarative metadata for a model family.

    Providers reach this table through their ``model_family`` property
    (``detect_model_family(self.model)``) instead of sniffing model-name
    substrings inline. Currently consulted:

    * ``supports_strict_mode`` — whether to emit an OpenAI ``"strict": true``
      schema and let :class:`SchemaOptimizer` force every property into
      ``required`` (``openai_base``, ``openai_azure``, ``openai_groq``,
      ``openai_deepseek``, ``anthropic_base``, ``bedrock``, ``ollama``).
    * ``supports_json_schema`` / ``schema_requires_gemini_fix`` — whether a
      model behind an OpenAI-compatible gateway needs the prompt-JSON fallback
      (``openai_vercel``) and whether to run ``fix_gemini_schema`` (``google``).

    The remaining flags are declared but not yet consulted by any provider.
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
