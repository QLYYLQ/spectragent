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
from spectragent.core.capabilities.browser.watchdogs.crash_watchdog import (
    CrashWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.dom_watchdog import DOMWatchdog
from spectragent.core.capabilities.browser.watchdogs.events import (
    AgentFocusChangedEvent,
    BrowserErrorEvent,
    BrowserStateRequestEvent,
    ClickCoordinateEvent,
    ClickElementEvent,
    DialogOpenedEvent,
    DOMContentLoadedEvent,
    ExecuteJavaScriptEvent,
    GoBackEvent,
    GoForwardEvent,
    LifecycleEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    PageLoadEvent,
    RefreshEvent,
    ScreenshotRequestEvent,
    ScrollEvent,
    SendKeysEvent,
    TabClosedEvent,
    TabCreatedEvent,
    TargetCrashedEvent,
    TypeTextEvent,
)
from spectragent.core.capabilities.browser.watchdogs.js_execution import (
    JavaScriptExecutionWatchdog,
)
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
    "CrashWatchdog",
    "DOMWatchdog",
    "JavaScriptExecutionWatchdog",
    "PopupsWatchdog",
    "ScreenshotWatchdog",
    "SecurityWatchdog",
    # Action request events
    "NavigateToUrlEvent",
    "ClickElementEvent",
    "ClickCoordinateEvent",
    "TypeTextEvent",
    "ScrollEvent",
    "SendKeysEvent",
    "GoBackEvent",
    "GoForwardEvent",
    "RefreshEvent",
    "ScreenshotRequestEvent",
    "BrowserStateRequestEvent",
    "ExecuteJavaScriptEvent",
    # CDP Page-domain bridged events
    "PageLoadEvent",
    "DOMContentLoadedEvent",
    "LifecycleEvent",
    # Lifecycle events
    "TabCreatedEvent",
    "TabClosedEvent",
    "NavigationStartedEvent",
    "NavigationCompleteEvent",
    "TargetCrashedEvent",
    "BrowserErrorEvent",
    "AgentFocusChangedEvent",
    # Dialog events
    "DialogOpenedEvent",
]
