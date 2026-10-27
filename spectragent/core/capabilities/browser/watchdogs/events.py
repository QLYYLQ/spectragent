"""Browser event definitions — BaseEvent subclasses for watchdog communication.

Events use lightweight references instead of full DOM node objects: an
element is ``(session_id, backend_node_id)``, the pair CDP itself addresses
nodes by. A backend node id is unique only within one target, and an element
inside an out-of-process iframe lives in a target of its own, so the id alone
does not say which element is meant. ``session_id`` is ``None`` for the tab's
own session. The selector_map lookup happens at handler time in each watchdog.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_cdp import BaseEvent
from pydantic import Field as PydanticField
from pydantic import model_validator

# ---------------------------------------------------------------------------
# Result container for BrowserStateRequestEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentStamp:
    """Which document a piece of evidence describes: the main frame's loader + URL.

    A new navigation gets a new ``loader_id``; a same-document change
    (``pushState``, a fragment) keeps it. M18 found ``browser_meta``'s URL
    about 0.25 s older than the DOM and screenshot of the same cycle, and
    nothing in the capture recorded a loader id to notice it (§4.14).
    """

    loader_id: str | None = None
    url: str | None = None


def documents_disagree(stamps: Mapping[str, DocumentStamp]) -> bool:
    """Whether a cycle's stamps name more than one document.

    Only stamps that know their ``loader_id`` are compared, and only when
    there are at least two of them: a watchdog (or a test double) that
    reports no stamp has nothing to disagree with.
    """
    known = {stamp.loader_id for stamp in stamps.values() if stamp.loader_id}
    counted = sum(1 for stamp in stamps.values() if stamp.loader_id)
    return counted >= 2 and len(known) > 1


def describe_stamps(stamps: Mapping[str, DocumentStamp]) -> str:
    """``handler → loader_id url`` for every stamp, for an error message."""
    return "; ".join(
        f"{handler} → loader {stamp.loader_id or '?'} {stamp.url or ''}".rstrip()
        for handler, stamp in stamps.items()
    )


@dataclass(slots=True)
class BrowserStateSummary:
    """One watchdog's contribution to an observe cycle.

    Keyed by fully-qualified fragment key (``"browser.dom"``), valued by
    the built fragment instance. The producing watchdog builds its own
    fragment — the capability only merges the dicts, so it needs no
    per-fragment knowledge and does not change when a fragment is added.

    ``document`` is the watchdog's stamp of the document it observed, when
    it knows one. The capability compares the stamps of one cycle and
    re-fires a cycle whose evidence describes different documents.
    """

    fragments: dict[str, Any] = field(default_factory=dict)
    document: DocumentStamp | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Action Request Events  (Agent → Watchdog)
# ═══════════════════════════════════════════════════════════════════════════


#: How long a navigation may take to commit, and then to reach its ``wait_until``
#: phase, when the event names no timeout (ms).
DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000
#: How much longer than its two waits together the caller of a navigation waits (s):
#: room for the handler's queue wait before them and its bookkeeping after.
NAVIGATION_EVENT_MARGIN_S = 10.0


class NavigateToUrlEvent(BaseEvent[dict[str, Any]]):
    """Navigate the current tab to a URL: wait for the commit, then for ``wait_until``.

    ``ActionWatchdog._on_navigate`` waits twice, one after the other:

    - **commit** — Chrome answers ``Page.navigate`` once the navigation
      committed or failed (a server that never answers gets no reply at all);
      at most :attr:`commit_timeout_s`. No reply in time, a command Chrome
      refuses, or an ``errorText`` in the reply fails the navigation;
    - **lifecycle** — then the committed document's ``wait_until`` phase, at
      most :attr:`lifecycle_timeout_s` from the reply. A phase that does not
      arrive in time is logged and the navigation carries on: the tab already
      shows the new document (a page whose ``load`` never comes because one
      resource never finishes is still drawn).

    The handler answers ``{"error": ...}`` when the navigation failed and
    ``{"navigated": True, "effect_facts": ...}`` otherwise (the measured
    ``NavigationEffect`` facts). ``event_timeout`` — how long ``await event`` waits, and
    ``execution_timeout`` — the separate handler budget — both default to
    the two waits plus :data:`NAVIGATION_EVENT_MARGIN_S`. The caller's wait
    includes queue time; the execution budget starts when its handler starts.
    """

    url: str
    wait_until: str = "load"  # "load" | "domcontentloaded" | "networkidle" | "" (no wait)
    new_tab: bool = False
    #: How long the navigation may take to commit (ms); ``None``: 30 s.
    timeout_ms: int | None = None
    #: How long the committed document may take to reach ``wait_until`` (ms);
    #: ``None``: ``timeout_ms``, else 30 s.
    lifecycle_timeout_ms: int | None = None

    @property
    def commit_timeout_s(self) -> float:
        """The commit wait's bound: ``timeout_ms``, else 30 s — in seconds."""
        ms = self.timeout_ms if self.timeout_ms is not None else DEFAULT_NAVIGATION_TIMEOUT_MS
        return ms / 1000

    @property
    def lifecycle_timeout_s(self) -> float:
        """The lifecycle wait's bound: ``lifecycle_timeout_ms``, else ``timeout_ms``, else 30 s."""
        ms = self.lifecycle_timeout_ms
        if ms is None:
            ms = self.timeout_ms if self.timeout_ms is not None else DEFAULT_NAVIGATION_TIMEOUT_MS
        return ms / 1000

    @model_validator(mode="after")
    def _outlast_both_waits(self) -> NavigateToUrlEvent:
        # Derived only when the emitter did not choose one (``None`` there
        # still means "wait indefinitely", as for every agent-cdp event).
        if "event_timeout" not in self.model_fields_set:
            self.event_timeout = (
                self.commit_timeout_s + self.lifecycle_timeout_s + NAVIGATION_EVENT_MARGIN_S
            )
        if "execution_timeout" not in self.model_fields_set:
            self.execution_timeout = (
                self.commit_timeout_s + self.lifecycle_timeout_s + NAVIGATION_EVENT_MARGIN_S
            )
        return self


