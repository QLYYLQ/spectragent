"""Declarative registry of chat-template test cases.

Each ProviderSpec encodes one registered provider (``registry_key``) plus how to
point it at a proxy (``base_url_style``) and which structured-output mechanisms
to exercise (``structured_modes``). Adding coverage for a new provider = one row.

``tier``:
  * core      — run by default (openai / anthropic / google, the three the user
                explicitly asked about, + openrouter).
  * extended  — run only with ``--extended`` (deepseek / cerebras / vercel /
                mistral / google-http-options).

``enabled_kinds``: restrict a spec to certain PROXY_KIND values. Native Anthropic
and native Gemini endpoints exist on dmxapi but NOT on openrouter, so those specs
are dmxapi-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from schemas import PERSON_PROMPT, PROJECT_PROMPT, PersonInfo, ProjectPlan


@dataclass(frozen=True)
class StructuredMode:
    """One way to request structured output from a provider."""

    label: str
    output_model: type[BaseModel]
    prompt: str = PERSON_PROMPT
    extra_kwargs: dict[str, Any] = field(default_factory=dict)
    inject_system: bool = False  # prepend a SystemMessage (needed by schema-in-prompt paths)


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    registry_key: str
    base_url_style: str  # openai_v1 | mistral_v1 | anthropic | google | google_http_options
    model_key: str  # key into env.models
    tier: str  # core | extended
    base_kwargs: dict[str, Any] = field(default_factory=dict)
    structured_modes: tuple[StructuredMode, ...] = ()
    enabled_kinds: frozenset[str] | None = None  # None = all kinds
    note: str = ""

    def enabled_for(self, kind: str) -> bool:
        return self.enabled_kinds is None or kind in self.enabled_kinds


# --- structured-mode fragments reused below ---
_STRICT = StructuredMode("strict", PersonInfo)
_STRICT_NESTED = StructuredMode("strict-nested", ProjectPlan, prompt=PROJECT_PROMPT)
_PROMPT_FALLBACK = StructuredMode(
    "prompt-fallback",
    PersonInfo,
    extra_kwargs={"dont_force_structured_output": True, "add_schema_to_system_prompt": True},
    inject_system=True,
)


SPECS: tuple[ProviderSpec, ...] = (
    # ── OpenAI (direct, via OpenAI-compat proxy) ──────────────────────────────
    ProviderSpec(
        id="openai",
        registry_key="openai.direct",
        base_url_style="openai_v1",
        model_key="openai",
        tier="core",
        base_kwargs={"temperature": 0.2, "max_completion_tokens": 1024},
        structured_modes=(_STRICT, _STRICT_NESTED, _PROMPT_FALLBACK),
        note="structured = response_format json_schema strict (no fallback). "
        "prompt-fallback probe isolates strict-schema rejection vs model incapacity.",
    ),
    # ── OpenRouter (OpenAI-compat, its own gateway) ───────────────────────────
    ProviderSpec(
        id="openrouter",
        registry_key="openai.openrouter",
        base_url_style="openai_v1",
        model_key="openrouter",
        tier="core",
        base_kwargs={"temperature": 0.2, "max_completion_tokens": 1024},
        structured_modes=(_STRICT,),
        enabled_kinds=frozenset({"openrouter"}),
        note="dmxapi doesn't take namespaced openrouter model ids; run under PROXY_KIND=openrouter.",
    ),
    # ── Anthropic (native Messages API — dmxapi only) ─────────────────────────
    ProviderSpec(
        id="anthropic",
        registry_key="anthropic.direct",
        base_url_style="anthropic",  # base_url must NOT include /v1
        model_key="anthropic",
        tier="core",
        base_kwargs={"max_tokens": 1024, "temperature": 0.2},
        structured_modes=(
            # native output_config.format is the DEFAULT path now (no extra kwargs).
            StructuredMode("native", PersonInfo),
            StructuredMode("native-nested", ProjectPlan, prompt=PROJECT_PROMPT),
            # tool-use = legacy path, opt-in for comparison.
            StructuredMode("tool-use", PersonInfo, extra_kwargs={"use_native_structured_output": False}),
            StructuredMode(
                "tool-use-nested",
                ProjectPlan,
                prompt=PROJECT_PROMPT,
                extra_kwargs={"use_native_structured_output": False},
            ),
        ),
        enabled_kinds=frozenset({"dmxapi"}),
        note="native output_config.format (default) vs tool-use. openrouter has no native Anthropic API.",
    ),
    # ── Google Gemini (native, base_url field — dmxapi only) ──────────────────
    ProviderSpec(
        id="google",
        registry_key="google.default",
        base_url_style="google",  # base_url must NOT include /v1
        model_key="google",
        tier="core",
        base_kwargs={"temperature": 0.5, "max_output_tokens": 1024},
        structured_modes=(
            StructuredMode("native", PersonInfo, extra_kwargs={"supports_structured_output": True}),
            StructuredMode(
                "native-nested",
                ProjectPlan,
                prompt=PROJECT_PROMPT,
                extra_kwargs={"supports_structured_output": True},
            ),
            StructuredMode(
                "prompt", PersonInfo, extra_kwargs={"supports_structured_output": False}
            ),
        ),
        enabled_kinds=frozenset({"dmxapi"}),
        note="structured = native response_schema (+ ```json fence fallback). openrouter has no native Gemini API.",
    ),
    # ── Google Gemini via http_options (verifies the alt base_url path) ───────
    ProviderSpec(
        id="google-http",
        registry_key="google.default",
        base_url_style="google_http_options",
        model_key="google",
        tier="extended",
        base_kwargs={"temperature": 0.5, "max_output_tokens": 1024},
        structured_modes=(
            StructuredMode("native", PersonInfo, extra_kwargs={"supports_structured_output": True}),
        ),
        enabled_kinds=frozenset({"dmxapi"}),
        note="same as google but wires base_url via http_options={'base_url': ...}.",
    ),
    # ── DeepSeek (OpenAI-compat; structured = function-calling, proxy-tolerant)─
    ProviderSpec(
        id="deepseek",
        registry_key="openai.deepseek",
        base_url_style="openai_v1",
        model_key="deepseek",
        tier="extended",
        base_kwargs={"temperature": 0.2, "max_completion_tokens": 1024},
        structured_modes=(StructuredMode("tool-call", PersonInfo),),
        note="structured via forced function-calling — avoids strict json_schema rejection.",
    ),
    # ── Cerebras (OpenAI-compat; structured = prompt-injection + regex) ───────
    ProviderSpec(
        id="cerebras",
        registry_key="openai.cerebras",
        base_url_style="openai_v1",
        model_key="cerebras",
        tier="extended",
        base_kwargs={"temperature": 0.2, "max_completion_tokens": 1024},
        structured_modes=(StructuredMode("prompt-inject", PersonInfo),),
        note="structured via prompt-injection + greedy regex — fragile on nested schemas.",
    ),
    # ── Vercel AI Gateway (OpenAI-compat; leave provider_options unset) ───────
    ProviderSpec(
        id="vercel",
        registry_key="openai.vercel",
        base_url_style="openai_v1",
        model_key="vercel",
        tier="extended",
        base_kwargs={"temperature": 0.2, "max_completion_tokens": 1024},
        structured_modes=(StructuredMode("auto", PersonInfo),),
        note="dual-path structured; proxy-safe only without provider_options.",
    ),
    # ── Mistral (raw httpx OpenAI-compat; structured = json_schema strict) ─────
    ProviderSpec(
        id="mistral",
        registry_key="mistral.default",
        base_url_style="mistral_v1",
        model_key="mistral",
        tier="extended",
        base_kwargs={"temperature": 0.2},
        structured_modes=(StructuredMode("json-schema", PersonInfo),),
        note="structured = response_format json_schema (no fallback).",
    ),
)


def select_specs(only: list[str] | None, extended: bool) -> list[ProviderSpec]:
    """Filter specs by --only ids and tier. --only overrides tier gating.

    Kind gating (enabled_for) is applied by the runner so it can emit SKIP rows.
    """
    chosen = []
    for spec in SPECS:
        if only:
            if spec.id not in only:
                continue
        else:
            if spec.tier == "extended" and not extended:
                continue
        chosen.append(spec)
    return chosen
