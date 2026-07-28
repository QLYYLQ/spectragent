"""Environment/config loader for the harness — pure, no network.

A single ``PROXY_BASE_URL`` host (WITHOUT the ``/v1`` suffix) is the source of
truth. From it we derive the two base-URL shapes the providers need:

    openai_base_url   = host + "/v1"     (OpenAI-compat: SDK appends /chat/completions)
    native_base_url   = host             (Anthropic SDK appends /v1/messages;
                                          Google SDK appends /v1beta/models)

Getting this backwards is the #1 cause of 404s (``/v1/v1/...``), so it is
centralised here and applied per-provider in build.py.

Credentials are read from the environment only — never hard-coded. Falls back to
the repo's existing ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` (loaded from .env)
so the harness works out-of-the-box with the project's current dmxapi config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # optional; .env is convenient but not required
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv always present in dev extra
    pass


# Per-provider default model ids. Override via <PROVIDER>_MODELS (comma-separated).
# Defaults chosen to be broadly available on dmxapi.cn; adjust for your proxy.
DEFAULT_MODELS: dict[str, list[str]] = {
    "openai": ["gpt-4o-mini"],
    "openrouter": ["openai/gpt-4o-mini"],
    "deepseek": ["deepseek-chat"],
    "cerebras": ["llama3.1-8b"],
    "vercel": ["openai/gpt-4o-mini"],
    "mistral": ["mistral-large-latest"],
    "anthropic": ["claude-sonnet-4-6"],
    "google": ["gemini-2.5-flash"],
}


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _strip_v1(url: str) -> str:
    """Normalise any base url to a bare host (drop a trailing /v1 or /v1/)."""
    u = url.strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[: -len("/v1")].rstrip("/")
    return u


@dataclass
class HarnessEnv:
    host: str  # bare host, NO /v1
    api_key: str
    kind: str  # "dmxapi" | "openrouter" | custom
    timeout_s: float
    models: dict[str, list[str]] = field(default_factory=dict)

    @property
    def openai_base_url(self) -> str:
        """OpenAI-compatible base — includes /v1."""
        return self.host.rstrip("/") + "/v1"

    @property
    def native_base_url(self) -> str:
        """Anthropic/Google native base — bare host, the SDK appends its version path."""
        return self.host.rstrip("/")

    @property
    def has_creds(self) -> bool:
        return bool(self.host and self.api_key)

    @property
    def masked_key(self) -> str:
        if not self.api_key:
            return "<unset>"
        return self.api_key[:6] + "…" + f"({len(self.api_key)} chars)"


def load_env() -> HarnessEnv:
    kind = (os.getenv("PROXY_KIND") or "dmxapi").strip().lower()

    host = os.getenv("PROXY_BASE_URL")
    if host:
        host = _strip_v1(host)
    else:
        legacy = os.getenv("OPENAI_BASE_URL")
        if legacy:
            host = _strip_v1(legacy)
        elif kind == "openrouter":
            host = "https://openrouter.ai/api"
        else:
            host = ""

    api_key = os.getenv("PROXY_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    timeout_s = float(os.getenv("LLM_TIMEOUT_S", "60"))

    models: dict[str, list[str]] = {}
    for name, default in DEFAULT_MODELS.items():
        models[name] = _split(os.getenv(f"{name.upper()}_MODELS")) or list(default)

    return HarnessEnv(host=host, api_key=api_key, kind=kind, timeout_s=timeout_s, models=models)


def dummy_env() -> HarnessEnv:
    """Credential-free env for offline construct-smoke checks (no network calls made)."""
    return HarnessEnv(
        host="https://proxy.invalid",
        api_key="sk-dummy-offline",
        kind="dmxapi",
        timeout_s=1.0,
        models={k: list(v) for k, v in DEFAULT_MODELS.items()},
    )