class ClickElementEvent(BaseEvent[dict[str, Any] | None]):
    """Click a DOM element by backend_node_id (resolved via selector_map)."""

    backend_node_id: int
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
    button: str = "left"
    click_count: int = 1
    event_timeout: float = 15.0
    execution_timeout: float = 15.0


class ClickCoordinateEvent(BaseEvent[dict[str, Any] | None]):
    """Click at absolute screen coordinates."""

    coordinate_x: int
    coordinate_y: int
    button: str = "left"
    click_count: int = 1
    event_timeout: float = 15.0
    execution_timeout: float = 15.0


class TypeTextEvent(BaseEvent[dict[str, Any] | None]):
    """Type text into a DOM element."""

    backend_node_id: int
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
    text: str
    clear: bool = True
    event_timeout: float = 60.0
    execution_timeout: float = 60.0


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
    execution_timeout: float = 60.0


class DragEvent(BaseEvent[dict[str, Any] | None]):
    """Press at one coordinate, move to another, release."""

    start_x: int
    start_y: int
    end_x: int
    end_y: int
    button: str = "left"
    event_timeout: float = 30.0
    execution_timeout: float = 30.0


class ScrollEvent(BaseEvent[dict[str, Any] | None]):
    """Scroll page or a specific element; the handler answers what moved (``effect_facts``)."""

    direction: str  # "up" | "down" | "left" | "right"
    amount: int = 300  # pixels
    backend_node_id: int | None = None  # None = scroll viewport
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
    event_timeout: float = 8.0
    execution_timeout: float = 8.0


