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
    * ``schema_rejects_closed_union_branches`` — whether to open the
      ``anyOf`` branches of a closed union before sending
      (``openai_base``, ``openai_azure``).

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
    schema_rejects_closed_union_branches: bool = False
    """The family's schema parser refuses ``additionalProperties`` inside an
    ``anyOf``/``oneOf`` branch.

    True for Gemini, measured through an OpenAI-compatible gateway: a closed
    union of single-key action models comes back
    ``Unknown name "additionalProperties" at
    'generation_config.response_schema...any_of[0]'``, while the same schema
    with open branches and a closed root is accepted. It is a fact about the
    *family*, not the gateway — the gateway only translates — so it is
    declared here and read by
    :meth:`SchemaOptimizer.open_union_branches`'s callers.

    A flag rather than an unconditional transform because the opposite is
    also true somewhere: OpenAI's own ``"strict": true`` dialect *requires*
    ``additionalProperties: false`` on every object, ``anyOf`` branches
    included. Opening branches for every request would trade a Gemini 400
    for an OpenAI 400. Something has to say which side a model is on, and
    that something is the family table — this is permanent, not a step on
    the way to deleting the flag.
    """
    name_patterns: tuple[str, ...] = ()
