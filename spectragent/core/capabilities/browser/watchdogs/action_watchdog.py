"""ActionWatchdog — executes browser actions via CDP Input/Page/DOM domains.

Listens to action events (Click, Type, Scroll, Navigate, ...) and translates
them into CDP commands.  Uses ``DOMWatchdog.selector_map`` to resolve
``backend_node_id`` → element coordinates.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope, WatchdogBase
from agent_cdp.events.base import BaseEvent

from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMSelectorMap
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserErrorEvent,
    ClickCoordinateEvent,
    ClickElementEvent,
    DOMContentLoadedEvent,
    DoneEvent,
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
    TypeTextEvent,
)

logger = logging.getLogger(__name__)

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
    "enter":      ("Enter",      "Enter",       13, "\r"),
    "tab":        ("Tab",        "Tab",          9, "\t"),
    "escape":     ("Escape",     "Escape",      27, None),
    "backspace":  ("Backspace",  "Backspace",    8, None),
    "delete":     ("Delete",     "Delete",      46, None),
    "arrowup":    ("ArrowUp",    "ArrowUp",     38, None),
    "arrowdown":  ("ArrowDown",  "ArrowDown",   40, None),
    "arrowleft":  ("ArrowLeft",  "ArrowLeft",   37, None),
    "arrowright": ("ArrowRight", "ArrowRight",  39, None),
    "home":       ("Home",       "Home",        36, None),
    "end":        ("End",        "End",         35, None),
    "pageup":     ("PageUp",     "PageUp",      33, None),
    "pagedown":   ("PageDown",   "PageDown",    34, None),
    "space":      (" ",          "Space",       32, " "),
}


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


class ActionWatchdog(WatchdogBase):
    """Executes browser actions via CDP."""

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        selector_map_getter: Callable[[], DOMSelectorMap | None] | None = None,
        main_frame_id_getter: Callable[[], str | None] | None = None,
        debug_highlight: bool = False,
        debug_highlight_ms: int = 350,
    ) -> None:
        self._cdp = cdp
        self._selector_map_getter = selector_map_getter
        # Per-tab main frame id, used by _on_navigate to filter sub-frame
        # Page.lifecycleEvent. Optional — when None, we do not filter.
        self._main_frame_id_getter = main_frame_id_getter
        self._scope: EventScope | None = None
        self._session_id: str | None = None

        # Mouse state (persists across steps).
        self._mouse_x: int = 0
        self._mouse_y: int = 0

        # Debug highlight state — Overlay.enable is sent lazily on first use.
        self._debug_highlight = debug_highlight
        self._debug_highlight_ms = debug_highlight_ms
        self._overlay_enabled = False
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

        Click coordinates come from a live ``getBoundingClientRect`` (CSS
        viewport pixels) rather than ``node.bounds`` to immunise against
        three known sources of drift between observe and act:

        - DPR mismatch (DOMSnapshot bounds are device pixels)
        - document vs viewport coordinate space (snapshot bounds include
          scrollOffset; CDP Input.dispatchMouseEvent expects viewport)
        - layout shifts that happened after the snapshot was taken
        """
        node = self._resolve_node(event.backend_node_id)
        if node is None:
            return {"error": f"Element {event.backend_node_id} not in selector_map"}

        if node.bounds is None:
            return {"error": f"Element {event.backend_node_id} has no bounds"}

        await self._highlight_node(event.backend_node_id)

        live = await self._get_live_viewport_center(event.backend_node_id)
        if live is not None:
            cx, cy = live
        else:
            # Fallback path: post Layer-1 bounds are CSS pixels, so this is
            # still correct for the static-page case but vulnerable to
            # layout shift / scroll between observe and act.
            logger.warning(
                "Live viewport center unavailable for node %d; falling back "
                "to snapshot bounds",
                event.backend_node_id,
            )
            cx_f, cy_f = node.bounds.center
            cx, cy = int(cx_f), int(cy_f)

        return await self._click_at(
            cx, cy, event.button, event.click_count,
            label=self._describe_node(node),
        )

    async def _on_click_coordinate(self, event: ClickCoordinateEvent) -> dict[str, Any] | None:
        """Click at absolute coordinates."""
        return await self._click_at(
            event.coordinate_x, event.coordinate_y,
            event.button, event.click_count,
            label=f"({event.coordinate_x}, {event.coordinate_y})",
        )

    async def _click_at(
        self, x: int, y: int, button: str, click_count: int,
        *, label: str = "",
    ) -> dict[str, Any]:
        """Execute click at (x, y) via CDP Input domain."""
        sid = self._session_id
        try:
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": x, "y": y,
            }, session_id=sid)
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y,
                "button": button, "clickCount": click_count,
            }, session_id=sid)
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y,
                "button": button, "clickCount": click_count,
            }, session_id=sid)
            self._mouse_x, self._mouse_y = x, y
            await self._paint_click(x, y, label)
            return {"clicked": True, "x": x, "y": y}
        except Exception as e:
            logger.error("Click failed at (%d, %d): %s", x, y, e)
            return {"error": str(e)}

    # -----------------------------------------------------------------------
    # Type
    # -----------------------------------------------------------------------

    async def _on_type_text(self, event: TypeTextEvent) -> dict[str, Any] | None:
        """Type text into an element via CDP Input.insertText (bulk)."""
        node = self._resolve_node(event.backend_node_id)
        if node is None:
            return {"error": f"Element {event.backend_node_id} not in selector_map"}

        await self._highlight_node(event.backend_node_id)
        sid = self._session_id
        try:
            # Focus the element.
            await self._cdp.send("DOM.focus", {"backendNodeId": event.backend_node_id}, session_id=sid)

            # Clear existing content if requested.
            if event.clear:
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyDown", "key": "a", "commands": ["selectAll"],
                }, session_id=sid)
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": "a",
                }, session_id=sid)
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyDown", "key": "Backspace",
                }, session_id=sid)
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyUp", "key": "Backspace",
                }, session_id=sid)

            # Bulk insert text (single CDP call, not per-character).
            await self._cdp.send("Input.insertText", {"text": event.text}, session_id=sid)

            return {"typed": True, "length": len(event.text)}
        except Exception as e:
            logger.error("Type failed: %s", e)
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

            sid = self._session_id
            if event.backend_node_id is not None:
                node = self._resolve_node(event.backend_node_id)
                if node is not None:
                    live = await self._get_live_viewport_center(event.backend_node_id)
                    if live is not None:
                        cx, cy = live
                    elif node.bounds is not None:
                        cx_f, cy_f = node.bounds.center
                        cx, cy = int(cx_f), int(cy_f)
                    else:
                        return
                    await self._cdp.send("Input.dispatchMouseEvent", {
                        "type": "mouseWheel", "x": cx, "y": cy,
                        "deltaX": dx, "deltaY": dy,
                    }, session_id=sid)
            else:
                await self._cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel",
                    "x": self._mouse_x, "y": self._mouse_y,
                    "deltaX": dx, "deltaY": dy,
                }, session_id=sid)
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
                self._main_frame_id_getter()
                if self._main_frame_id_getter is not None
                else None
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
                if (
                    main_frame_id is not None
                    and e_frame is not None
                    and e_frame != main_frame_id
                ):
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
        self._scope.emit(NavigationStartedEvent(
            target_id=self._session_id or "", url=event.url,
        ))

        error_text: str | None = None
        try:
            try:
                result = await self._cdp.send(
                    "Page.navigate", {"url": event.url},
                    session_id=self._session_id,
                )
            except Exception as exc:
                logger.error("Page.navigate failed: %s", exc)
                self._scope.emit(BrowserErrorEvent(
                    error_type="navigation", message=str(exc),
                ))
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
                        event.wait_until, wait_s, event.url,
                    )

            self._scope.emit(NavigationCompleteEvent(
                target_id=self._session_id or "",
                url=event.url,
                error_text=error_text,
            ))
        finally:
            if connection is not None:
                try:
                    connection.disconnect()
                except Exception as exc:
                    logger.debug("Lifecycle connection disconnect failed: %s", exc)

    def _lifecycle_target(
        self, wait_until: str,
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
                lambda e: getattr(e, "name", "") in
                ("networkIdle", "networkAlmostIdle")
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
        await self._highlight_node(event.backend_node_id)
        try:
            sid = self._session_id
            # Resolve the backend node to a remote object.
            resolve_result = await self._cdp.send("DOM.resolveNode", {
                "backendNodeId": event.backend_node_id,
            }, session_id=sid)
            if not resolve_result or "object" not in resolve_result:
                logger.error("Cannot resolve node %d for dropdown", event.backend_node_id)
                return

            object_id = resolve_result["object"]["objectId"]

            if event.value is not None:
                await self._cdp.send("Runtime.callFunctionOn", {
                    "functionDeclaration": (
                        "function(value) {"
                        "  this.value = value;"
                        "  this.dispatchEvent(new Event('change', {bubbles: true}));"
                        "}"
                    ),
                    "objectId": object_id,
                    "arguments": [{"value": event.value}],
                }, session_id=sid)
            elif event.text is not None:
                await self._cdp.send("Runtime.callFunctionOn", {
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
                }, session_id=sid)
            elif event.index is not None:
                await self._cdp.send("Runtime.callFunctionOn", {
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
                }, session_id=sid)
        except Exception as e:
            logger.error("Select dropdown failed: %s", e)

    # -----------------------------------------------------------------------
    # Extract content
    # -----------------------------------------------------------------------

    async def _on_extract_content(self, event: ExtractContentEvent) -> str | None:
        """Extract text content from page or element."""
        sid = self._session_id
        try:
            if event.backend_node_id is not None:
                result = await self._cdp.send("DOM.getOuterHTML", {
                    "backendNodeId": event.backend_node_id,
                }, session_id=sid)
                return result.get("outerHTML", "") if result else ""
            else:
                result = await self._cdp.send("Runtime.evaluate", {
                    "expression": "document.body.innerText",
                }, session_id=sid)
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

    async def _highlight_node(self, backend_node_id: int) -> None:
        """DevTools-style highlight on the target node, then sleep briefly.

        No-op when ``debug_highlight`` is off. Lazy-enables the Overlay
        domain on first use (Overlay.enable is per-target/session).
        Failures are logged at debug level — highlighting is best-effort
        and must never block the underlying action.
        """
        if not self._debug_highlight:
            return
        sid = self._session_id
        try:
            if not self._overlay_enabled:
                await self._cdp.send("Overlay.enable", {}, session_id=sid)
                self._overlay_enabled = True
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

    async def _get_live_viewport_center(
        self, backend_node_id: int,
    ) -> tuple[int, int] | None:
        """Return the element's current viewport-relative center, or None.

        Pipeline: scroll the element into view, resolve it to a JS object,
        then read ``getBoundingClientRect()`` — those numbers are in CSS
        viewport pixels and are exactly what ``Input.dispatchMouseEvent``
        wants. Any failure → ``None`` so the caller can fall back to the
        snapshot bounds path.
        """
        sid = self._session_id
        try:
            try:
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
                    backend_node_id, exc,
                )

            resolved = await self._cdp.send(
                "DOM.resolveNode",
                {"backendNodeId": backend_node_id},
                session_id=sid,
            )
            if not resolved or "object" not in resolved:
                return None
            object_id = resolved["object"].get("objectId")
            if not object_id:
                return None

            rect_result = await self._cdp.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": (
                        "function() {"
                        "  const r = this.getBoundingClientRect();"
                        "  return {x: r.x, y: r.y, w: r.width, h: r.height};"
                        "}"
                    ),
                    "objectId": object_id,
                    "returnByValue": True,
                },
                session_id=sid,
            )
            value = (rect_result or {}).get("result", {}).get("value")
            if not isinstance(value, dict):
                return None
            try:
                x = float(value["x"])
                y = float(value["y"])
                w = float(value["w"])
                h = float(value["h"])
            except (KeyError, TypeError, ValueError):
                return None
            return int(x + w / 2), int(y + h / 2)
        except Exception as exc:
            logger.warning(
                "Live viewport center query failed for node %d: %s",
                backend_node_id, exc,
            )
            return None

    def _resolve_node(self, backend_node_id: int) -> DOMElementNode | None:
        """Look up a node from DOMWatchdog's selector_map by backend_node_id."""
        if self._selector_map_getter is None:
            return None
        selector_map = self._selector_map_getter()
        if selector_map is None:
            return None
        # selector_map is keyed by backend_node_id (since both BrowserUseCleaner
        # and SkyvernCleaner now use this key).
        return selector_map.get(backend_node_id)
