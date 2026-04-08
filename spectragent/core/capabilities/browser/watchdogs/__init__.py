"""Browser watchdogs — event-driven CDP automation on top of agent-cdp."""

from spectragent.core.capabilities.browser.watchdogs.events import (
    AgentFocusChangedEvent,
    BrowserErrorEvent,
    BrowserStateRequestEvent,
    ClickCoordinateEvent,
    ClickElementEvent,
    CloseTabEvent,
    DialogOpenedEvent,
    DownloadProgressEvent,
    DownloadStartedEvent,
    FileDownloadedEvent,
    GoBackEvent,
    GoForwardEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    RefreshEvent,
    ScreenshotRequestEvent,
    ScrollEvent,
    SendKeysEvent,
    SwitchTabEvent,
    TabClosedEvent,
    TabCreatedEvent,
    TargetCrashedEvent,
    TypeTextEvent,
)

__all__ = [
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
    "SwitchTabEvent",
    "CloseTabEvent",
    "ScreenshotRequestEvent",
    "BrowserStateRequestEvent",
    # Lifecycle events
    "TabCreatedEvent",
    "TabClosedEvent",
    "NavigationStartedEvent",
    "NavigationCompleteEvent",
    "TargetCrashedEvent",
    "BrowserErrorEvent",
    "AgentFocusChangedEvent",
    # Download events
    "DownloadStartedEvent",
    "DownloadProgressEvent",
    "FileDownloadedEvent",
    # Dialog events
    "DialogOpenedEvent",
]
