"""Browser environment core -- CDP, DOM, watchdogs, and session management."""

from spectragent.core.browser.cdp_client import CDPClient
from spectragent.core.browser.profiles import (
    BUILTIN_PROFILES,
    BrowserProfileConfig,
    resolve_profile,
)
from spectragent.core.browser.session import BrowserSession

__all__ = [
    "BrowserProfileConfig",
    "BrowserSession",
    "BUILTIN_PROFILES",
    "CDPClient",
    "resolve_profile",
]
