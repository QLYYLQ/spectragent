"""Browser capability backends — CDP-providing infrastructure.

A *backend* is the execution-layer adapter for a capability: it owns the
session lifecycle (``open``/``close``) and exposes a transport endpoint
(here, a CDP WebSocket URL) that the capability connects to. Each backend
is a Protocol-conforming implementation of :class:`BaseBrowserProvider`,
auto-registered into :data:`BrowserProviderRegistrar` on import.

The :class:`BrowserCapability` declares ``__wiring__["backend"] =
"browser_provider"`` (Mode 1 auto-discovery); ``ConfigComposer`` then
injects the chosen provider instance via setattr — same IoC pattern as
``env.capabilities`` and ``agent.llm``.

Concrete providers (``local_chrome``, ``lexmount``, ``skyvern_lite``, ...)
auto-register on import. Use
``discover("spectragent.core.capabilities.browser.backends")`` to trigger
discovery of all providers, or import individual modules directly.
"""

from spectragent.core.capabilities.browser.backends._registrar import (
    BrowserProviderBase,
    BrowserProviderRegistrar,
)
from spectragent.core.capabilities.browser.backends.base import BaseBrowserProvider

__all__ = [
    "BaseBrowserProvider",
    "BrowserProviderBase",
    "BrowserProviderRegistrar",
]
