"""Declarative model catalog — replaces the 140-line if/elif in get_llm_by_name."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelEntry:
    model_type: str
    model: str
    api_key_env: str
    extra: dict[str, Any] = field(default_factory=dict)


MODEL_CATALOG: dict[str, ModelEntry] = {
    # --- OpenAI (direct) ---
    "openai_gpt_4o": ModelEntry("openai.direct", "gpt-4o", "OPENAI_API_KEY"),
    "openai_gpt_4o_mini": ModelEntry("openai.direct", "gpt-4o-mini", "OPENAI_API_KEY"),
    "openai_gpt_4_1_mini": ModelEntry("openai.direct", "gpt-4.1-mini", "OPENAI_API_KEY"),
    "openai_o1": ModelEntry("openai.direct", "o1", "OPENAI_API_KEY"),
    "openai_o1_mini": ModelEntry("openai.direct", "o1-mini", "OPENAI_API_KEY"),
    "openai_o1_pro": ModelEntry("openai.direct", "o1-pro", "OPENAI_API_KEY"),
    "openai_o3": ModelEntry("openai.direct", "o3", "OPENAI_API_KEY"),
    "openai_o3_mini": ModelEntry("openai.direct", "o3-mini", "OPENAI_API_KEY"),
    "openai_o3_pro": ModelEntry("openai.direct", "o3-pro", "OPENAI_API_KEY"),
    "openai_o4_mini": ModelEntry("openai.direct", "o4-mini", "OPENAI_API_KEY"),
    "openai_gpt_5": ModelEntry("openai.direct", "gpt-5", "OPENAI_API_KEY"),
    "openai_gpt_5_mini": ModelEntry("openai.direct", "gpt-5-mini", "OPENAI_API_KEY"),
    "openai_gpt_5_nano": ModelEntry("openai.direct", "gpt-5-nano", "OPENAI_API_KEY"),
    # --- Azure ---
    "azure_gpt_4o": ModelEntry(
        "openai.azure",
        "gpt-4o",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_gpt_4o_mini": ModelEntry(
        "openai.azure",
        "gpt-4o-mini",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_gpt_4_1_mini": ModelEntry(
        "openai.azure",
        "gpt-4.1-mini",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_o1": ModelEntry(
        "openai.azure",
        "o1",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_o1_mini": ModelEntry(
        "openai.azure",
        "o1-mini",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_o1_pro": ModelEntry(
        "openai.azure",
        "o1-pro",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_o3": ModelEntry(
        "openai.azure",
        "o3",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_o3_mini": ModelEntry(
        "openai.azure",
        "o3-mini",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_o3_pro": ModelEntry(
        "openai.azure",
        "o3-pro",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_gpt_5": ModelEntry(
        "openai.azure",
        "gpt-5",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    "azure_gpt_5_mini": ModelEntry(
        "openai.azure",
        "gpt-5-mini",
        "AZURE_OPENAI_KEY",
        extra={"azure_endpoint_env": "AZURE_OPENAI_ENDPOINT"},
    ),
    # --- Google ---
    "google_gemini_2_0_flash": ModelEntry("google.default", "gemini-2.0-flash", "GOOGLE_API_KEY"),
    "google_gemini_2_0_pro": ModelEntry("google.default", "gemini-2.0-pro", "GOOGLE_API_KEY"),
    "google_gemini_2_5_pro": ModelEntry("google.default", "gemini-2.5-pro", "GOOGLE_API_KEY"),
    "google_gemini_2_5_flash": ModelEntry("google.default", "gemini-2.5-flash", "GOOGLE_API_KEY"),
    "google_gemini_2_5_flash_lite": ModelEntry(
        "google.default", "gemini-2.5-flash-lite", "GOOGLE_API_KEY"
    ),
    # --- Mistral ---
    "mistral_large": ModelEntry("mistral.default", "mistral-large-latest", "MISTRAL_API_KEY"),
    "mistral_medium": ModelEntry("mistral.default", "mistral-medium-latest", "MISTRAL_API_KEY"),
    "mistral_small": ModelEntry("mistral.default", "mistral-small-latest", "MISTRAL_API_KEY"),
    "codestral": ModelEntry("mistral.default", "codestral-latest", "MISTRAL_API_KEY"),
    "pixtral_large": ModelEntry("mistral.default", "pixtral-large-latest", "MISTRAL_API_KEY"),
    # --- Cerebras ---
    "cerebras_llama3_1_8b": ModelEntry("openai.cerebras", "llama3.1-8b", "CEREBRAS_API_KEY"),
    "cerebras_llama3_3_70b": ModelEntry("openai.cerebras", "llama-3.3-70b", "CEREBRAS_API_KEY"),
    "cerebras_gpt_oss_120b": ModelEntry("openai.cerebras", "gpt-oss-120b", "CEREBRAS_API_KEY"),
    "cerebras_llama_4_scout_17b_16e_instruct": ModelEntry(
        "openai.cerebras", "llama-4-scout-17b-16e-instruct", "CEREBRAS_API_KEY"
    ),
    "cerebras_llama_4_maverick_17b_128e_instruct": ModelEntry(
        "openai.cerebras", "llama-4-maverick-17b-128e-instruct", "CEREBRAS_API_KEY"
    ),
    "cerebras_qwen_3_32b": ModelEntry("openai.cerebras", "qwen-3-32b", "CEREBRAS_API_KEY"),
    "cerebras_qwen_3_235b_a22b_instruct_2507": ModelEntry(
        "openai.cerebras", "qwen-3-235b-a22b-instruct-2507", "CEREBRAS_API_KEY"
    ),
    "cerebras_qwen_3_235b_a22b_thinking_2507": ModelEntry(
        "openai.cerebras", "qwen-3-235b-a22b-thinking-2507", "CEREBRAS_API_KEY"
    ),
    "cerebras_qwen_3_coder_480b": ModelEntry(
        "openai.cerebras", "qwen-3-coder-480b", "CEREBRAS_API_KEY"
    ),
    # --- Browser Use ---
    "bu_latest": ModelEntry("browser_use.default", "bu-latest", "BROWSER_USE_API_KEY"),
    "bu_1_0": ModelEntry("browser_use.default", "bu-1-0", "BROWSER_USE_API_KEY"),
    "bu_2_0": ModelEntry("browser_use.default", "bu-2-0", "BROWSER_USE_API_KEY"),
}
