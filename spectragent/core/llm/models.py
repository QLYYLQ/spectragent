"""Convenient access to LLM models via catalog lookup."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from conscribe import discover

from spectragent.core.llm._registrar import LLMRegistrar
from spectragent.core.llm.catalog import MODEL_CATALOG

if TYPE_CHECKING:
    from spectragent.core.llm.base import BaseChatModel


def get_llm_by_name(model_name: str) -> "BaseChatModel":
    """废弃接口
    Factory: create an LLM instance from a string name using env vars for API keys.

    Uses the conscribe registry to resolve ``model_type`` → provider class,
    eliminating the hard-coded if/elif dispatch.

    Examples::

        get_llm_by_name("openai_gpt_4o")
        get_llm_by_name("azure_gpt_4_1_mini")
        get_llm_by_name("google_gemini_2_5_pro")
        get_llm_by_name("bu_latest")
    """
    if not model_name:
        raise ValueError("Model name cannot be empty")

    entry = MODEL_CATALOG.get(model_name)
    if entry is None:
        available = sorted(MODEL_CATALOG.keys())
        raise ValueError(
            f"Unknown model name: '{model_name}'. "
            f"Available: {', '.join(available[:20])}{'...' if len(available) > 20 else ''}"
        )

    # Ensure all provider modules are imported (idempotent)
    discover("spectragent.core.llm.providers")

    cls = LLMRegistrar.get(entry.model_type)

    api_key = os.getenv(entry.api_key_env)
    kwargs: dict[str, object] = dict(entry.extra)

    # Handle azure_endpoint_env indirection
    endpoint_env = kwargs.pop("azure_endpoint_env", None)
    if endpoint_env:
        kwargs["azure_endpoint"] = os.getenv(str(endpoint_env))

    if entry.model:
        kwargs["model"] = entry.model
    if api_key is not None:
        kwargs["api_key"] = api_key

    return cls(**kwargs)


def __getattr__(name: str) -> "BaseChatModel":
    """Create model instances on demand with API keys from environment."""
    try:
        return get_llm_by_name(name)
    except ValueError as e:
        raise AttributeError(f"module '{__name__}' has no attribute '{name}'") from e


__all__ = ["get_llm_by_name"]
