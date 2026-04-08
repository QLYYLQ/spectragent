"""Browser event definitions — BaseEvent subclasses for watchdog communication.

Events use lightweight references (backend_node_id: int) instead of full DOM node
objects. The selector_map lookup happens at handler time in each watchdog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_cdp import BaseEvent


# ---------------------------------------------------------------------------
# Result container for BrowserStateRequestEvent
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BrowserStateSummary:
    """Aggregated browser state returned by DOMWatchdog + ScreenshotWatchdog."""

    dom_state: Any | None = None
    screenshot: bytes | None = None
    url: str | None = None
    page_title: str | None = None
    tabs: list[dict[str, Any]] | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Action Request Events  (Agent → Watchdog)
# ═══════════════════════════════════════════════════════════════════════════


class NavigateToUrlEvent(BaseEvent[None]):
    """Navigate the current tab to a URL."""

    url: str
    wait_until: str = "load"
    new_tab: bool = False
    timeout_ms: int | None = None
    event_timeout: float = 30.0


class ClickElementEvent(BaseEvent[dict[str, Any] | None]):
    """Click a DOM element by backend_node_id (resolved via selector_map)."""

    backend_node_id: int
    button: str = "left"
    click_count: int = 1
    event_timeout: float = 15.0


class ClickCoordinateEvent(BaseEvent[dict[str, Any] | None]):
    """Click at absolute screen coordinates."""

    coordinate_x: int
    coordinate_y: int
    button: str = "left"
    click_count: int = 1
    event_timeout: float = 15.0


class TypeTextEvent(BaseEvent[dict[str, Any] | None]):
    """Type text into a DOM element."""

    backend_node_id: int
    text: str
    clear: bool = True
    event_timeout: float = 60.0


class ScrollEvent(BaseEvent[None]):
    """Scroll page or a specific element."""

    direction: str  # "up" | "down" | "left" | "right"
    amount: int = 300  # pixels
    backend_node_id: int | None = None  # None = scroll viewport
    event_timeout: float = 8.0


class SendKeysEvent(BaseEvent[None]):
    """Send keyboard keys (e.g. Enter, Escape, Ctrl+A)."""

    keys: str
    event_timeout: float = 60.0


class GoBackEvent(BaseEvent[None]):
    """Navigate back in browser history."""

    event_timeout: float = 15.0


class GoForwardEvent(BaseEvent[None]):
    """Navigate forward in browser history."""

    event_timeout: float = 15.0


class RefreshEvent(BaseEvent[None]):
    """Refresh the current page."""

    event_timeout: float = 15.0


class SelectDropdownEvent(BaseEvent[None]):
    """Select an option from a dropdown element."""

    backend_node_id: int
    value: str | None = None
    text: str | None = None
    index: int | None = None
    event_timeout: float = 10.0


class ExtractContentEvent(BaseEvent[str | None]):
    """Extract text content from the page or a specific element."""

    backend_node_id: int | None = None
    goal: str | None = None
    event_timeout: float = 30.0


class DoneEvent(BaseEvent[None]):
    """Agent signals task completion."""

    extracted_content: str | None = None
    event_timeout: float = 5.0


# ═══════════════════════════════════════════════════════════════════════════
# Tab Management Events
# ═══════════════════════════════════════════════════════════════════════════


class SwitchTabEvent(BaseEvent[str | None]):
    """Switch agent focus to a different tab."""

    target_id: str | None = None  # None = switch to most recent
    event_timeout: float = 10.0


class CloseTabEvent(BaseEvent[None]):
    """Close a specific tab."""

    target_id: str
    event_timeout: float = 10.0


# ═══════════════════════════════════════════════════════════════════════════
# Observation Events
# ═══════════════════════════════════════════════════════════════════════════


class ScreenshotRequestEvent(BaseEvent[bytes]):
    """Request a screenshot of the current page."""

    full_page: bool = False
    clip: dict[str, float] | None = None  # {x, y, width, height}
    format: str = "png"  # "png" | "jpeg" | "webp"
    quality: int | None = None  # jpeg/webp quality 0-100
    event_timeout: float = 15.0


class BrowserStateRequestEvent(BaseEvent[BrowserStateSummary]):
    """Request full browser state (DOM + screenshot + metadata).

    DOMWatchdog and ScreenshotWatchdog both handle this event.
    Results are aggregated by the session layer.
    """

    include_dom: bool = True
    include_screenshot: bool = True
    event_timeout: float = 30.0


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle Notification Events  (Watchdog → System)
# ═══════════════════════════════════════════════════════════════════════════


class TabCreatedEvent(BaseEvent[None]):
    """A new browser tab was detected (Target.attachedToTarget)."""

    target_id: str
    url: str = ""


class TabClosedEvent(BaseEvent[None]):
    """A browser tab was closed (Target.detachedFromTarget)."""

    target_id: str


class NavigationStartedEvent(BaseEvent[None]):
    """Page navigation started."""

    target_id: str
    url: str = ""
    loader_id: str | None = None


class NavigationCompleteEvent(BaseEvent[None]):
    """Page navigation completed."""

    target_id: str
    url: str = ""
    error_text: str | None = None  # Page.navigate errorText (not HTTP status code)


class TargetCrashedEvent(BaseEvent[None]):
    """A browser target crashed."""

    target_id: str
    error: str = ""


class BrowserErrorEvent(BaseEvent[None]):
    """General browser error."""

    error_type: str
    message: str
    details: dict[str, Any] | None = None


class AgentFocusChangedEvent(BaseEvent[None]):
    """Agent focus switched to a different tab."""

    target_id: str
    url: str = ""
    reason: str = ""  # "user_switch" | "crash_recovery" | "new_tab"


# ═══════════════════════════════════════════════════════════════════════════
# Download Events
# ═══════════════════════════════════════════════════════════════════════════


class DownloadStartedEvent(BaseEvent[None]):
    """File download started (Browser.downloadWillBegin)."""

    guid: str
    url: str
    suggested_filename: str = ""


class DownloadProgressEvent(BaseEvent[None]):
    """File download progress update (Browser.downloadProgress)."""

    guid: str
    received_bytes: int = 0
    total_bytes: int = 0
    state: str = ""  # "inProgress" | "completed" | "canceled"


class FileDownloadedEvent(BaseEvent[None]):
    """File download completed."""

    guid: str | None = None
    url: str = ""
    path: str = ""
    file_name: str = ""
    file_size: int = 0


# ═══════════════════════════════════════════════════════════════════════════
# Dialog Events
# ═══════════════════════════════════════════════════════════════════════════


class DialogOpenedEvent(BaseEvent[None]):
    """JavaScript dialog opened (Page.javascriptDialogOpening)."""

    dialog_type: str = ""  # "alert" | "confirm" | "prompt" | "beforeunload"
    message: str = ""
    url: str = ""
    default_prompt: str = ""
