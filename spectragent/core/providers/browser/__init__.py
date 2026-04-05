"""Browser provider infrastructure.

Concrete providers are auto-registered via conscribe when their modules are
imported.  Use ``discover("spectragent.core.providers.browser")`` to
trigger discovery of all providers, or import individual modules directly.
"""

from spectragent.core.providers.browser._registrar import (
    BrowserProviderBase,
    BrowserProviderRegistrar,
)
from spectragent.core.providers.browser.base import BaseBrowserProvider

__all__ = [
    "BaseBrowserProvider",
    "BrowserProviderBase",
    "BrowserProviderRegistrar",
]
