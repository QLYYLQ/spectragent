"""ActionWatchdog — executes browser actions via CDP Input/Page/DOM domains.

Listens to action events (Click, Type, Scroll, Navigate, ...) and translates
them into CDP commands.  Uses ``DOMWatchdog.selector_map`` to resolve an
element ``(session_id, backend_node_id)`` → element coordinates.

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
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope
from agent_cdp.events.base import BaseEvent

from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMSelectorMap
from spectragent.core.capabilities.browser.dom.sensitivity import is_sensitive_control
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
from spectragent.core.capabilities.browser.watchdogs.base import (
    BrowserWatchdog,
    WatchdogContext,
    WatchdogLevel,
)
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

logger = logging.getLogger(__name__)

#: The element that receives keystrokes, as the sensitivity rule reads it:
#: its tag and the four attributes the rule looks at. Follows the focus
#: through open shadow roots and frames this document can reach; a frame it
#: cannot look into (another process, another origin) is reported opaque.
_FOCUSED_FIELD_JS = """(() => {
  let el = document.activeElement;
  for (let hops = 0; el && hops < 64; hops += 1) {
    if (el.shadowRoot && el.shadowRoot.activeElement) {
      el = el.shadowRoot.activeElement;
      continue;
    }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      let inner = null;
      try { inner = el.contentDocument; } catch (e) { inner = null; }
      if (!inner) return {opaque: true};
      el = inner.activeElement;
      continue;
    }
    break;
  }
  if (!el) return null;
  const attrs = {};
  for (const name of ['type', 'autocomplete', 'id', 'name']) {
    const value = el.getAttribute(name);
    if (value !== null) attrs[name] = value;
  }
  return {tag: el.tagName, attrs};
})()"""


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


# Key name → (DOM `key`, DOM `code`, virtual key code, optional `char`
# text) for CDP Input.dispatchKeyEvent. The 4th element, when not None,
# means this key has a text interpretation and a separate ``type=char``
# event should be sandwiched between keyDown and keyUp; without that
# event Chrome won't fire ``keypress`` and many JS handlers (Amazon's
# search-on-Enter, autosuggest selectors, contenteditable inputs) treat
# the press as half-hearted and skip their reaction. Two-event keyDown
# + keyUp is enough for navigation keys (arrows, Esc, Home/End, Pg*).
# Reference: ref/browser-use/browser_use/actor/element.py:423-501.
_KEY_INFO: dict[str, tuple[str, str, int, str | None]] = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, "\t"),
    "escape": ("Escape", "Escape", 27, None),
    "backspace": ("Backspace", "Backspace", 8, None),
    "delete": ("Delete", "Delete", 46, None),
    "arrowup": ("ArrowUp", "ArrowUp", 38, None),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, None),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, None),
    "arrowright": ("ArrowRight", "ArrowRight", 39, None),
    "home": ("Home", "Home", 36, None),
    "end": ("End", "End", 35, None),
    "pageup": ("PageUp", "PageUp", 33, None),
    "pagedown": ("PageDown", "PageDown", 34, None),
    "space": (" ", "Space", 32, " "),
}


#: What a click on a frame element that can no longer be located reports.
_STALE_FRAME_ELEMENT = (
    "The element is inside a frame that changed since the last observation "
    "(navigated, reloaded or removed), so it cannot be located; observe the "
    "page again before acting on it."
)


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


def _resolve_key_event(raw: str) -> tuple[dict[str, Any], str | None]:
    """Build CDP Input.dispatchKeyEvent params for one logical key.

    Returns ``(base_payload, char_text)``:

    - ``base_payload`` is the shared subset (key, code,
      windowsVirtualKeyCode, nativeVirtualKeyCode) that the caller wraps
      with ``type=keyDown`` / ``type=keyUp`` and any ``modifiers``.
    - ``char_text`` is the text to dispatch as a ``type=char`` event
      between keyDown and keyUp; ``None`` skips the char event.

    Single printable characters (``"a"``, ``"5"``, ``"%"``) always emit
    a char event with the character itself. Named navigation keys
    (arrows, Esc, ...) skip char. Enter / Tab / Space carry their
    canonical text representation per the table above.
    """
    info = _KEY_INFO.get(raw.lower())
    if info is not None:
        key, code, vk, char = info
        return (
            {
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
            },
            char,
        )
    if len(raw) == 1:
        ch = raw
        upper = ch.upper()
        if ch.isalpha():
            code = f"Key{upper}"
        elif ch.isdigit():
            code = f"Digit{ch}"
        else:
            code = upper
        return (
            {
                "key": ch,
                "code": code,
                "windowsVirtualKeyCode": ord(upper),
                "nativeVirtualKeyCode": ord(upper),
            },
            ch,
        )
    # Unknown multi-char key — pass through key-only.
    return ({"key": raw}, None)


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
        return cls(
            ctx.cdp,
            selector_map_getter=ctx.selector_map_getter,
            main_frame_id_getter=ctx.main_frame_id_getter,
            debug_highlight=ctx.debug_highlight,
            mouse_trajectory=ctx.mouse_trajectory,
            typing_rhythm=ctx.typing_rhythm,
            viewport=ctx.viewport,
        )

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        selector_map_getter: Callable[[], DOMSelectorMap | None] | None = None,
        main_frame_id_getter: Callable[[], str | None] | None = None,
        debug_highlight: bool = False,
        debug_highlight_ms: int = 350,
        mouse_trajectory: MouseTrajectoryProtocol | None = None,
        typing_rhythm: TypingRhythmProtocol | None = None,
        viewport: tuple[int, int] = (1280, 720),
    ) -> None:
        self._cdp = cdp
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
            viewport=viewport,
            move_callback=self._overlay_move_cursor if debug_highlight else None,
        )

        # Keystroke middle layer — owns the typing rhythm and performs every
        # text entry. Same shape and same reason as the mouse: the ``instant``
        # rhythm (also the fallback when none is configured) is the legacy
        # single bulk ``Input.insertText``.
        self._keyboard = HumanKeyboard(cdp, rhythm=typing_rhythm)

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

    def attach(self, scope: EventScope, session_id: str | None = None) -> None:
        self._scope = scope
        self._session_id = session_id

        for event_type, handler in [
            (ClickElementEvent, self._on_click_element),
            (ClickCoordinateEvent, self._on_click_coordinate),
            (TypeTextEvent, self._on_type_text),
            (TypeAtCoordinateEvent, self._on_type_at_coordinate),
            (DragEvent, self._on_drag),
            (ScrollEvent, self._on_scroll),
            (NavigateToUrlEvent, self._on_navigate),
            (SendKeysEvent, self._on_send_keys),
            (GoBackEvent, self._on_go_back),
            (GoForwardEvent, self._on_go_forward),
            (RefreshEvent, self._on_refresh),
            (SelectDropdownEvent, self._on_select_dropdown),
            (ExtractContentEvent, self._on_extract_content),
            (DoneEvent, self._on_done),
        ]:
            scope.connect(event_type, handler, mode=ConnectionType.QUEUED)

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

        ax, ay = self._aim_inside(cx, cy, target_w, target_h)
        return await self._click_at(
            ax,
            ay,
            event.button,
            event.click_count,
            label=self._describe_node(node),
            target_w=target_w,
            target_h=target_h,
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
    ) -> dict[str, Any]:
        """Execute click at (x, y) via the HumanMouse middle layer.

        Cursor movement (a single jump with the ``instant`` strategy, a full
        human trajectory otherwise) plus press/release are owned by
        ``self._mouse``.
        The returned coords are the *requested* target, unchanged.
        """
        try:
            await self._mouse.click(
                x,
                y,
                button,
                click_count,
                target_w=target_w,
                target_h=target_h,
            )
            await self._paint_click(x, y, label)
            return {"clicked": True, "x": x, "y": y}
        except Exception as e:
            logger.error("Click failed at (%d, %d): %s", x, y, e)
            return {"error": str(e)}

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
            return await self._insert_text_at_focus(event.text, event.clear, session_id=sid)
        except Exception as e:
            logger.error("Type failed: %s", e)
            return {"error": str(e)}

    async def _on_type_at_coordinate(self, event: TypeAtCoordinateEvent) -> dict[str, Any] | None:
        """Type into whatever is at (x, y) — the vision-only path.

        A vision-only agent negotiates no DOM fragment, so the
        selector_map is empty and ``DOM.focus`` has nothing to aim at.
        Clicking the coordinate is what moves focus; everything after it
        (select-all + Backspace, ``Input.insertText``) is focus-relative
        already and is shared verbatim with :meth:`_on_type_text`.
        """
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
            return await self._insert_text_at_focus(event.text, event.clear)
        except Exception as e:
            logger.error("Type at coordinate failed: %s", e)
            return {"error": str(e)}

    async def _insert_text_at_focus(
        self, text: str, clear: bool, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """Clear (optionally) and enter *text* into the focused node.

        Every CDP call here targets the *current focus*, never a node id —
        which is why the coordinate path can reuse it after a click.
        ``session_id`` is the target the focus was put in; ``None`` is the
        tab, whose input Chrome routes to its focused frame.

        The text itself belongs to :class:`HumanKeyboard`: one bulk
        ``Input.insertText`` under the ``instant`` rhythm (the default), a
        real key event per character on modelled timings under any other.
        The clear is also keyboard input: ``HumanKeyboard`` sends a complete
        select-all shortcut followed by Backspace, with the configured
        rhythm deciding its timing.

        Right before the text goes in, the focused element is judged by the
        sensitivity rule; the verdict comes back as ``sensitive`` and the
        action records it (``ActionResult.typed_into_sensitive_field``).
        """
        sid = self._element_session(session_id)
        if clear:
            await self._keyboard.clear_focused(session_id=sid)

        sensitive = await self._focused_field_is_sensitive(sid)
        await self._keyboard.type_text(text, session_id=sid)
        return {"typed": True, "length": len(text), "sensitive": sensitive}

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
        """
        try:
            await self._mouse.drag(
                event.start_x, event.start_y, event.end_x, event.end_y, event.button
            )
            return {
                "dragged": True,
                "from": [event.start_x, event.start_y],
                "to": [event.end_x, event.end_y],
            }
        except Exception as e:
            logger.error(
                "Drag failed (%d, %d) -> (%d, %d): %s",
                event.start_x,
                event.start_y,
                event.end_x,
                event.end_y,
                e,
            )
            return {"error": str(e)}

    # -----------------------------------------------------------------------
    # Scroll
    # -----------------------------------------------------------------------

    async def _on_scroll(self, event: ScrollEvent) -> None:
        """Scroll the viewport or a specific element."""
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
                if node is not None:
                    owners = node.frame_owners()
                    try:
                        live = await self._get_live_viewport_center(
                            event.backend_node_id, event.session_id, owners
                        )
                    except FrameGeometryError as exc:
                        logger.warning("Not scrolling %s: %s", self._describe_node(node), exc)
                        return
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
                        return
                    elif node.bounds is not None:
                        cx_f, cy_f = node.bounds.center
                        cx, cy = self._aim_inside(cx_f, cy_f, node.bounds.width, node.bounds.height)
                    else:
                        return
                    await self._mouse.wheel(cx, cy, dx, dy)
            else:
                mx, my = self._mouse.position
                await self._mouse.wheel(mx, my, dx, dy)
        except Exception as e:
            logger.error("Scroll failed: %s", e)

    # -----------------------------------------------------------------------
    # Navigation
    # -----------------------------------------------------------------------

    async def _on_navigate(self, event: NavigateToUrlEvent) -> None:
        """Navigate to a URL and (optionally) wait for a Page lifecycle phase.

        ``wait_until`` honours ``"load"`` / ``"domcontentloaded"`` /
        ``"networkidle"``. Anything else (including the empty string)
        means fire-and-forget — emit ``NavigationCompleteEvent`` as soon
        as ``Page.navigate`` returns.

        Lifecycle waiter is registered **synchronously** before
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

        Timeout is ``lifecycle_timeout_ms`` (or ``timeout_ms``, or 30s).
        On timeout we log a warning and emit ``NavigationCompleteEvent``
        normally — settle behaviour for popup-after-load is the job of
        ``post_delay_ms`` / ``browser.wait``, not this code path.
        """
        if not self._scope:
            return

        # 1. Pick lifecycle event class + base predicate from wait_until.
        lifecycle_target = self._lifecycle_target(event.wait_until)

        # 2. Synchronously register the waiter BEFORE Page.navigate.
        future: asyncio.Future[Any] | None = None
        connection: Any = None
        if lifecycle_target is not None:
            target_evt, base_predicate = lifecycle_target
            future = asyncio.get_running_loop().create_future()
            main_frame_id = (
                self._main_frame_id_getter() if self._main_frame_id_getter is not None else None
            )
            # ``main_loader_holder`` is mutated after Page.navigate
            # returns so we can tighten the predicate to the just-issued
            # navigation. We use a list as a poor-man's cell.
            main_loader_holder: list[str | None] = [None]

            def _waiter(e: Any) -> None:
                if future is None or future.done():
                    return
                if not base_predicate(e):
                    return
                # Main-frame filter (only when both sides are known).
                e_frame = getattr(e, "frame_id", None)
                if main_frame_id is not None and e_frame is not None and e_frame != main_frame_id:
                    return
                # Loader filter (only when both sides are known).
                e_loader = getattr(e, "loader_id", None)
                expected_loader = main_loader_holder[0]
                if (
                    expected_loader is not None
                    and e_loader is not None
                    and e_loader != expected_loader
                ):
                    return
                future.set_result(e)

            connection = self._scope.connect(
                target_evt,
                _waiter,
                mode=ConnectionType.DIRECT,
                priority=-1000,
            )

        # 3. Emit start, then fire Page.navigate, then wait, then emit complete.
        self._scope.emit(
            NavigationStartedEvent(
                target_id=self._session_id or "",
                url=event.url,
            )
        )

        error_text: str | None = None
        try:
            try:
                result = await self._cdp.send(
                    "Page.navigate",
                    {"url": event.url},
                    session_id=self._session_id,
                )
            except Exception as exc:
                logger.error("Page.navigate failed: %s", exc)
                self._scope.emit(
                    BrowserErrorEvent(
                        error_type="navigation",
                        message=str(exc),
                    )
                )
                return

            if result:
                error_text = result.get("errorText")
                # Tighten the predicate to this navigation's loader.
                if future is not None:
                    main_loader_holder[0] = result.get("loaderId")

            if future is not None:
                wait_s = self._resolve_lifecycle_timeout_s(event)
                try:
                    await asyncio.wait_for(future, timeout=wait_s)
                except TimeoutError:
                    logger.warning(
                        "navigate wait_until=%r timed out after %.1fs on %s — "
                        "emitting NavigationCompleteEvent anyway; tune "
                        "post_delay_ms or use browser.wait if popups arrive "
                        "later via JS timer",
                        event.wait_until,
                        wait_s,
                        event.url,
                    )

            self._scope.emit(
                NavigationCompleteEvent(
                    target_id=self._session_id or "",
                    url=event.url,
                    error_text=error_text,
                )
            )
        finally:
            if connection is not None:
                try:
                    connection.disconnect()
                except Exception as exc:
                    logger.debug("Lifecycle connection disconnect failed: %s", exc)

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

    @staticmethod
    def _resolve_lifecycle_timeout_s(event: NavigateToUrlEvent) -> float:
        """``lifecycle_timeout_ms`` → ``timeout_ms`` → 30000ms, in seconds."""
        ms = event.lifecycle_timeout_ms
        if ms is None:
            ms = event.timeout_ms
        if ms is None:
            ms = 30000
        return ms / 1000

    async def _on_go_back(self, _event: GoBackEvent) -> None:
        try:
            await self._cdp.send("Page.goBack", {}, session_id=self._session_id)
        except Exception as e:
            logger.error("GoBack failed: %s", e)

    async def _on_go_forward(self, _event: GoForwardEvent) -> None:
        try:
            await self._cdp.send("Page.goForward", {}, session_id=self._session_id)
        except Exception as e:
            logger.error("GoForward failed: %s", e)

    async def _on_refresh(self, _event: RefreshEvent) -> None:
        try:
            await self._cdp.send("Page.reload", {}, session_id=self._session_id)
        except Exception as e:
            logger.error("Refresh failed: %s", e)

    # -----------------------------------------------------------------------
    # Send keys
    # -----------------------------------------------------------------------

    async def _on_send_keys(self, event: SendKeysEvent) -> None:
        """Send keyboard keys, supports modifiers (e.g. 'Ctrl+a', 'Enter').

        Uses ``_resolve_key_event`` to fill ``code`` /
        ``windowsVirtualKeyCode`` / ``nativeVirtualKeyCode`` so the
        synthesized DOM ``KeyboardEvent`` has a non-zero ``keyCode`` and
        a real ``code`` string — both of which JS handlers (especially
        React's onKeyDown) check before treating the press as Enter.
        """
        try:
            parts = event.keys.split("+")
            key = parts[-1].strip()
            modifiers = [p.strip().lower() for p in parts[:-1]]

            # Build modifier bitmask.
            mod_mask = 0
            if "ctrl" in modifiers or "control" in modifiers:
                mod_mask |= 1
            if "alt" in modifiers:
                mod_mask |= 2
            if "shift" in modifiers:
                mod_mask |= 8
            if "meta" in modifiers or "cmd" in modifiers:
                mod_mask |= 4

            payload: dict[str, Any]
            char_text: str | None
            payload, char_text = _resolve_key_event(key)
            await self._cdp.send(
                "Input.dispatchKeyEvent",
                {**payload, "type": "keyDown", "modifiers": mod_mask},
                session_id=self._session_id,
            )
            if char_text is not None:
                await self._cdp.send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "char",
                        "text": char_text,
                        "key": payload.get("key", char_text),
                        "modifiers": mod_mask,
                    },
                    session_id=self._session_id,
                )
            await self._cdp.send(
                "Input.dispatchKeyEvent",
                {**payload, "type": "keyUp", "modifiers": mod_mask},
                session_id=self._session_id,
            )
        except Exception as e:
            logger.error("SendKeys failed: %s", e)

    # -----------------------------------------------------------------------
    # Select dropdown — uses DOM.resolveNode + Runtime.callFunctionOn
    # -----------------------------------------------------------------------

    async def _on_select_dropdown(self, event: SelectDropdownEvent) -> None:
        """Select a dropdown option via CDP DOM.resolveNode + Runtime.callFunctionOn.

        Uses proper CDP argument binding to avoid JavaScript injection.
        """
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
                logger.error("Cannot resolve node %d for dropdown", event.backend_node_id)
                return

            object_id = resolve_result["object"]["objectId"]

            if event.value is not None:
                await self._cdp.send(
                    "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": (
                            "function(value) {"
                            "  this.value = value;"
                            "  this.dispatchEvent(new Event('change', {bubbles: true}));"
                            "}"
                        ),
                        "objectId": object_id,
                        "arguments": [{"value": event.value}],
                    },
                    session_id=sid,
                )
            elif event.text is not None:
                await self._cdp.send(
                    "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": (
                            "function(text) {"
                            "  for (const opt of this.options) {"
                            "    if (opt.text.trim() === text) {"
                            "      this.value = opt.value;"
                            "      this.dispatchEvent(new Event('change', {bubbles: true}));"
                            "      return;"
                            "    }"
                            "  }"
                            "}"
                        ),
                        "objectId": object_id,
                        "arguments": [{"value": event.text}],
                    },
                    session_id=sid,
                )
            elif event.index is not None:
                await self._cdp.send(
                    "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": (
                            "function(idx) {"
                            "  if (this.options && this.options[idx]) {"
                            "    this.selectedIndex = idx;"
                            "    this.dispatchEvent(new Event('change', {bubbles: true}));"
                            "  }"
                            "}"
                        ),
                        "objectId": object_id,
                        "arguments": [{"value": event.index}],
                    },
                    session_id=sid,
                )
        except Exception as e:
            logger.error("Select dropdown failed: %s", e)

    # -----------------------------------------------------------------------
    # Extract content
    # -----------------------------------------------------------------------

    async def _on_extract_content(self, event: ExtractContentEvent) -> str | None:
        """Extract text content from page or element."""
        try:
            if event.backend_node_id is not None:
                result = await self._cdp.send(
                    "DOM.getOuterHTML",
                    {
                        "backendNodeId": event.backend_node_id,
                    },
                    session_id=self._element_session(event.session_id),
                )
                return result.get("outerHTML", "") if result else ""
            else:
                result = await self._cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": "document.body.innerText",
                    },
                    session_id=self._session_id,
                )
                return result.get("result", {}).get("value", "") if result else ""
        except Exception as e:
            logger.error("Extract content failed: %s", e)
            return None

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
        return session_id if session_id is not None else self._session_id

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
                and self._element_session(node.session_id) == wanted
            ):
                return node
        return None
