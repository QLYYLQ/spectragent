"""Browser capability package.

Self-contained CDP-based browser automation capability. Owns its entire
mid-layer protocol stack (CDPClient, EventScope/Watchdog system, DOM
pipeline, profiles) and exposes the ``BrowserCapability`` class to the
framework.

See docs/architecture_design.md §4.9.
"""

from spectragent.core.capabilities.browser.capability import BrowserCapability

__all__ = ["BrowserCapability"]
