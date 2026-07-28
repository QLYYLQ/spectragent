"""Construct a live provider instance from a ProviderSpec — the one place that
applies the per-provider base_url /v1 rule.
"""

from __future__ import annotations

from typing import Any

from conscribe import discover

from spectragent.core.llm._registrar import LLMRegistrar

from cases import ProviderSpec
from env import HarnessEnv

_discovered = False


def ensure_registered() -> None:
    """Import all provider modules so the registry is populated (idempotent)."""
    global _discovered
    if not _discovered:
        discover("spectragent.core.llm.providers")
        _discovered = True


def registry_keys() -> list[str]:
    ensure_registered()
    return sorted(LLMRegistrar.keys())


def base_url_kwargs(style: str, env: HarnessEnv) -> dict[str, Any]:
    """Map a base_url_style to the exact constructor kwarg(s) for that provider."""
    if style in ("openai_v1", "mistral_v1"):
        return {"base_url": env.openai_base_url}  # host + /v1
    if style in ("anthropic", "google"):
        return {"base_url": env.native_base_url}  # bare host (SDK appends its version path)
    if style == "google_http_options":
        return {"http_options": {"base_url": env.native_base_url}}
    raise ValueError(f"unknown base_url_style: {style!r}")


def build_llm(spec: ProviderSpec, model: str, mode_extra: dict[str, Any], env: HarnessEnv):
    """Instantiate the provider class for ``spec`` pointed at the proxy."""
    ensure_registered()
    cls = LLMRegistrar.get(spec.registry_key)
    kwargs: dict[str, Any] = dict(spec.base_kwargs)
    kwargs.update(mode_extra)
    kwargs.update(base_url_kwargs(spec.base_url_style, env))
    kwargs["model"] = model
    kwargs["api_key"] = env.api_key
    return cls(**kwargs)
