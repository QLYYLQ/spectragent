"""Browser event definitions — BaseEvent subclasses for watchdog communication.

Events use lightweight references instead of full DOM node objects: an
element is ``(session_id, backend_node_id)``, the pair CDP itself addresses
nodes by. A backend node id is unique only within one target, and an element
inside an out-of-process iframe lives in a target of its own, so the id alone
does not say which element is meant. ``session_id`` is ``None`` for the tab's
own session. The selector_map lookup happens at handler time in each watchdog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_cdp import BaseEvent

# ---------------------------------------------------------------------------
# Result container for BrowserStateRequestEvent
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BrowserStateSummary:
    """One watchdog's contribution to an observe cycle.

    Keyed by fully-qualified fragment key (``"browser.dom"``), valued by
    the built fragment instance. The producing watchdog builds its own
    fragment — the capability only merges the dicts, so it needs no
    per-fragment knowledge and does not change when a fragment is added.
    """

    fragments: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# Action Request Events  (Agent → Watchdog)
# ═══════════════════════════════════════════════════════════════════════════


class NavigateToUrlEvent(BaseEvent[None]):
    """Navigate the current tab to a URL."""

    url: str
    wait_until: str = "load"  # "load" | "domcontentloaded" | "networkidle" | "" (no wait)
    new_tab: bool = False
    timeout_ms: int | None = None
    # Lifecycle wait timeout (ms). When None, falls back to ``timeout_ms`` and
    # then to a 30s default. Independent from ``event_timeout`` (which gates
    # ``await event`` / handler completion, not the CDP lifecycle wait).
    lifecycle_timeout_ms: int | None = None
    event_timeout: float = 30.0


class ClickElementEvent(BaseEvent[dict[str, Any] | None]):
    """Click a DOM element by backend_node_id (resolved via selector_map)."""

    backend_node_id: int
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
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
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
    text: str
    clear: bool = True
    event_timeout: float = 60.0


class TypeAtCoordinateEvent(BaseEvent[dict[str, Any] | None]):
    """Type text into whatever sits at absolute screen coordinates.

    Separate from :class:`TypeTextEvent` for the same reason
    :class:`ClickCoordinateEvent` is separate from
    :class:`ClickElementEvent`: a vision-only agent has no selector_map,
    so there is no ``backend_node_id`` to resolve and no DOM node to
    focus. The handler clicks the coordinate to move focus there and then
    reuses the focus-relative clear + ``Input.insertText`` sequence.
    """

    coordinate_x: int
    coordinate_y: int
    text: str
    clear: bool = True
    event_timeout: float = 60.0


class DragEvent(BaseEvent[dict[str, Any] | None]):
    """Press at one coordinate, move to another, release."""

    start_x: int
    start_y: int
    end_x: int
    end_y: int
    button: str = "left"
    event_timeout: float = 30.0


class ScrollEvent(BaseEvent[None]):
    """Scroll page or a specific element."""

    direction: str  # "up" | "down" | "left" | "right"
    amount: int = 300  # pixels
    backend_node_id: int | None = None  # None = scroll viewport
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
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
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
    value: str | None = None
    text: str | None = None
    index: int | None = None
    event_timeout: float = 10.0


class ExtractContentEvent(BaseEvent[str | None]):
    """Extract a scope wholesale: page ``innerText``, or element ``outerHTML``.

    No ``goal`` field. One existed and no handler ever read it; see
    ``docs/actions_that_call_an_llm.md`` for what it was meant to be and
    what has to exist first.
    """

    backend_node_id: int | None = None
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
    event_timeout: float = 30.0


class DoneEvent(BaseEvent[None]):
    """Agent signals task completion."""

    extracted_content: str | None = None
    event_timeout: float = 5.0


# ═══════════════════════════════════════════════════════════════════════════
# Tab Management
# ═══════════════════════════════════════════════════════════════════════════
#
# There are deliberately no tab-management *events*. Opening, closing and
# focusing tabs all mutate capability-level state (the scope/watchdog
# registry and ``_default_session_id``), which a tab-scoped watchdog
# handler cannot reach. They are plain ``BrowserCapability`` methods:
# ``create_tab`` / ``close_tab`` / ``switch_tab``.


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
    """Request exactly the observation fragments this run negotiated.

    ``fragments`` carries fully-qualified fragment keys. Every fragment
    watchdog answers only when its own ``provides`` intersects the set,
    so a vision-only agent that negotiated ``browser.screenshot`` but not
    ``browser.dom`` never pays for a DOM extraction.

    This replaced fixed ``include_dom`` / ``include_screenshot`` flags:
    those made the event schema one of the places a new fragment had to
    edit, and they were computed from the capability's *full* provides
    set rather than the negotiated one — which cancelled out the entire
    cost-saving purpose of negotiation.
    """

    fragments: frozenset[str] = frozenset()
    event_timeout: float = 30.0


# ═══════════════════════════════════════════════════════════════════════════
# CDP Page-domain Lifecycle Events  (CDP → CDPEventBridge → EventScope)
# ═══════════════════════════════════════════════════════════════════════════
#
# These are bridged from CDP messages by ``BrowserCapability._attach_tab``:
#
#   Page.loadEventFired       → PageLoadEvent
#   Page.domContentEventFired → DOMContentLoadedEvent
#   Page.lifecycleEvent       → LifecycleEvent
#
# ``Page.enable`` and ``Page.setLifecycleEventsEnabled`` must be sent per
# session for any of these to fire — that wiring also lives in _attach_tab.
# Consumers (currently ``ActionWatchdog._on_navigate``) wait on these to
# make ``NavigateToUrlEvent.wait_until`` actually mean something.


class PageLoadEvent(BaseEvent[None]):
    """``Page.loadEventFired`` — main resource and all sub-resources loaded."""

    frame_id: str | None = None
    loader_id: str | None = None
    timestamp: float | None = None


class DOMContentLoadedEvent(BaseEvent[None]):
    """``Page.domContentEventFired`` — DOMContentLoaded fired on the page."""

    frame_id: str | None = None
    loader_id: str | None = None
    timestamp: float | None = None


class LifecycleEvent(BaseEvent[None]):
    """``Page.lifecycleEvent`` — ``name`` is the lifecycle phase.

    Common values: ``"init"``, ``"DOMContentLoaded"``, ``"load"``,
    ``"networkAlmostIdle"``, ``"networkIdle"``, ``"firstPaint"``,
    ``"firstContentfulPaint"``, ``"firstMeaningfulPaint"``.
    """

    name: str
    frame_id: str | None = None
    loader_id: str | None = None
    timestamp: float | None = None


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
# Dialog Events
# ═══════════════════════════════════════════════════════════════════════════


class DialogOpenedEvent(BaseEvent[None]):
    """JavaScript dialog opened (Page.javascriptDialogOpening)."""

    dialog_type: str = ""  # "alert" | "confirm" | "prompt" | "beforeunload"
    message: str = ""
    url: str = ""
    default_prompt: str = ""
