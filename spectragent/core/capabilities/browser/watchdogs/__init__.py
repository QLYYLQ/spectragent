"""Browser watchdogs — event-driven CDP automation on top of agent-cdp.

Importing this package registers every watchdog under ``agent_cdp``'s
``WatchdogRegistrar``. ``BrowserCapability`` imports it for exactly that
side effect: it looks watchdogs up by registry key and never names a
class, so adding a watchdog here is the whole change.
"""

from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
    ActionWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.archive_watchdog import (
    ArchiveWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
    browser_watchdog_classes,
    fragments_provided_by,
    validate_watchdog_names,
)
from spectragent.core.capabilities.browser.watchdogs.browser_meta_watchdog import (
    BrowserMetaWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.capture_state_watchdog import (
    CaptureStateWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.crash_watchdog import (
    CrashWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.dom_watchdog import DOMWatchdog
from spectragent.core.capabilities.browser.watchdogs.js_execution import (
    JavaScriptExecutionWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.platform_watchdog import PlatformWatchdog
from spectragent.core.capabilities.browser.watchdogs.popups_watchdog import (
    PopupsWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.screenshot_watchdog import (
    ScreenshotWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.security_watchdog import (
    SecurityWatchdog,
)

__all__ = [
    # Registrar contract
    "BrowserWatchdog",
    "WatchdogContext",
    "WatchdogLevel",
    "browser_watchdog_classes",
    "fragments_provided_by",
    "validate_watchdog_names",
    # Watchdogs
    "ActionWatchdog",
    "ArchiveWatchdog",
    "BrowserMetaWatchdog",
    "CaptureStateWatchdog",
    "CrashWatchdog",
    "DOMWatchdog",
    "JavaScriptExecutionWatchdog",
    "PopupsWatchdog",
    "PlatformWatchdog",
    "ScreenshotWatchdog",
    "SecurityWatchdog",
]
