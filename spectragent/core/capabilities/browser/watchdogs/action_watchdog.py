"""ActionWatchdog — executes browser actions via CDP Input/Page/DOM domains.

Listens to action events (Click, Type, Scroll, Navigate, ...) and translates
them into CDP commands.  Resolves an element ``(session_id, backend_node_id)``
→ element coordinates through the selector map ``DOMWatchdog`` publishes to
the capability (``WatchdogContext.selector_map_getter``).

Elements in frames. DOM commands about an element (scroll into view, measure,
focus, select, read) go to the session that owns it — the tab's own for the
page and its same-process frames, the frame target's for an out-of-process
iframe. Mouse input never does: it is dispatched on the tab's session at page
CSS coordinates, where Chrome hit-tests it into whichever frame is under the
point, exactly as a real pointer would be. The page point of a frame element
is measured from layout (``DOM.getContentQuads``), not by script in its frame,
lifted to the page at each out-of-process boundary by that frame's
:class:`~spectragent.core.capabilities.browser.geometry.FrameProjection` — the
affine map the owner's box model describes, so a frame under a CSS transform
lands where it is drawn and not where an untransformed one would be. A frame
element that can no longer be measured is not acted on at its snapshot
position: its frame changed, and that point belongs to something else.

Where a box becomes a point. Measuring gives a box; clicking needs a point,
and this module is where that choice is made — :meth:`ActionWatchdog._aim_inside`
is the one seam, used by every path that turns a measured element into a
coordinate (click, the cursor approach before type/select, the wheel's aim
point). The *policy* is the trajectory strategy's (``aim_offset_px``, so it
swaps with ``trajectory_type`` and its knobs are config like every other
humanization number); the seam is here because a trajectory's contract is that
its path ends *exactly* on the point it was given and is never clamped. If a
strategy moved the endpoint itself, nothing downstream — the trace, the
overlay, the result — would agree on where the click went. So the requested
point carries the offset and every layer below sees one exact point.

The seam is also the only place that knows how big the window is, so it is
where the offset is clipped: a measured box is the element's *whole* border
box, which can be larger than the viewport or hang off it, and a point outside
the viewport is not a click on the element — it is a click Chrome discards
while the action reports success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar

from agent_cdp import (
    CDPCommandProtocol,
    ConnectionType,
    EventScope,
    Participation,
    ResultContract,
    expect,
)
from agent_cdp.events.base import BaseEvent

from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMSelectorMap
from spectragent.core.capabilities.browser.dom.sensitivity import (
    is_sensitive_control,
    withhold_sensitive_markup,
)
from spectragent.core.capabilities.browser.effect_probes import (
    DEEP_FOCUS_JS,
    SELECT_FACTS_JS,
    UNKNOWN,
    FieldProbe,
    ProbeBudget,
    current_location,
    document_loaded,
    document_loader,
    element_at,
    focused_element,
    history_entry_committed,
    history_location,
    release_object,
    reload_committed,
    resolve_object,
    selection_facts,
)
from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    ShortcutOfAnotherPlatform,
    ShortcutWithoutUi,
    ViewportPoint,
)
from spectragent.core.capabilities.browser.geometry import (
    FrameGeometryError,
    FrameProjection,
    bounding_box,
    corners,
)
from spectragent.core.capabilities.browser.input import (
    HumanKeyboard,
    HumanMouse,
    MouseTrajectoryProtocol,
    TypingRhythmProtocol,
)
from spectragent.core.capabilities.browser.input.chords import KeyChord, parse_key_chord
from spectragent.core.capabilities.browser.input.mouse import PointerPosition
from spectragent.core.capabilities.browser.input.shortcuts import (
    OtherPlatform,
    WithoutUi,
    browser_shortcut,
)
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.capabilities.browser.scroll_probe import ScrollProbe
from spectragent.core.capabilities.browser.sessions import TabSessions
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
from spectragent.core.capabilities.browser.watchdogs.contracts import action_result
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserErrorEvent,
    ClickCoordinateEvent,
    ClickElementEvent,
    DOMContentLoadedEvent,
    DoneEvent,
    DragEvent,
    ExtractContentEvent,
    GoBackEvent,
    GoForwardEvent,
    LifecycleEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    PageLoadEvent,
    RefreshEvent,
    ScrollEvent,
    SelectDropdownEvent,
    SendKeysEvent,
    TypeAtCoordinateEvent,
    TypeTextEvent,
)
from spectragent.schemas.action import Unmeasured

logger = logging.getLogger(__name__)

#: The element that receives keystrokes, as the sensitivity rule reads it:
#: its tag and the four attributes the rule looks at. Follows the focus
#: through open shadow roots and frames this document can reach; a frame it
#: cannot look into (another process, another origin) is reported opaque.
_FOCUSED_FIELD_JS = (
    "(() => {"
    + DEEP_FOCUS_JS
    + """
  const f = deepFocus();
  if (f.opaque) return {opaque: true};
  const el = f.el;
  if (!el) return null;
  const attrs = {};
  for (const name of ['type', 'autocomplete', 'id', 'name']) {
    const value = el.getAttribute(name);
    if (value !== null) attrs[name] = value;
  }
  return {tag: el.tagName, attrs};
})()"""
)


def _focused_field_verdict(reply: Any) -> bool | None:
    """The sensitivity of the focused element a ``_FOCUSED_FIELD_JS`` reply describes.

    ``True`` / ``False`` by the shared rule (``dom/sensitivity.py``); nothing
    focused is ``False`` (the text goes into no field); ``None`` when the
    reply cannot tell — the evaluation threw, the reply is malformed, or the
    focus sits in a frame this session cannot look into.
    """
    if not isinstance(reply, dict) or reply.get("exceptionDetails"):
        return None
    result = reply.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if value is None:
        return False if result.get("subtype") == "null" else None
    if not isinstance(value, dict) or value.get("opaque"):
        return None
    attrs = value.get("attrs")
    return is_sensitive_control(
        str(value.get("tag") or ""),
        {str(k).lower(): str(v) for k, v in attrs.items()} if isinstance(attrs, dict) else {},
    )


#: How often the focused element is read again while typing waits for focus
#: to land on a text field (``BrowserCapability.type_focus_wait_s``).
FOCUS_POLL_S = 0.05


def _no_text_entry(field: FieldProbe) -> str | None:
    """Why the focus *field* read takes no typed text, or ``None``.

    ``None`` when it does (a text field, or a ``textbox`` widget that takes
    the keys itself), and when the reading cannot tell (it gave no answer, a
    frame of another site, an element whose own reading failed, a closed
    shadow root, embedded content): only a measured "nothing focused" or "no
    text control" stops a typing action.
    """
    if field.target == "nothing":
        return "nothing has focus"
    if not isinstance(field.target, ElementFacts):
        return None
    if field.entry == "read-only":
        return f"focus is on {field.target.text()}, which is read-only"
    if field.entry == "none":
        return f"focus is on {field.target.text()}, which does not take typed text"
    return None


def _inside_sensitive_control(node: DOMElementNode | None) -> bool:
    """Whether *node* lies inside a sensitive control, by its ancestors in the observation tree.

    An ``<option>`` of a sensitive ``<select>``, or a node of a control's
    shadow root: its own markup names no control, yet every value in it is
    the control's. The tree's parent links cross shadow roots and frame
    documents the way the BrowserUse cleaner passes its flag down. ``False``
    for a node the selector map no longer holds (its ancestors are unknown).
    """
    ancestor = node.parent if node is not None else None
    while ancestor is not None:
        if ancestor.node_type == 1 and is_sensitive_control(
            ancestor.tag_name, {str(k).lower(): str(v) for k, v in ancestor.attributes.items()}
        ):
            return True
        ancestor = ancestor.parent
    return False


#: What a click on a frame element that can no longer be located reports.
_STALE_FRAME_ELEMENT = (
    "The element is inside a frame that changed since the last observation "
    "(navigated, reloaded or removed), so it cannot be located; observe the "
    "page again before acting on it."
)

#: What a press on an element that stays outside the viewport reports.
_OUTSIDE_VIEWPORT = (
    "The element's centre lies outside the viewport and scrolling does not bring "
    "it into view (it may be hidden off-screen, such as a closed panel), so a "
    "press there would reach nothing; observe the page again and pick a visible "
    "element."
)

#: Scroll an element so its centre is in the middle of the viewport, at once:
#: without ``behavior: 'instant'`` a page's ``scroll-behavior: smooth`` animates
#: the scroll, and the box measured right after it is the one before.
_CENTRE_INTO_VIEW_JS = (
    "function() { this.scrollIntoView("
    "{block: 'center', inline: 'center', behavior: 'instant'}); return {}; }"
)


def _centre(box: tuple[float, float, float, float]) -> tuple[float, float]:
    """The centre of an ``(x, y, width, height)`` box."""
    x, y, w, h = box
    return x + w / 2, y + h / 2


def _visible_span(centre: float, size: float, extent: int) -> float:
    """The part of a box that is on screen, kept symmetric about its centre.

    A measured border box is not clipped to the window: an element can be
    taller than the viewport, or hang off it after ``scrollIntoViewIfNeeded``
    put its *centre* in view. Aiming anywhere in such a box dispatches clicks
    at negative or off-screen coordinates, which Chrome drops while the action
    still reports success — so the box handed to the aiming policy is shrunk
    to what is actually visible.

    Symmetric (the smaller of the two reaches, doubled) rather than clipped on
    each side independently, because the offset it feeds is a *centred*
    distribution: an asymmetric window would bias the aim toward whichever
    side happens to be on screen. Returns ``0.0`` when the centre itself is
    off screen — nothing sensible to aim at, so the centre is kept exactly as
    it was before any of this existed.
    """
    reach = min(size / 2.0, centre, float(extent - 1) - centre)
    return 2.0 * reach if reach > 0.0 else 0.0


#: Live border-box rect of a top-level element, in the page viewport.
_CLIENT_RECT_JS = (
    "function() {"
    "  const r = this.getBoundingClientRect();"
    "  return {x: r.x, y: r.y, w: r.width, h: r.height};"
    "}"
)


_DEBUG_OVERLAY_JS = r"""
(function () {
  if (window.__af_overlay_installed__) return;
  window.__af_overlay_installed__ = true;

  function ensureRoot() {
    if (!document.documentElement) return null;
    let root = document.getElementById('__af_overlay_root__');
    if (root) return root;
    root = document.createElement('div');
    root.id = '__af_overlay_root__';
    root.setAttribute('aria-hidden', 'true');
    root.setAttribute('data-af-overlay', '1');
    root.style.cssText =
      'position:fixed;left:0;top:0;width:0;height:0;' +
      'pointer-events:none;z-index:2147483647;';
    (document.body || document.documentElement).appendChild(root);
    return root;
  }

  const cursorSVG =
    '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" ' +
    'style="width:100%;height:100%;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.7));">' +
    '<path d="M3 2 L3 19 L8.5 14.5 L11.5 21.5 L14.5 20.3 L11.5 13.5 L19 13.5 Z" ' +
    'fill="#ffffff" stroke="#000000" stroke-width="1.5" stroke-linejoin="round"/>' +
    '</svg>';

  function ensureCursor() {
    const root = ensureRoot();
    if (!root) return null;
    let c = document.getElementById('__af_cursor__');
    if (c) return c;
    c = document.createElement('div');
    c.id = '__af_cursor__';
    c.setAttribute('data-af-overlay', '1');
    c.style.cssText =
      'position:fixed;width:48px;height:48px;' +
      'left:50vw;top:50vh;' +
      'pointer-events:none;z-index:2147483647;' +
      'transition:left 220ms ease-out, top 220ms ease-out;' +
      'transform:translate(-4px,-4px);';
    c.innerHTML = cursorSVG;
    root.appendChild(c);
    return c;
  }

  window.__af_move_cursor__ = function (x, y) {
    const c = ensureCursor();
    if (!c) return;
    c.style.left = x + 'px';
    c.style.top = y + 'px';
  };

  window.__af_show_click__ = function (x, y, label) {
    const root = ensureRoot();
    if (!root) return;
    const oldM = document.getElementById('__af_click_marker__');
    if (oldM) oldM.remove();
    const oldL = document.getElementById('__af_click_label__');
    if (oldL) oldL.remove();
    const oldD = document.getElementById('__af_click_dot__');
    if (oldD) oldD.remove();

    // Outer pulsing ring — large and obvious.
    const m = document.createElement('div');
    m.id = '__af_click_marker__';
    m.setAttribute('data-af-overlay', '1');
    m.style.cssText =
      'position:fixed;width:64px;height:64px;border-radius:50%;' +
      'border:6px solid #ff3300;background:rgba(255,51,0,0.18);' +
      'box-shadow:' +
      '  0 0 0 4px rgba(255,51,0,0.45),' +
      '  0 0 32px 8px rgba(255,51,0,0.65),' +
      '  inset 0 0 0 2px rgba(255,255,255,0.85);' +
      'pointer-events:none;z-index:2147483646;' +
      'transform:translate(-32px,-32px);' +
      'left:' + x + 'px;top:' + y + 'px;';
    root.appendChild(m);

    // Inner crosshair dot — pinpoints the exact click pixel.
    const d = document.createElement('div');
    d.id = '__af_click_dot__';
    d.setAttribute('data-af-overlay', '1');
    d.style.cssText =
      'position:fixed;width:10px;height:10px;border-radius:50%;' +
      'background:#ffffff;border:2px solid #ff3300;' +
      'box-shadow:0 0 6px rgba(0,0,0,0.6);' +
      'pointer-events:none;z-index:2147483646;' +
      'transform:translate(-7px,-7px);' +
      'left:' + x + 'px;top:' + y + 'px;';
    root.appendChild(d);

    if (label) {
      const l = document.createElement('div');
      l.id = '__af_click_label__';
      l.setAttribute('data-af-overlay', '1');
      l.style.cssText =
        'position:fixed;background:rgba(255,51,0,0.96);color:#fff;' +
        'padding:8px 14px;border-radius:6px;' +
        'font:bold 16px/1.25 -apple-system,Segoe UI,Roboto,sans-serif;' +
        'pointer-events:none;z-index:2147483646;white-space:nowrap;' +
        'max-width:520px;overflow:hidden;text-overflow:ellipsis;' +
        'box-shadow:0 4px 12px rgba(0,0,0,0.55);' +
        'border:2px solid #ffffff;' +
        'transform:translate(40px,-16px);' +
        'left:' + x + 'px;top:' + y + 'px;';
      l.textContent = label;
      root.appendChild(l);
    }

    window.__af_move_cursor__(x, y);
  };

  // Visibility toggle — called by BrowserCapability around the
  // observe cycle so injected nodes don't pollute DOM scrape /
  // screenshot. Display:none also makes them invisible to the
  // browser-use cleaner's CSS visibility filter.
  window.__af_set_overlay_visible__ = function (visible) {
    const nodes = document.querySelectorAll('[data-af-overlay="1"]');
    const display = visible ? '' : 'none';
    for (let i = 0; i < nodes.length; i++) {
      nodes[i].style.display = display;
    }
  };
})();
"""


class ActionWatchdog(BrowserWatchdog):
    """Executes browser actions via CDP."""

    level: ClassVar[WatchdogLevel] = "tab"

    @classmethod
    def build(cls, ctx: WatchdogContext) -> ActionWatchdog:
        if ctx.input_platform is None:
            raise ValueError("ActionWatchdog requires the measured browser input_platform")
        return cls(
            ctx.cdp,
            input_platform=ctx.input_platform,
            selector_map_getter=ctx.selector_map_getter,
            main_frame_id_getter=ctx.main_frame_id_getter,
            debug_highlight=ctx.debug_highlight,
            mouse_trajectory=ctx.mouse_trajectory,
            typing_rhythm=ctx.typing_rhythm,
            viewport=ctx.viewport,
            sessions=ctx.tab_sessions,
            pointer_position=ctx.pointer_position,
            action_effect_timeout_s=ctx.action_effect_timeout_s,
            type_focus_wait_s=ctx.type_focus_wait_s,
        )

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        input_platform: InputPlatform,
        selector_map_getter: Callable[[], DOMSelectorMap | None] | None = None,
        main_frame_id_getter: Callable[[], str | None] | None = None,
        debug_highlight: bool = False,
        debug_highlight_ms: int = 350,
        mouse_trajectory: MouseTrajectoryProtocol | None = None,
        typing_rhythm: TypingRhythmProtocol | None = None,
        viewport: tuple[int, int] = (1280, 720),
        sessions: TabSessions | None = None,
        pointer_position: PointerPosition | None = None,
        action_effect_timeout_s: float = 1.0,
        type_focus_wait_s: float = 0.5,
    ) -> None:
        self._cdp = cdp
        # How long each action may spend measuring its own effect (probe time).
        self._effect_timeout_s = action_effect_timeout_s
        # How much of that typing may spend waiting for focus to land on a
        # text field; the rest stays for the probes after the wait.
        self._focus_wait_s = type_focus_wait_s
        self._sessions = sessions
        self._selector_map_getter = selector_map_getter
        # Per-tab main frame id, used by _on_navigate to filter sub-frame
        # Page.lifecycleEvent. Optional — when None, we do not filter.
        self._main_frame_id_getter = main_frame_id_getter
        self._scope: EventScope | None = None
        self._session_id: str | None = None

        # Mouse middle layer — owns cursor state + the trajectory strategy and
        # performs every move/click/wheel. The ``instant`` strategy (also the
        # fallback when none is configured) is the legacy instant jump.
        self._mouse = HumanMouse(
            cdp,
            session_id_getter=lambda: self._session_id,
            trajectory=mouse_trajectory,
            pointer_position=pointer_position,
            viewport=viewport,
            move_callback=self._overlay_move_cursor if debug_highlight else None,
        )

        # Keystroke middle layer — owns the typing rhythm and performs every
        # text entry. Same shape and same reason as the mouse: the ``instant``
        # rhythm (also the fallback when none is configured) is the legacy
        # single bulk ``Input.insertText``.
        self._keyboard = HumanKeyboard(cdp, input_platform=input_platform, rhythm=typing_rhythm)
        # Which chords are the browser's own shortcuts (``input/shortcuts.py``).
        self._input_platform: InputPlatform = input_platform

        # The window a dispatched point has to land in — the only thing
        # ``_aim_inside`` knows that the strategy deciding the offset does not.
        self._vw, self._vh = viewport

        # Debug highlight state — Overlay.enable is sent lazily, once per
        # session it is needed on (an element in an out-of-process iframe is
        # highlighted through that frame's session).
        self._debug_highlight = debug_highlight
        self._debug_highlight_ms = debug_highlight_ms
        self._overlay_sessions: set[str | None] = set()
        # Tracks whether the JS cursor/click overlay has been registered
        # via Page.addScriptToEvaluateOnNewDocument for this session, so
        # we register it at most once per tab.
        self._overlay_script_installed = False

    def _effect_budget(self) -> ProbeBudget:
        """A fresh measurement budget for one action (``action_effect_timeout_s``)."""
        return ProbeBudget(self._effect_timeout_s)

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._scope = scope
        self._session_id = session_id

        connections = []
        for event_type, handler, contract in [
            (ClickElementEvent, self._on_click_element, action_result(marker="clicked")),
            (ClickCoordinateEvent, self._on_click_coordinate, action_result(marker="clicked")),
            (TypeTextEvent, self._on_type_text, action_result(marker="typed")),
            (TypeAtCoordinateEvent, self._on_type_at_coordinate, action_result(marker="typed")),
            (DragEvent, self._on_drag, action_result(marker="dragged")),
            (ScrollEvent, self._on_scroll, action_result(marker="scrolled")),
            (NavigateToUrlEvent, self._on_navigate, action_result(marker="navigated")),
            (SendKeysEvent, self._on_send_keys, action_result(marker="pressed")),
            (GoBackEvent, self._on_go_back, action_result(marker="navigated")),
            (GoForwardEvent, self._on_go_forward, action_result(marker="navigated")),
            (RefreshEvent, self._on_refresh, action_result(marker="navigated")),
            (SelectDropdownEvent, self._on_select_dropdown, action_result(marker="selected")),
            (
                ExtractContentEvent,
                self._on_extract_content,
                ResultContract(lambda value: isinstance(value, str), "Extraction returned no text"),
            ),
            (DoneEvent, self._on_done, action_result()),
        ]:
            connections.append(
                scope.connect(
                    event_type,
                    handler,
                    mode=ConnectionType.QUEUED,
                    participation=Participation.HANDLER,
                    receiver_key="browser.action",
                    result_contract=contract,
                )
            )
        self.track_binding(scope, session_id, connections=connections)

    # -----------------------------------------------------------------------
    # Click
    # -----------------------------------------------------------------------

    async def _on_click_element(self, event: ClickElementEvent) -> dict[str, Any] | None:
        """Click element by backend_node_id → resolve to coordinates → click.

        Click coordinates come from a live measurement (CSS viewport pixels,
        see ``_get_live_viewport_center``) rather than ``node.bounds`` to
        immunise against three known sources of drift between observe and act:

        - DPR mismatch (DOMSnapshot bounds are device pixels)
        - document vs viewport coordinate space (snapshot bounds include
          scrollOffset; CDP Input.dispatchMouseEvent expects viewport)
        - layout shifts that happened after the snapshot was taken

        For an element inside frames the live box is lifted to the page
        through the frames around it (see the module docstring), and the
        press lands on the tab at that page point. When that point cannot be
        measured the click is refused, not guessed: a frame element's
        position depends on every frame around it, so a failure there means
        the frame navigated, reloaded or went away — and the snapshot's point
        now belongs to whatever replaced it.

        The click's effect (``PointerEffect``) says whether the element hit
        under the pointer is this one or inside it: the element is held as a
        remote object of the tab and asked right before the press. An element
        inside frames is not asked (``on_target`` is ``"unavailable"``): the
        press is hit-tested on the tab, where such an element has no object.
        """
        node = self._resolve_node(event.backend_node_id, event.session_id)
        if node is None:
            return {"error": f"Element {event.backend_node_id} not in selector_map"}

        if node.bounds is None:
            return {"error": f"Element {event.backend_node_id} has no bounds"}

        await self._highlight_node(event.backend_node_id, event.session_id)

        target_w = target_h = 0.0
        owners = node.frame_owners()
        try:
            live = await self._get_live_viewport_center(
                event.backend_node_id, event.session_id, owners
            )
        except FrameGeometryError as exc:
            logger.warning("Not clicking %s: %s", self._describe_node(node), exc)
            return {"error": str(exc)}
        if live is not None:
            cx, cy, target_w, target_h = live
        elif owners:
            logger.warning(
                "Element %s inside a frame could not be located live; not clicking "
                "its snapshot position",
                self._describe_node(node),
            )
            return {"error": _STALE_FRAME_ELEMENT}
        else:
            # Fallback path: post Layer-1 bounds are CSS pixels, so this is
            # still correct for the static-page case but vulnerable to
            # layout shift / scroll between observe and act.
            logger.warning(
                "Live viewport center unavailable for node %d; falling back to snapshot bounds",
                event.backend_node_id,
            )
            cx_f, cy_f = node.bounds.center
            cx, cy = int(cx_f), int(cy_f)
            target_w, target_h = node.bounds.width, node.bounds.height

        if not self._in_viewport(cx, cy):
            # Chrome drops a press outside the viewport while the action would
            # report success; the element was scrolled as far as it goes.
            logger.warning(
                "Not clicking %s: its centre (%d, %d) stays outside the viewport",
                self._describe_node(node),
                cx,
                cy,
            )
            return {"error": _OUTSIDE_VIEWPORT}

        ax, ay = self._aim_inside(cx, cy, target_w, target_h)
        budget = self._effect_budget()
        # Not inside frames, the element belongs to the tab's own session.
        target_object_id = (
            None
            if owners
            else await resolve_object(self._cdp, self._session_id, event.backend_node_id, budget)
        )
        return await self._click_at(
            ax,
            ay,
            event.button,
            event.click_count,
            label=self._describe_node(node),
            target_w=target_w,
            target_h=target_h,
            budget=budget,
            target_object_id=target_object_id,
            on_target="unavailable" if owners else UNKNOWN,
        )

    def _aim_inside(self, cx: float, cy: float, width: float, height: float) -> tuple[int, int]:
        """Pick the point inside an element's box that this action aims at.

        A box does not say where to click, it says where it is legal to.
        Aiming at the arithmetic centre every single time is a tell no amount
        of path realism covers: measured on real pages the error was 0.78 px,
        where humans scatter 3-31 px. *Where* to aim is the trajectory
        strategy's decision (``aim_offset_px``) — it is humanization, so it
        swaps with the strategy and its knobs are that strategy's config;
        ``instant`` returns no offset at all, which is how the legacy path
        stays byte-for-byte legacy.

        This method is the seam, and it owns two things the strategy cannot
        know. First, the window: the offset is drawn inside the *visible*
        part of the box (see :func:`_visible_span`), because a border box is
        not clipped to the viewport and a point outside it is a click that
        goes nowhere while the action reports success. Second, exactness —
        the trajectory is handed the aimed point as its endpoint and still
        ends exactly there, ``_click_at`` reports it, and the overlay paints
        it: one point, agreed on by everything downstream.

        Every measured-box action comes through here — the click, the cursor
        approach before type/select, and the wheel's aim point — because they
        all carry the same tell. A raw-coordinate click does not: that point
        is the model's own aim, see :meth:`_on_click_coordinate`.
        """
        dx, dy = self._mouse.trajectory.aim_offset_px(
            _visible_span(cx, width, self._vw),
            _visible_span(cy, height, self._vh),
        )
        return round(cx + dx), round(cy + dy)

    async def _on_click_coordinate(self, event: ClickCoordinateEvent) -> dict[str, Any] | None:
        """Click at a viewport point given in CSS pixels.

        Not scattered, unlike an index click: this point is the *model's*
        aim, usually read off a screenshot. Nudging it would be overruling
        the agent's intent with our own idea of where the target is — and
        where an index click has a measured box to stay inside, here there
        is only a hit-test guess. The box we do find is for the trajectory's
        timing (``_target_box_at``), never for moving the point.
        """
        target_w, target_h = await self._target_box_at(event.coordinate_x, event.coordinate_y)
        return await self._click_at(
            event.coordinate_x,
            event.coordinate_y,
            event.button,
            event.click_count,
            label=f"({event.coordinate_x}, {event.coordinate_y})",
            target_w=target_w,
            target_h=target_h,
            budget=self._effect_budget(),
        )

    async def _target_box_at(self, x: int, y: int) -> tuple[float, float]:
        """Size of the target region under a raw point, for the trajectory.

        An index click knows its element's box; a coordinate click does not,
        and a strategy conditioned on the target size (``learned``, Fitts timing)
        would otherwise see ``0 x 0``. Hit-test the point, take the element's
        border box, and return the largest box *centred on the point* that
        stays inside it — strategies treat the endpoint as the box centre, and
        a click near an element's edge is aiming at a narrower target than the
        element.

        Only when humanized: the instant jump ignores the box, and the legacy
        coordinate click stays exactly three input events. Best-effort — any
        failure means "size unknown", never a failed click. The hit-test runs
        in this tab's session, so a point over a cross-origin iframe resolves
        to the iframe element itself.
        """
        if not self._mouse.humanized:
            return 0.0, 0.0
        sid = self._session_id
        try:
            hit = await self._cdp.send(
                "DOM.getNodeForLocation",
                {"x": x, "y": y, "ignorePointerEventsNone": True},
                session_id=sid,
            )
            backend_node_id = (hit or {}).get("backendNodeId")
            if not backend_node_id:
                return 0.0, 0.0
            box = await self._cdp.send(
                "DOM.getBoxModel",
                {"backendNodeId": backend_node_id},
                session_id=sid,
            )
            quad = ((box or {}).get("model") or {}).get("border") or []
            if len(quad) < 8:
                return 0.0, 0.0
            xs, ys = quad[0::2], quad[1::2]
            half_w = min(x - min(xs), max(xs) - x)
            half_h = min(y - min(ys), max(ys) - y)
            if half_w <= 0 or half_h <= 0:
                return 0.0, 0.0
            return float(2 * half_w), float(2 * half_h)
        except Exception as exc:
            logger.debug("hit-test at (%d, %d) failed: %s", x, y, exc)
            return 0.0, 0.0

    async def _click_at(
        self,
        x: int,
        y: int,
        button: str,
        click_count: int,
        *,
        label: str = "",
        target_w: float = 0.0,
        target_h: float = 0.0,
        budget: ProbeBudget | None = None,
        target_object_id: str | None = None,
        on_target: bool | Unmeasured | None = None,
    ) -> dict[str, Any]:
        """Execute click at (x, y) via the HumanMouse middle layer.

        Cursor movement (a single jump with the ``instant`` strategy, a full
        human trajectory otherwise) plus press/release are owned by
        ``self._mouse``.
        The returned coords are the *requested* target, unchanged.

        With a *budget* the click measures its effect (``PointerEffect``) and
        answers it as ``effect_facts``: once the cursor has arrived, right
        before the press, the element the tab hit-tests at the dispatched
        point; after the release, the focused element. *target_object_id*
        is the intended element as a remote object of the tab, for
        ``on_target``; without one, ``on_target`` is the given value
        (``None``: the click named no element). Measuring never changes the
        click: a probe that fails or runs out of the budget leaves its fact
        unknown. Without a budget (the focusing click of ``type_coordinate``)
        nothing is measured.
        """
        facts: dict[str, Any] = {
            "point": ViewportPoint(x=x, y=y),
            "hit": UNKNOWN,
            "on_target": on_target,
            "focus_after": UNKNOWN,
        }

        before_press: Callable[[int, int], Awaitable[None]] | None = None
        if budget is not None:
            probe_budget = budget

            async def hit_test(px: int, py: int) -> None:
                facts["point"] = ViewportPoint(x=px, y=py)
                hit, verdict = await element_at(
                    self._cdp,
                    self._session_id,
                    px,
                    py,
                    probe_budget,
                    target_object_id=target_object_id,
                )
                facts["hit"] = hit
                if target_object_id is not None:
                    facts["on_target"] = verdict

            before_press = hit_test

        try:
            await self._mouse.click(
                x,
                y,
                button,
                click_count,
                target_w=target_w,
                target_h=target_h,
                before_press=before_press,
            )
            await self._paint_click(x, y, label)
            answer: dict[str, Any] = {"clicked": True, "x": x, "y": y}
            if budget is not None:
                facts["focus_after"] = await focused_element(self._cdp, self._session_id, budget)
                answer["effect_facts"] = facts
            return answer
        except Exception as e:
            logger.error("Click failed at (%d, %d): %s", x, y, e)
            return {"error": str(e)}
        finally:
            if target_object_id is not None and budget is not None:
                object_id = target_object_id
                await budget.run(
                    lambda: release_object(self._cdp, self._session_id, object_id),
                    "release target",
                )

    # -----------------------------------------------------------------------
    # Type
    # -----------------------------------------------------------------------

    async def _on_type_text(self, event: TypeTextEvent) -> dict[str, Any] | None:
        """Type text into an element via CDP Input.insertText (bulk).

        Focus and keystrokes go to the session that owns the element: focusing
        a node inside an out-of-process iframe through its own frame target
        is what makes that frame the one the keystrokes reach.
        """
        node = self._resolve_node(event.backend_node_id, event.session_id)
        if node is None:
            return {"error": f"Element {event.backend_node_id} not in selector_map"}

        sid = self._element_session(event.session_id)
        await self._highlight_node(event.backend_node_id, event.session_id)
        await self._approach_node(event.backend_node_id, event.session_id)
        try:
            # Focus the element.
            await self._cdp.send(
                "DOM.focus",
                {"backendNodeId": event.backend_node_id},
                session_id=sid,
            )
            index = node.element_index
            return await self._insert_text_at_focus(
                event.text,
                event.clear,
                budget=self._effect_budget(),
                session_id=sid,
                no_field=(
                    "Typing into the element found no text field with focus"
                    if index is None
                    else f"Typing into element {index} found no text field with focus"
                ),
            )
        except Exception as e:
            logger.error("Type failed: %s", e)
            return {"error": str(e)}

    async def _on_type_at_coordinate(self, event: TypeAtCoordinateEvent) -> dict[str, Any] | None:
        """Type into whatever is at (x, y) — the vision-only path.

        A vision-only agent negotiates no DOM fragment, so the
        selector_map is empty and ``DOM.focus`` has nothing to aim at.
        Clicking the coordinate is what moves focus; everything after it
        (the check that a text field took focus, select-all + Backspace,
        ``Input.insertText``) is focus-relative already and is shared
        verbatim with :meth:`_on_type_text`.

        The focusing click measures nothing: this action's effect is the
        typing's (``TextInputEffect``), measured from the focus it left.
        """
        budget = self._effect_budget()
        clicked = await self._click_at(
            event.coordinate_x,
            event.coordinate_y,
            "left",
            1,
            label=f"({event.coordinate_x}, {event.coordinate_y})",
        )
        if "error" in clicked:
            return clicked
        try:
            return await self._insert_text_at_focus(
                event.text,
                event.clear,
                budget=budget,
                # No coordinates in the text: the action line already says where, on the
                # grid the agent writes, and a CSS-pixel copy here would contradict it.
                no_field="No text field took focus at the clicked point",
            )
        except Exception as e:
            logger.error("Type at coordinate failed: %s", e)
            return {"error": str(e)}

    async def _insert_text_at_focus(
        self,
        text: str,
        clear: bool,
        *,
        budget: ProbeBudget,
        no_field: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Clear (optionally) and enter *text* into the focused text field.

        Every CDP call here targets the *current focus*, never a node id —
        which is why the coordinate path can reuse it after a click.
        ``session_id`` is the target the focus was put in; ``None`` is the
        tab, whose input Chrome routes to its focused frame.

        Before any key is sent, the focused element is judged
        (:class:`FieldProbe`, ``entry``): typed text has to have somewhere to
        go. When focus is on nothing, or on an element that takes no typed
        text (a button, a link, a read-only input), nothing is sent — no clear,
        no text — and the handler answers with an ``error`` that begins with
        *no_field* and says what has focus instead, plus the effect it
        measured. A select-all with nothing editable focused selects the whole
        page's text, and the text then goes nowhere.

        Focus the page moves a moment after the click or ``DOM.focus`` (a
        widget that focuses its input on a timer or after an opening
        animation) is waited for: the focus is read again every
        :data:`FOCUS_POLL_S` for up to ``type_focus_wait_s``, which the wait
        spends from *budget* while the rest of it stays for the probes after
        the wait. Only the pauses and the reads of where focus is belong to
        the wait: reading the element a read found focused may use the rest,
        so the end of the wait never cuts that reading short. Cut short, an
        element that keeps focus and takes no text (a button) would become one
        that cannot be told, and be typed into, depending on when the wait
        happened to end. A reading that cannot even say where focus is (it
        failed or ran out) is dropped, and the last one that could stands, for
        the verdict and the effect alike. A reading that cannot tell whether the
        focused element takes text — the element's own reading failed, focus
        is in a frame of another site, in a closed shadow root or embedded
        content — does not stop the typing: only a measured "no text field"
        does.

        The text itself belongs to :class:`HumanKeyboard`: one bulk
        ``Input.insertText`` under the ``instant`` rhythm (the default), a
        real key event per character on modelled timings under any other.
        The clear is also keyboard input: ``HumanKeyboard`` sends a complete
        select-all shortcut followed by Backspace, with the configured
        rhythm deciding its timing. The shortcut carries Chrome's
        ``selectAll`` editing command only into a field the browser edits; a
        ``textbox`` widget that is no editor gets the same keys without it,
        to handle them itself, because the command would select the page.

        Right before the text goes in, the focused element is judged by the
        sensitivity rule; the verdict comes back as ``sensitive`` and the
        action records it (``ActionResult.typed_into_sensitive_field``).

        The effect (``TextInputEffect``, answered as ``effect_facts``) is
        measured on *budget*: the focused control is read before the clear
        and again after the text (:class:`FieldProbe`: whether its value
        changed, never the value), the keys are the clear's select-all chord
        and Backspace, and the focused element is read once the text is in.
        A probe that fails or runs out of the budget leaves its fact unknown;
        it never changes the typing.
        """
        sid = self._element_session(session_id)
        field = FieldProbe(self._cdp, sid, budget)
        keys: tuple[str, ...] = ()
        try:
            await field.before()
            refused = _no_text_entry(field)
            if refused is not None:
                # The wait spends at most ``type_focus_wait_s`` of the budget;
                # the rest is kept for the probes after it.
                wait_until = time.monotonic() + self._focus_wait_s
                kept = min(budget.remaining, budget.seconds - self._focus_wait_s)
                while refused is not None and time.monotonic() < wait_until:
                    again = FieldProbe(self._cdp, sid, budget)
                    adopted = False
                    try:
                        with budget.keeping(kept):
                            if budget.remaining <= 0:
                                break
                            await budget.sleep(min(FOCUS_POLL_S, wait_until - time.monotonic()))
                            await again.locate()
                        if again.located:
                            # Reading the element found is no part of the
                            # wait: it may use the kept rest, so the end of
                            # the wait never cuts it short.
                            await again.read_focused()
                            field, again, adopted = again, field, True
                    finally:
                        # The reading not kept: the one replaced, or one that
                        # could not say where focus is.
                        await budget.run(again.release, "release field")
                    if adopted:
                        refused = _no_text_entry(field)
            if refused is not None:
                error = f"{no_field}: {refused}, so no text was typed."
                logger.info("Not typing: %s", error)
                value_changed = await field.after()
                focus_after = await focused_element(self._cdp, self._session_id, budget)
                return {
                    "error": error,
                    "effect_facts": {
                        "target": field.target,
                        "sensitive": field.sensitive,
                        "value_changed": value_changed,
                        "keys": (),
                        "text_entered": False,
                        "focus_after": focus_after,
                    },
                }
            if clear:
                await self._keyboard.clear_focused(
                    session_id=sid, select_all_command=field.entry != "widget"
                )
                select_all = parse_key_chord(f"{self._keyboard.select_all_modifier[0]}+a")
                keys = (select_all.text, "Backspace")
                # The clear's keystrokes can move focus (a code box that steps
                # back on Backspace), so the field is judged again right before
                # the text goes in.
                sensitive = await self._focused_field_is_sensitive(sid)
            else:
                # Nothing was sent since the field probe read the focused
                # element: its verdict is the one right before the text.
                sensitive = field.sensitive if isinstance(field.sensitive, bool) else None
            await self._keyboard.type_text(text, session_id=sid)
            value_changed = await field.after()
            focus_after = await focused_element(self._cdp, self._session_id, budget)
        finally:
            await budget.run(field.release, "release field")
        return {
            "typed": True,
            "length": len(text),
            "sensitive": sensitive,
            "effect_facts": {
                "target": field.target,
                "sensitive": field.sensitive,
                "value_changed": value_changed,
                "keys": keys,
                "text_entered": bool(text),
                "focus_after": focus_after,
            },
        }

    async def _focused_field_is_sensitive(self, session_id: str | None) -> bool | None:
        """Whether the element that will receive the text is a sensitive control.

        Read in the session the keystrokes go to: the element's own frame
        target for an index type, the tab for a coordinate type (Chrome routes
        it into the focused frame, which the read follows when it can, and
        reports ``None`` when that frame belongs to another process). A
        failed read is ``None`` too — unknown, never "not sensitive".
        """
        try:
            reply = await self._cdp.send(
                "Runtime.evaluate",
                {"expression": _FOCUSED_FIELD_JS, "returnByValue": True},
                session_id=session_id,
            )
        except Exception as exc:
            logger.debug("Reading the focused element failed: %s", exc)
            return None
        return _focused_field_verdict(reply)

    # -----------------------------------------------------------------------
    # Drag
    # -----------------------------------------------------------------------

    async def _on_drag(self, event: DragEvent) -> dict[str, Any] | None:
        """Press at the start coordinate, travel, release at the end.

        The whole gesture belongs to :class:`HumanMouse` — press/move/
        release with the run's trajectory strategy in between, so a
        humanised run drags along a human path rather than teleporting.

        Its effect (``PointerEffect``, answered as ``effect_facts``): the
        press and release points, the element the tab hit-tests at the press
        point right before the button goes down, and the focused element
        afterwards. A probe that fails or runs out of the budget leaves its
        fact unknown; it never changes the drag.
        """
        budget = self._effect_budget()
        facts: dict[str, Any] = {
            "point": ViewportPoint(x=event.start_x, y=event.start_y),
            "release_point": ViewportPoint(x=event.end_x, y=event.end_y),
            "hit": UNKNOWN,
            "focus_after": UNKNOWN,
        }

        async def hit_test(px: int, py: int) -> None:
            facts["point"] = ViewportPoint(x=px, y=py)
            facts["hit"], _ = await element_at(self._cdp, self._session_id, px, py, budget)

        try:
            await self._mouse.drag(
                event.start_x,
                event.start_y,
                event.end_x,
                event.end_y,
                event.button,
                before_press=hit_test,
            )
            answer: dict[str, Any] = {
                "dragged": True,
                "from": [event.start_x, event.start_y],
                "to": [event.end_x, event.end_y],
            }
            facts["focus_after"] = await focused_element(self._cdp, self._session_id, budget)
            answer["effect_facts"] = facts
            return answer
        except Exception as e:
            logger.error(
                "Drag failed (%d, %d) -> (%d, %d): %s",
                event.start_x,
                event.start_y,
                event.end_x,
                event.end_y,
                e,
            )
            # TimeoutError may have an empty message. An empty error is read as
            # success by action result consumers, despite the failed gesture.
            return {"error": f"{type(e).__name__}: {e}"}

    # -----------------------------------------------------------------------
    # Scroll
    # -----------------------------------------------------------------------

    async def _on_scroll(self, event: ScrollEvent) -> dict[str, Any]:
        """Scroll the viewport or a specific element, and measure what moved.

        The wheel is dispatched at the pointer's current position for a page
        scroll, or at an aimed point inside the element for an element
        scroll. Around it, a :class:`ScrollProbe` holds the containers the
        wheel could move at that point and reports the one that did, with its
        offsets before and after and whether it reached its edge
        (``effects.ScrollEffect``). Measuring is bounded by the action's
        budget and never fails the scroll.
        """
        try:
            dx, dy = 0, 0
            if event.direction == "down":
                dy = event.amount
            elif event.direction == "up":
                dy = -event.amount
            elif event.direction == "right":
                dx = event.amount
            elif event.direction == "left":
                dx = -event.amount

            if event.backend_node_id is not None:
                node = self._resolve_node(event.backend_node_id, event.session_id)
                if node is None:
                    raise ValueError(f"Element {event.backend_node_id} not in selector_map")
                owners = node.frame_owners()
                try:
                    live = await self._get_live_viewport_center(
                        event.backend_node_id, event.session_id, owners
                    )
                except FrameGeometryError as exc:
                    logger.warning("Not scrolling %s: %s", self._describe_node(node), exc)
                    raise
                if live is not None:
                    cx, cy, w, h = live
                    # The wheel is aimed at a measured box too, so it goes
                    # through the same seam as the click.
                    cx, cy = self._aim_inside(cx, cy, w, h)
                elif owners:
                    # Same reason as a click: the frame changed.
                    logger.warning(
                        "Element %s inside a frame could not be located live; "
                        "not scrolling at its snapshot position",
                        self._describe_node(node),
                    )
                    raise RuntimeError("Element could not be located for scrolling")
                elif node.bounds is not None:
                    cx_f, cy_f = node.bounds.center
                    cx, cy = self._aim_inside(cx_f, cy_f, node.bounds.width, node.bounds.height)
                else:
                    raise RuntimeError("Element could not be located for scrolling")
                if not self._in_viewport(cx, cy):
                    # A wheel outside the viewport scrolls nothing; same rule as a click.
                    raise RuntimeError(_OUTSIDE_VIEWPORT)
            else:
                cx, cy = self._mouse.position

            direction = event.direction
            if direction not in ("up", "down", "left", "right"):
                raise ValueError(f"Unknown scroll direction {direction!r}")
            probe = ScrollProbe(
                self._cdp, self._session_id, self._effect_budget(), direction=direction
            )
            try:
                await probe.before(cx, cy)
                await self._mouse.wheel(cx, cy, dx, dy)
                facts = await probe.after()
            finally:
                await probe.release()
            return {"scrolled": True, "effect_facts": facts}
        except Exception as e:
            logger.error("Scroll failed: %s", e)
            raise

    # -----------------------------------------------------------------------
    # Navigation
    # -----------------------------------------------------------------------

    async def _on_navigate(self, event: NavigateToUrlEvent) -> dict[str, Any]:
        """Navigate to a URL: wait for the commit, then (optionally) for a lifecycle phase.

        Two waits, one after the other (``docs/architecture_design.md`` §4.14,
        "``browser.navigate``"):

        1. **The commit.** Chrome answers ``Page.navigate`` once the
           navigation committed or failed; a server that never answers gets
           no reply at all. The reply is awaited for at most
           ``event.commit_timeout_s``. No reply in time, a command Chrome
           refuses (an invalid URL), or an ``errorText`` in the reply (an
           unreachable host, whose error page Chrome then shows; a 204 or a
           download, which commit nothing) fail the navigation: the answer is
           ``{"error": ...}``, which ``NavigateAction`` turns into a failed
           result.
        2. **The lifecycle phase.** ``wait_until`` honours ``"load"`` /
           ``"domcontentloaded"`` / ``"networkidle"``; anything else
           (including the empty string) does not wait. The committed
           document is waited on for at most ``event.lifecycle_timeout_s``
           from the reply. A phase that does not arrive in time is logged at
           WARNING and the navigation succeeds: the tab already shows the new
           document — a page whose ``load`` never comes because one resource
           never finishes is still drawn, and the next observation's settle
           wait pays for that ``load`` at most once more. A reply without a
           ``loaderId`` is a same-document navigation: there is no new
           document to wait for.

        The event's own timeout outlasts both waits
        (``NavigateToUrlEvent._outlast_both_waits``), so every outcome above
        is this handler's answer, never the caller giving up first.

        A navigation that reached its page answers ``{"navigated": True,
        "effect_facts": ...}``: the facts of a
        :class:`~spectragent.core.capabilities.browser.effects.NavigationEffect`
        (``docs/architecture_design.md`` §4.5, "动作效果") — the current
        history entry's URL before anything else happens and after both
        waits, and whether the document shown then finished loading. They are
        measured within ``action_effect_timeout_s``; a probe that fails or
        runs out leaves its fact unknown and never changes the answer.

        The lifecycle waiter is registered **synchronously** before
        ``Page.navigate`` is sent so we don't race the CDP response.
        Using :func:`agent_cdp.advanced.expect.expect` here would be a
        race because its ``scope.connect`` runs inside the coroutine
        body and may not be installed before ``Page.loadEventFired``
        arrives.

        Filters applied to the lifecycle event:

        - main frame only (``frame_id == self._main_frame_id_getter()``
          when both are populated; otherwise allow any frame)
        - this navigation only (``loader_id == result["loaderId"]`` when
          both are populated; this discards stale ``Page.lifecycleEvent``
          replays Chrome may emit when ``Page.enable`` is called against
          an already-loaded document)
        """
        if self._scope is None:
            raise RuntimeError("Navigation executor has no attached tab scope")

        # The URL before, read before the lifecycle waiter is armed: while it
        # is armed and the loader is not yet known, an event of the document
        # being left would match it.
        budget = self._effect_budget()
        before = await current_location(self._cdp, self._session_id, budget)

        # 1. Pick lifecycle event class + base predicate from wait_until.
        lifecycle_target = self._lifecycle_target(event.wait_until)

        # Arm synchronously before sending the command. The lifecycle budget
        # starts after commit, so this listener deliberately has no own deadline.
        waiter = None
        expected_loader: str | None = None
        if lifecycle_target is not None:
            target_evt, base_predicate = lifecycle_target
            main_frame_id = (
                self._main_frame_id_getter() if self._main_frame_id_getter is not None else None
            )

            def matches(e: Any) -> bool:
                if not base_predicate(e):
                    return False
                e_frame = getattr(e, "frame_id", None)
                if main_frame_id is not None and e_frame is not None and e_frame != main_frame_id:
                    return False
                e_loader = getattr(e, "loader_id", None)
                return expected_loader is None or e_loader is None or e_loader == expected_loader

            waiter = expect(self._scope, target_evt, include=matches)

        # 3. Emit start, then fire Page.navigate (the commit), then wait for
        #    the phase, then emit complete.
        self._scope.emit(
            NavigationStartedEvent(
                target_id=self._session_id or "",
                url=event.url,
            )
        )

        try:
            commit_s = event.commit_timeout_s
            try:
                result = await asyncio.wait_for(
                    self._cdp.send(
                        "Page.navigate",
                        {"url": event.url},
                        session_id=self._session_id,
                    ),
                    timeout=commit_s,
                )
            except TimeoutError:
                return self._navigation_failed(event, f"did not commit within {commit_s:g} s")
            except Exception as exc:
                return self._navigation_failed(event, f"was refused by the browser: {exc}")

            if not isinstance(result, dict):
                return self._navigation_failed(event, "returned no navigation reply")
            reply: dict[str, Any] = result
            error_text = reply.get("errorText")
            if error_text:
                # Chrome says the navigation failed: an unreachable host (its
                # error page is now on screen), a 204 or a download (nothing
                # committed). There is no document of this navigation to wait for.
                return self._navigation_failed(
                    event, f"failed: {error_text}", error_text=str(error_text)
                )

            loader_id = reply.get("loaderId")
            if waiter is not None and loader_id:
                # Tighten the predicate to this navigation's loader.
                expected_loader = loader_id
                wait_s = event.lifecycle_timeout_s
                try:
                    await asyncio.wait_for(waiter, timeout=wait_s)
                except TimeoutError:
                    logger.warning(
                        "navigate: %s committed, but its %r lifecycle event did not arrive "
                        "within %gs; carrying on with the page as it is",
                        event.url,
                        event.wait_until,
                        wait_s,
                    )

            self._scope.emit(
                NavigationCompleteEvent(
                    target_id=self._session_id or "",
                    url=event.url,
                )
            )
            after = await current_location(self._cdp, self._session_id, budget)
            loaded = await document_loaded(self._cdp, self._session_id, budget)
            return {
                "navigated": True,
                "effect_facts": {"before": before, "after": after, "document_loaded": loaded},
            }
        finally:
            if waiter is not None:
                waiter.close()

    def _navigation_failed(
        self, event: NavigateToUrlEvent, what: str, *, error_text: str | None = None
    ) -> dict[str, Any]:
        """The answer for a navigation that did not reach its page: ``{"error": ...}``.

        Logged at WARNING and announced on the scope: Chrome's own verdict
        (*error_text*, the reply's ``errorText``) on ``NavigationCompleteEvent``,
        anything else — no reply in time, a refused command — on
        ``BrowserErrorEvent``.
        """
        message = f"navigation to {event.url!r} {what}"
        logger.warning("%s", message)
        if self._scope is not None:
            if error_text is not None:
                self._scope.emit(
                    NavigationCompleteEvent(
                        target_id=self._session_id or "",
                        url=event.url,
                        error_text=error_text,
                    )
                )
            else:
                self._scope.emit(BrowserErrorEvent(error_type="navigation", message=message))
        return {"error": message}

    def _lifecycle_target(
        self,
        wait_until: str,
    ) -> tuple[type[BaseEvent[Any]], Callable[[Any], bool]] | None:
        """Map ``wait_until`` to (event class, predicate).

        Returns ``None`` for unknown / empty values so navigate becomes
        fire-and-forget. The predicate runs *before* main-frame and
        loader filters.
        """
        if wait_until == "load":
            return PageLoadEvent, lambda _e: True
        if wait_until == "domcontentloaded":
            return DOMContentLoadedEvent, lambda _e: True
        if wait_until == "networkidle":
            # Either the lifecycle phase fired by setLifecycleEventsEnabled
            # — full quiet — or the more permissive "almost idle" variant
            # which Chrome emits a few hundred ms earlier. Accepting both
            # makes timeouts on long-poll sites less catastrophic.
            return LifecycleEvent, (
                lambda e: getattr(e, "name", "") in ("networkIdle", "networkAlmostIdle")
            )
        return None

    async def _on_go_back(self, _event: GoBackEvent) -> dict[str, Any]:
        return await self._navigate_history(-1)

    async def _on_go_forward(self, _event: GoForwardEvent) -> dict[str, Any]:
        return await self._navigate_history(1)

    async def _navigate_history(self, offset: int) -> dict[str, Any]:
        """Go to the adjacent committed history entry; answer ``{"navigated": True, ...}``.

        ``Page.navigateToHistoryEntry`` answers before the navigation commits,
        so the effect's URL after is read once that entry has become the
        current one, waited for within ``action_effect_timeout_s``
        (:func:`~spectragent.core.capabilities.browser.effect_probes.history_entry_committed`).
        When it has not by then, the URL after and whether the document
        loaded are unknown — never the page being left reported as the
        destination. The URL before is the current entry of the history this
        handler read to find the adjacent one.
        """
        history = await self._cdp.send("Page.getNavigationHistory", {}, session_id=self._session_id)
        if not isinstance(history, dict) or not isinstance(history.get("currentIndex"), int):
            raise RuntimeError("Browser returned no navigation history")
        entries = history.get("entries", [])
        index = history["currentIndex"] + offset
        if not 0 <= index < len(entries):
            direction = "back" if offset < 0 else "forward"
            raise ValueError(f"Cannot go {direction}: no navigation history entry")
        entry_id = entries[index].get("id")
        if not isinstance(entry_id, int):
            raise RuntimeError("Browser navigation history entry has no id")
        before = history_location(history) or UNKNOWN
        await self._cdp.send(
            "Page.navigateToHistoryEntry", {"entryId": entry_id}, session_id=self._session_id
        )
        budget = self._effect_budget()
        after = await history_entry_committed(self._cdp, self._session_id, entry_id, budget)
        loaded = (
            await document_loaded(self._cdp, self._session_id, budget)
            if after != UNKNOWN
            else UNKNOWN
        )
        return {
            "navigated": True,
            "effect_facts": {"before": before, "after": after, "document_loaded": loaded},
        }

    async def _on_refresh(self, event: RefreshEvent) -> dict[str, Any]:
        """Reload the tab's document; answer ``{"navigated": True, ...}``.

        ``Page.reload`` (``ignoreCache`` for a hard reload) answers before the
        new document commits; the effect's URL after is read once the main
        frame shows another document, waited for within
        ``action_effect_timeout_s``
        (:func:`~spectragent.core.capabilities.browser.effect_probes.reload_committed`).
        When it has not by then, the URL after and whether the document loaded
        are unknown — never the document being left reported as the reloaded one.
        """
        budget = self._effect_budget()
        before = await current_location(self._cdp, self._session_id, budget)
        loader = await document_loader(self._cdp, self._session_id, budget)
        await self._cdp.send(
            "Page.reload", {"ignoreCache": event.ignore_cache}, session_id=self._session_id
        )
        after = await reload_committed(self._cdp, self._session_id, loader, budget)
        loaded = (
            await document_loaded(self._cdp, self._session_id, budget)
            if after != UNKNOWN
            else UNKNOWN
        )
        return {
            "navigated": True,
            "effect_facts": {"before": before, "after": after, "document_loaded": loaded},
        }

    # -----------------------------------------------------------------------
    # Send keys
    # -----------------------------------------------------------------------

    async def _on_send_keys(self, event: SendKeysEvent) -> dict[str, Any]:
        """Send one key or chord (``Enter``, ``a``, ``Control+A``).

        The input is parsed by :func:`~spectragent.core.capabilities.browser.input.chords.parse_key_chord`,
        the same parser ``browser.send_keys`` refuses with before it emits:
        anything that is not a named key, a single character or a ``+``
        chord of them answers an error and sends no key event at all
        (``docs/architecture_design.md`` §4.13). The key event carries
        ``code`` and ``windowsVirtualKeyCode`` so the synthesized DOM
        ``KeyboardEvent`` has a non-zero ``keyCode`` and a real ``code``
        string — both of which JS handlers (especially React's onKeyDown)
        check before treating the press as Enter. No
        ``nativeVirtualKeyCode``: on macOS Chrome matched an unhandled key
        carrying one against its own menu and opened
        ``chrome://settings/help`` in front of the agent's tab (the
        ``input/keyboard.py`` module docstring, "No native key code").

        The effect (``TextInputEffect``, answered as ``effect_facts``) is
        measured as typing's is: the focused control before and after the
        key (:class:`FieldProbe`), the chord as written canonically, and the
        focused element afterwards. A key that types a printable character
        (no Control, Alt or Meta held) into a field that is sensitive — or
        whose sensitivity could not be read — is not named: it is reported as
        entered text, so a secret's characters never reach the effect. An
        input refused by the parser measures nothing.

        A chord that is a browser shortcut this browser does not run
        (``input/shortcuts.py``) is pressed like any other, and the effect's
        ``shortcut`` says what it is (:meth:`_shortcut_note`). The shortcuts it
        runs never reach this handler: ``browser.send_keys`` runs them itself,
        and sends one here only when a text field takes it (macOS's
        Meta+ArrowLeft / Meta+ArrowRight).
        """
        try:
            chord = parse_key_chord(event.keys)
        except Exception as e:
            logger.error("SendKeys failed: %s", e)
            return {"error": f"{type(e).__name__}: {e}"}
        budget = self._effect_budget()
        field = FieldProbe(self._cdp, self._session_id, budget)
        try:
            await field.before()
            await self._keyboard.press_key(
                chord.payload,
                modifiers=chord.modifier_mask,
                char_text=chord.char_text,
                session_id=self._session_id,
            )
            value_changed = await field.after()
            focus_after = await focused_element(self._cdp, self._session_id, budget)
        except Exception as e:
            logger.error("SendKeys failed: %s", e)
            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            await budget.run(field.release, "release field")
        types_a_character = (
            chord.char_text is not None
            and chord.char_text.isprintable()
            and set(chord.modifiers) <= {"Shift"}
        )
        withheld = types_a_character and field.sensitive is not False
        facts: dict[str, Any] = {
            "target": field.target,
            "sensitive": field.sensitive,
            "value_changed": value_changed,
            "keys": () if withheld else (chord.text,),
            "text_entered": withheld,
            "focus_after": focus_after,
        }
        note = None if withheld else self._shortcut_note(chord, field)
        if note is not None:
            facts["shortcut"] = note
        return {"pressed": True, "effect_facts": facts}

    def _shortcut_note(
        self, chord: KeyChord, field: FieldProbe
    ) -> ShortcutWithoutUi | ShortcutOfAnotherPlatform | None:
        """What a pressed chord is to the browser, when it is a shortcut it does not run.

        The measured platform's shortcut for browser UI this browser does not
        show, always; another platform's browser shortcut only when nothing
        that takes text had focus (``entry`` measured ``"none"``), where it is
        often the measured platform's editing key instead.
        """
        platform = self._input_platform
        match = browser_shortcut(chord, platform)
        if isinstance(match, WithoutUi):
            return ShortcutWithoutUi(chord=chord.text, command=match.command)
        if isinstance(match, OtherPlatform) and field.entry == "none":
            return ShortcutOfAnotherPlatform(
                chord=chord.text,
                command=match.command,
                tab=match.tab,
                platform=platform,
                binding=match.binding,
            )
        return None

    # -----------------------------------------------------------------------
    # Select dropdown — uses DOM.resolveNode + Runtime.callFunctionOn
    # -----------------------------------------------------------------------

    async def _on_select_dropdown(self, event: SelectDropdownEvent) -> dict[str, Any]:
        """Select a dropdown option via CDP DOM.resolveNode + Runtime.callFunctionOn.

        Uses proper CDP argument binding to avoid JavaScript injection.

        Answers ``{"selected": True, "effect_facts": ...}``: the facts of a
        :class:`~spectragent.core.capabilities.browser.effects.SelectionEffect`,
        read by the selection script itself, right before and right after it
        selects (:func:`~spectragent.core.capabilities.browser.effect_probes.selection_facts`).
        A control the shared sensitivity rule judges sensitive in the
        observation it was indexed from keeps its option text in the page.
        """
        node = self._resolve_node(event.backend_node_id, event.session_id)
        if node is None:
            raise ValueError(f"Element {event.backend_node_id} not in selector_map")
        withhold = is_sensitive_control(
            node.tag_name, {str(k).lower(): str(v) for k, v in node.attributes.items()}
        )
        if event.value is None and event.text is None and event.index is None:
            raise ValueError("Dropdown selection requires value, text or index")
        await self._highlight_node(event.backend_node_id, event.session_id)
        await self._approach_node(event.backend_node_id, event.session_id)
        try:
            sid = self._element_session(event.session_id)
            # Resolve the backend node to a remote object.
            resolve_result = await self._cdp.send(
                "DOM.resolveNode",
                {
                    "backendNodeId": event.backend_node_id,
                },
                session_id=sid,
            )
            if not resolve_result or "object" not in resolve_result:
                raise RuntimeError(f"Cannot resolve node {event.backend_node_id} for dropdown")

            object_id = resolve_result["object"]["objectId"]

            if event.value is not None:
                value: str | int = event.value
                selection = "Array.from(this.options).findIndex(opt => opt.value === value)"
            elif event.text is not None:
                value = event.text
                selection = "Array.from(this.options).findIndex(opt => opt.text.trim() === value)"
            else:
                assert event.index is not None
                value = event.index
                selection = "value"
            result = await self._cdp.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": (
                        "function(value) {"
                        + SELECT_FACTS_JS
                        + "  if (!this.options || this.disabled) return false;"
                        f"  const index = {selection};"
                        "  const option = this.options[index];"
                        "  if (!option || option.disabled || option.parentElement.disabled) return false;"
                        "  const before = this.selectedIndex;"
                        "  this.selectedIndex = index;"
                        "  this.dispatchEvent(new Event('change', {bubbles: true}));"
                        "  return {selected: this.selectedIndex === index, before: before,"
                        f"          after: selectFacts(this, {'true' if withhold else 'false'})}};"
                        "}"
                    ),
                    "objectId": object_id,
                    "arguments": [{"value": value}],
                    "returnByValue": True,
                },
                session_id=sid,
            )
            if not isinstance(result, dict) or result.get("exceptionDetails"):
                raise RuntimeError("Dropdown selection returned no successful JavaScript result")
            answer = result.get("result", {}).get("value")
            if not isinstance(answer, dict) or answer.get("selected") is not True:
                raise ValueError("Dropdown has no enabled option matching the requested selection")
        except Exception as e:
            logger.error("Select dropdown failed: %s", e)
            raise
        facts = selection_facts(answer.get("before"), answer.get("after"), withheld=withhold)
        return {"selected": True, "effect_facts": facts}

    # -----------------------------------------------------------------------
    # Extract content
    # -----------------------------------------------------------------------

    async def _on_extract_content(self, event: ExtractContentEvent) -> str | None:
        """Extract the page's text, or one element's outer HTML with sensitive values withheld.

        The element's markup goes into the action result, and from there into
        the next prompt, so the sensitivity rule is applied to it first
        (``dom/sensitivity.withhold_sensitive_markup``): a password the page
        wrote into a ``value`` attribute comes back as the placeholder. Whether
        the element itself lies inside a sensitive control is read off the
        observation tree it was indexed from.
        """
        try:
            if event.backend_node_id is not None:
                if self._resolve_node(event.backend_node_id, event.session_id) is None:
                    raise ValueError(f"Element {event.backend_node_id} not in selector_map")
                result = await self._cdp.send(
                    "DOM.getOuterHTML",
                    {
                        "backendNodeId": event.backend_node_id,
                    },
                    session_id=self._element_session(event.session_id),
                )
                if not isinstance(result, dict) or not isinstance(result.get("outerHTML"), str):
                    raise RuntimeError("DOM.getOuterHTML returned no markup")
                node = self._resolve_node(event.backend_node_id, event.session_id)
                return withhold_sensitive_markup(
                    result["outerHTML"],
                    inside_sensitive=_inside_sensitive_control(node),
                )
            else:
                result = await self._cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": "document.body.innerText",
                    },
                    session_id=self._session_id,
                )
                if not isinstance(result, dict) or result.get("exceptionDetails"):
                    raise RuntimeError(
                        "Page text extraction returned no successful JavaScript result"
                    )
                text = result.get("result", {}).get("value")
                if not isinstance(text, str):
                    raise RuntimeError("Page text extraction returned no text")
                return text
        except Exception as e:
            logger.error("Extract content failed: %s", e)
            raise

    # -----------------------------------------------------------------------
    # Done
    # -----------------------------------------------------------------------

    async def _on_done(self, event: DoneEvent) -> None:
        """Agent signals task completion — no-op at watchdog level."""
        logger.info("Agent done: %s", event.extracted_content)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _describe_node(node: DOMElementNode) -> str:
        """Build a short human label for the persistent click marker.

        Picks the most useful identifying string available, in order:
        accessible name, visible text, common attributes (aria-label,
        title, name, value, placeholder, alt), then ``#id``/``.class``.
        Truncates to 80 chars so the on-page label stays one line.
        """
        tag = (node.tag_name or "?").lower()
        attrs = node.attributes or {}
        candidates = [
            (node.ax_name or "").strip(),
            (node.text_content or "").strip(),
            attrs.get("aria-label", "").strip(),
            attrs.get("title", "").strip(),
            attrs.get("placeholder", "").strip(),
            attrs.get("value", "").strip(),
            attrs.get("name", "").strip(),
            attrs.get("alt", "").strip(),
        ]
        text = next((c for c in candidates if c), "")
        if not text:
            ident = ""
            if attrs.get("id"):
                ident = "#" + attrs["id"]
            elif attrs.get("class"):
                ident = "." + attrs["class"].split()[0]
            text = ident
        # Collapse whitespace so multi-line text content stays on one line.
        text = " ".join(text.split())
        if len(text) > 80:
            text = text[:77] + "..."
        idx = node.element_index
        prefix = f"<{tag}>" if idx is None else f"[{idx}] <{tag}>"
        return f"{prefix} {text}".rstrip()

    async def _install_overlay_script(self) -> None:
        """Register the JS overlay so it runs on every navigation in this tab.

        Uses ``Page.addScriptToEvaluateOnNewDocument`` so the cursor +
        click-marker installer survives reloads and SPA route changes,
        plus a one-shot ``Runtime.evaluate`` so the *current* document
        gets it too without waiting for the next navigation. Idempotent
        per session.
        """
        if not self._debug_highlight or self._overlay_script_installed:
            return
        sid = self._session_id
        try:
            await self._cdp.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _DEBUG_OVERLAY_JS},
                session_id=sid,
            )
            await self._cdp.send(
                "Runtime.evaluate",
                {"expression": _DEBUG_OVERLAY_JS},
                session_id=sid,
            )
            self._overlay_script_installed = True
        except Exception as exc:
            logger.debug("debug overlay install failed: %s", exc)

    async def _paint_click(self, x: int, y: int, label: str) -> None:
        """Render the persistent click marker + cursor at (x, y).

        No-op when ``debug_highlight`` is off. Best-effort: any CDP
        failure is logged at debug level — the visual aid must never
        block the underlying action.
        """
        if not self._debug_highlight:
            return
        try:
            await self._install_overlay_script()
            expr = (
                f"window.__af_show_click__ && "
                f"window.__af_show_click__({x}, {y}, {json.dumps(label)});"
            )
            await self._cdp.send(
                "Runtime.evaluate",
                {"expression": expr},
                session_id=self._session_id,
            )
        except Exception as exc:
            logger.debug("debug overlay paint failed at (%d, %d): %s", x, y, exc)

    async def set_overlay_visible(self, visible: bool) -> None:
        """Toggle the debug overlay's visibility.

        Called by :class:`BrowserCapability` around the observe cycle
        so the injected cursor/marker/label don't appear in the DOM
        scrape (the browser-use cleaner filters ``display:none`` via
        its CSS visibility check) or in the screenshot fed to vision
        agents. No-op when ``debug_highlight`` is off or the overlay
        script has never been installed (no clicks yet → nothing to
        hide). All failures are swallowed at debug level — the
        observation must never fail because the visual aid is busy.
        """
        if not self._debug_highlight or not self._overlay_script_installed:
            return
        flag = "true" if visible else "false"
        try:
            await self._cdp.send(
                "Runtime.evaluate",
                {
                    "expression": (
                        f"window.__af_set_overlay_visible__ && "
                        f"window.__af_set_overlay_visible__({flag});"
                    ),
                },
                session_id=self._session_id,
            )
        except Exception as exc:
            logger.debug("debug overlay set_visible(%s) failed: %s", flag, exc)

    async def _highlight_node(self, backend_node_id: int, session_id: str | None = None) -> None:
        """DevTools-style highlight on the target node, then sleep briefly.

        No-op when ``debug_highlight`` is off. Lazy-enables the Overlay
        domain on first use per session (Overlay.enable is per-target/session;
        a node is highlighted through the session that owns it).
        Failures are logged at debug level — highlighting is best-effort
        and must never block the underlying action.
        """
        if not self._debug_highlight:
            return
        sid = self._element_session(session_id)
        try:
            if sid not in self._overlay_sessions:
                await self._cdp.send("Overlay.enable", {}, session_id=sid)
                self._overlay_sessions.add(sid)
            await self._cdp.send(
                "Overlay.highlightNode",
                {
                    "backendNodeId": backend_node_id,
                    "highlightConfig": {
                        "showInfo": True,
                        "contentColor": {"r": 255, "g": 81, "b": 0, "a": 0.4},
                        "paddingColor": {"r": 255, "g": 200, "b": 0, "a": 0.3},
                        "borderColor": {"r": 255, "g": 81, "b": 0, "a": 1.0},
                    },
                },
                session_id=sid,
            )
            await asyncio.sleep(self._debug_highlight_ms / 1000)
        except Exception as exc:
            logger.debug("debug_highlight failed for node %d: %s", backend_node_id, exc)

    async def _overlay_move_cursor(self, x: int, y: int) -> None:
        """Drive the debug overlay's JS cursor to (x, y) for one trajectory point.

        Passed to ``HumanMouse`` as its ``move_callback`` only when
        ``debug_highlight`` is on, so a human watching headed Chrome sees the
        cursor animate along the real path. Best-effort — any CDP failure is
        swallowed at debug level so the overlay never blocks the action.
        """
        try:
            await self._cdp.send(
                "Runtime.evaluate",
                {
                    "expression": (
                        f"window.__af_move_cursor__ && window.__af_move_cursor__({x}, {y});"
                    )
                },
                session_id=self._session_id,
            )
        except Exception as exc:
            logger.debug("overlay cursor move to (%d,%d) failed: %s", x, y, exc)

    async def _approach_node(self, backend_node_id: int, session_id: str | None = None) -> None:
        """Move the humanized cursor onto an element before a keyboard/DOM action.

        No-op unless the trajectory strategy is humanized, i.e. anything but
        ``instant`` (type/select are keyboard/DOM operations with no native
        cursor move, so we only add an approach when humanizing). Best-effort: a failed coordinate resolution must not block
        the underlying type/select — including a frame whose transform has no
        affine inverse, where the keystrokes still reach the element through
        its own session and only the cursor cannot be aimed.
        """
        if not self._mouse.humanized:
            return
        node = self._resolve_node(backend_node_id, session_id)
        owners = node.frame_owners() if node is not None else []
        try:
            live = await self._get_live_viewport_center(backend_node_id, session_id, owners)
        except FrameGeometryError as exc:
            logger.debug("cursor approach to node %d skipped: %s", backend_node_id, exc)
            return
        if live is None:
            return
        cx, cy, w, h = live
        # Same box-to-point seam as a click: a cursor that rests on the exact
        # centre of every field it types into is the same tell, and a
        # mousemove endpoint is exactly what a page records.
        ax, ay = self._aim_inside(cx, cy, w, h)
        try:
            await self._mouse.move_to(ax, ay, target_w=w, target_h=h)
        except Exception as exc:
            logger.debug("cursor approach to node %d failed: %s", backend_node_id, exc)

    async def _get_live_viewport_center(
        self,
        backend_node_id: int,
        session_id: str | None = None,
        frame_owners: Sequence[DOMElementNode] = (),
    ) -> tuple[int, int, int, int] | None:
        """Return ``(center_x, center_y, width, height)`` in page viewport px, or None.

        Pipeline: scroll the element into view, then measure its border box
        where it is drawn now.

        - An element of the top-level document is resolved to a JS object and
          reads ``getBoundingClientRect()`` — CSS pixels of the page viewport,
          exactly what ``Input.dispatchMouseEvent`` wants.
        - An element inside frames (``frame_owners``, innermost first) is
          measured from layout alone, with no script in its frame: frames are
          where third-party code runs (captchas, anti-bot vendors, payment
          fields), and a measuring call through a page's own prototypes is
          something that code can watch. ``DOM.getContentQuads`` answers in
          the coordinates of the element's local root — the page viewport for
          a same-process frame, the frame's own viewport for an out-of-process
          one — so each process boundary on the way up applies its owner's
          frame projection (``DOM.getBoxModel``, measured in the owner's own
          local root).

        Width/height feed the trajectory layer's Fitts's-law timing and the
        ``gan`` strategy's target box. Any failure → ``None``; the caller
        decides whether a snapshot position is still worth anything.
        :class:`FrameGeometryError` propagates instead, because "this frame's
        transform cannot be inverted" is not something a later observation
        fixes and the agent is told so by name.
        """
        sid = self._element_session(session_id)
        try:
            try:
                # Scrolls every enclosing frame too, across processes.
                await self._cdp.send(
                    "DOM.scrollIntoViewIfNeeded",
                    {"backendNodeId": backend_node_id},
                    session_id=sid,
                )
            except Exception as exc:
                # Element may already be in view, or scrolling may not apply
                # (e.g. fixed-positioned). Continue rather than abort.
                logger.warning(
                    "scrollIntoViewIfNeeded failed for node %d: %s",
                    backend_node_id,
                    exc,
                )

            if frame_owners:
                box = await self._frame_element_box(backend_node_id, sid, frame_owners)
            else:
                box = await self._page_element_box(backend_node_id, sid)
                if box is not None and not self._in_viewport(*_centre(box)):
                    # ``scrollIntoViewIfNeeded`` leaves an element alone that is
                    # partly visible already, centre outside or not. Centre it.
                    await self._call_on_node(backend_node_id, sid, _CENTRE_INTO_VIEW_JS)
                    box = await self._page_element_box(backend_node_id, sid)
            if box is None:
                return None
            x, y, w, h = box
            return int(x + w / 2), int(y + h / 2), int(w), int(h)
        except FrameGeometryError:
            raise
        except Exception as exc:
            logger.warning(
                "Live viewport center query failed for node %d: %s",
                backend_node_id,
                exc,
            )
            return None

    def _in_viewport(self, x: float, y: float) -> bool:
        """Whether a point is inside the viewport, where a press reaches the page."""
        return 0 <= x < self._vw and 0 <= y < self._vh

    async def _page_element_box(
        self, backend_node_id: int, session_id: str | None
    ) -> tuple[float, float, float, float] | None:
        """A top-level element's live ``getBoundingClientRect()``: ``(x, y, w, h)``."""
        rect = await self._call_on_node(backend_node_id, session_id, _CLIENT_RECT_JS)
        if rect is None:
            return None
        try:
            return float(rect["x"]), float(rect["y"]), float(rect["w"]), float(rect["h"])
        except (KeyError, TypeError, ValueError):
            return None

    async def _frame_element_box(
        self,
        backend_node_id: int,
        session_id: str | None,
        frame_owners: Sequence[DOMElementNode],
    ) -> tuple[float, float, float, float] | None:
        """A frame element's live border box in the page viewport, from layout only.

        The box starts in the coordinates of the element's own local root
        and is mapped through one :class:`FrameProjection` per process
        boundary, each built from that frame's owner's box model. The
        corners travel, not the box: a rotated frame turns the box into a
        parallelogram, and re-squaring it at every hop would inflate it
        each time. Same-renderer owners are skipped — Blink already
        composed their transforms into the quads it answered with.

        Raises on a CDP error (a detached node, a session that is gone)
        and :class:`FrameGeometryError` when a frame's own transform has
        no affine inverse.
        """
        box = await self._layout_box(backend_node_id, session_id)
        if box is None:
            return None
        points = corners(box)
        local_root = session_id
        for owner in frame_owners:
            owner_session = self._element_session(owner.session_id)
            if owner_session == local_root:
                continue  # same renderer: already in this local root's coordinates
            if owner.backend_node_id is None:
                return None
            model = await self._cdp.send(
                "DOM.getBoxModel",
                {"backendNodeId": owner.backend_node_id},
                session_id=owner_session,
            )
            points = FrameProjection.of_box_model(model).quad(points)
            local_root = owner_session
        return bounding_box(points)

    async def _layout_box(
        self, backend_node_id: int, session_id: str | None
    ) -> tuple[float, float, float, float] | None:
        """``(x, y, w, h)`` around a node's content quads, in its local root's viewport.

        The bounding box of every quad, as ``getBoundingClientRect`` is of
        every client rect; a node Chrome computes no content quads for falls
        back to its border box.
        """
        try:
            result = await self._cdp.send(
                "DOM.getContentQuads", {"backendNodeId": backend_node_id}, session_id=session_id
            )
            quads = [q for q in (result or {}).get("quads") or [] if len(q) >= 8]
        except Exception as exc:
            logger.debug("getContentQuads failed for node %d: %s", backend_node_id, exc)
            quads = []
        if not quads:
            model = await self._cdp.send(
                "DOM.getBoxModel", {"backendNodeId": backend_node_id}, session_id=session_id
            )
            border = ((model or {}).get("model") or {}).get("border") or []
            if len(border) < 8:
                return None
            quads = [border]
        xs = [value for quad in quads for value in quad[0::2]]
        ys = [value for quad in quads for value in quad[1::2]]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)

    async def _call_on_node(
        self, backend_node_id: int, session_id: str | None, function_declaration: str
    ) -> dict[str, Any] | None:
        """Run ``function_declaration`` with ``this`` = the node; its dict result, or None."""
        resolved = await self._cdp.send(
            "DOM.resolveNode",
            {"backendNodeId": backend_node_id},
            session_id=session_id,
        )
        if not resolved or "object" not in resolved:
            return None
        object_id = resolved["object"].get("objectId")
        if not object_id:
            return None
        result = await self._cdp.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": function_declaration,
                "objectId": object_id,
                "returnByValue": True,
            },
            session_id=session_id,
        )
        value = (result or {}).get("result", {}).get("value")
        return value if isinstance(value, dict) else None

    def _element_session(self, session_id: str | None) -> str | None:
        """The CDP session to address an element's node through; ``None`` = this tab."""
        target = session_id if session_id is not None else self._session_id
        if self._sessions is not None and (
            self._session_id is None
            or target is None
            or not self._sessions.owns(self._session_id, target)
        ):
            raise ValueError(
                f"Element session {target!r} does not belong to live tab {self._session_id!r}"
            )
        return target

    def _resolve_node(
        self, backend_node_id: int, session_id: str | None = None
    ) -> DOMElementNode | None:
        """Find the selector_map node that is ``backend_node_id`` in ``session_id``.

        Matched by identity, not by key: the map is keyed by element handle,
        which is the backend node id only for the tab's own target — an
        out-of-process frame's element has a namespaced key and a backend
        node id that may belong to a page element too.
        """
        if self._selector_map_getter is None:
            return None
        selector_map = self._selector_map_getter()
        if not selector_map:
            return None
        wanted = self._element_session(session_id)
        for node in selector_map.values():
            if (
                node.backend_node_id == backend_node_id
                and (node.session_id or self._session_id) == wanted
            ):
                return node
        return None