class SendKeysEvent(BaseEvent[dict[str, Any] | None]):
    """Send keyboard keys (e.g. Enter, Escape, Ctrl+A)."""

    keys: str
    event_timeout: float = 60.0
    execution_timeout: float = 60.0


class GoBackEvent(BaseEvent[dict[str, Any]]):
    """Navigate back in browser history; answered ``{"navigated": True, "effect_facts": ...}``."""

    event_timeout: float = 15.0
    execution_timeout: float = 15.0


class GoForwardEvent(BaseEvent[dict[str, Any]]):
    """Navigate forward in browser history; answered ``{"navigated": True, "effect_facts": ...}``."""

    event_timeout: float = 15.0
    execution_timeout: float = 15.0


class RefreshEvent(BaseEvent[dict[str, Any]]):
    """Reload the current page; answered ``{"navigated": True, "effect_facts": ...}``.

    ``ignore_cache``: reload ignoring cached content (the browser's Hard reload).
    """

    ignore_cache: bool = False
    event_timeout: float = 15.0
    execution_timeout: float = 15.0


class SelectDropdownEvent(BaseEvent[dict[str, Any]]):
    """Select an option from a dropdown element; answered ``{"selected": True, "effect_facts": ...}``."""

    backend_node_id: int
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
    value: str | None = None
    text: str | None = None
    index: int | None = None
    event_timeout: float = 10.0
    execution_timeout: float = 10.0


class ExtractContentEvent(BaseEvent[str | None]):
    """Extract a scope wholesale: page ``innerText``, or element ``outerHTML``.

    The element's markup comes back with sensitive values withheld
    (``dom/sensitivity.withhold_sensitive_markup``).

    No ``goal`` field. One existed and no handler ever read it; see
    ``docs/actions_that_call_an_llm.md`` for what it was meant to be and
    what has to exist first.
    """

    backend_node_id: int | None = None
    #: CDP session that owns ``backend_node_id``; ``None`` = the tab's own.
    session_id: str | None = None
    event_timeout: float = 30.0
    execution_timeout: float = 30.0


class DoneEvent(BaseEvent[None]):
    """Agent signals task completion."""

    extracted_content: str | None = None
    event_timeout: float = 5.0
    execution_timeout: float = 5.0


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
    execution_timeout: float = 15.0


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
    #: The screenshot resolver answers before every other fragment of this
    #: capture. Set only for a restored capture whose media alignment left an
    #: element the recording showed playing at its target, playing
    #: (§4.14 "媒体对齐"): a tab captures its fragments one after another, and
    #: a playing video moves on through a DOM capture taken first (measured:
    #: 0.6 s at 5,000 elements, 3.5 s at 30,000). Every other capture keeps
    #: the connection order (DOM, then screenshot).
    screenshot_first: bool = False
    event_timeout: float = 30.0
    execution_timeout: float = 30.0


class ExecuteJavaScriptEvent(BaseEvent[dict[str, Any]]):
    """Run one permitted CDP JavaScript command for the named tab session.

    The observation scope emits this event, but its handler runs on that
    tab's separate JS execution scope.  A request may therefore be awaited
    from a queued DOM observation handler without waiting on its own queue.
    The JS watchdog checks ``session_id`` against the connection's tab before
    sending anything to CDP.
    """

    method: str
    params: dict[str, Any] = PydanticField(default_factory=dict)
    #: Tab that owns the observation scope and connection.
    session_id: str
    #: OOP iframe target discovered under this tab, if different.
    target_session_id: str | None = None
    event_timeout: float = 15.0
    execution_timeout: float = 15.0


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


# ═══════════════════════════════════════════════════════════════════════════
# Dialog Events
# ═══════════════════════════════════════════════════════════════════════════


class DialogOpenedEvent(BaseEvent[None]):
    """JavaScript dialog opened (Page.javascriptDialogOpening)."""

    dialog_type: str = ""  # "alert" | "confirm" | "prompt" | "beforeunload"
    message: str = ""
    url: str = ""
    default_prompt: str = ""
