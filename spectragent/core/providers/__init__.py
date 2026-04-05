"""Provider infrastructure — one BaseProvider per environment type."""

from typing import Any

from spectragent.core.providers.browser._registrar import (
    BrowserProviderBase,
    BrowserProviderRegistrar,
)
from spectragent.core.providers.browser.base import BaseBrowserProvider
from spectragent.core.providers.session import ProviderSession

# Domain → Registrar mapping (手动构建)
PROVIDER_REGISTRARS: dict[str, Any] = {
    "browser": BrowserProviderRegistrar,
    # "desktop": DesktopProviderRegistrar,  # L2+
    # "mobile": MobileProviderRegistrar,    # L2+
}

__all__ = [
    "BaseBrowserProvider",
    "BrowserProviderBase",
    "BrowserProviderRegistrar",
    "PROVIDER_REGISTRARS",
    "ProviderSession",
]
