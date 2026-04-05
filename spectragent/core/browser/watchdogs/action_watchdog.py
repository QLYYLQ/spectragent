"""ActionWatchdog — executes browser actions via CDP Input/Page/DOM domains.

Listens to action events (Click, Type, Scroll, Navigate, ...) and translates
them into CDP commands.  Uses ``DOMWatchdog.selector_map`` to resolve
``backend_node_id`` → element coordinates.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent_cdp import CDPCommandProtocol, ConnectionType, EventScope, WatchdogBase

from spectragent.core.browser.dom.models import DOMElementNode, DOMSelectorMap
from spectragent.core.browser.watchdogs.events import (
    BrowserErrorEvent,
    ClickCoordinateEvent,
    ClickElementEvent,
    DoneEvent,
    ExtractContentEvent,
    GoBackEvent,
    GoForwardEvent,
    NavigateToUrlEvent,
    NavigationCompleteEvent,
    NavigationStartedEvent,
    RefreshEvent,
    ScrollEvent,
    SelectDropdownEvent,
    SendKeysEvent,
    TypeTextEvent,
)

logger = logging.getLogger(__name__)

# Key name → CDP key identifier mapping (common keys).
_KEY_MAP: dict[str, str] = {
    "enter": "Enter", "tab": "Tab", "escape": "Escape",
    "backspace": "Backspace", "delete": "Delete",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "home": "Home", "end": "End",
    "pageup": "PageUp", "pagedown": "PageDown",
    "space": " ",
}


class ActionWatchdog(WatchdogBase):
    """Executes browser actions via CDP."""

    def __init__(
        self,
        cdp: CDPCommandProtocol,
        *,
        selector_map_getter: Callable[[], DOMSelectorMap | None] | None = None,
    ) -> None:
        self._cdp = cdp
        self._selector_map_getter = selector_map_getter
        self._scope: EventScope | None = None
        self._session_id: str | None = None

        # Mouse state (persists across steps).
        self._mouse_x: int = 0
        self._mouse_y: int = 0

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
        """Click element by backend_node_id → resolve to coordinates → click."""
        node = self._resolve_node(event.backend_node_id)
        if node is None:
            return {"error": f"Element {event.backend_node_id} not in selector_map"}

        if node.bounds is None:
            return {"error": f"Element {event.backend_node_id} has no bounds"}

        cx, cy = node.bounds.center
        return await self._click_at(int(cx), int(cy), event.button, event.click_count)

    async def _on_click_coordinate(self, event: ClickCoordinateEvent) -> dict[str, Any] | None:
        """Click at absolute coordinates."""
        return await self._click_at(event.coordinate_x, event.coordinate_y, event.button, event.click_count)

    async def _click_at(
        self, x: int, y: int, button: str, click_count: int
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
                if node and node.bounds:
                    cx, cy = node.bounds.center
                    await self._cdp.send("Input.dispatchMouseEvent", {
                        "type": "mouseWheel", "x": int(cx), "y": int(cy),
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
        """Navigate to a URL."""
        try:
            if self._scope:
                self._scope.emit(NavigationStartedEvent(
                    target_id=self._session_id or "", url=event.url,
                ))

            result = await self._cdp.send("Page.navigate", {"url": event.url}, session_id=self._session_id)

            # Page.navigate returns errorText on failure, no HTTP status code.
            error_text = result.get("errorText") if result else None
            if self._scope:
                self._scope.emit(NavigationCompleteEvent(
                    target_id=self._session_id or "",
                    url=event.url,
                    error_text=error_text,
                ))
        except Exception as e:
            logger.error("Navigate failed: %s", e)
            if self._scope:
                self._scope.emit(BrowserErrorEvent(
                    error_type="navigation", message=str(e),
                ))

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
        """Send keyboard keys, supports modifiers (e.g. 'Ctrl+a', 'Enter')."""
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

            # Resolve key name.
            resolved = _KEY_MAP.get(key.lower(), key)

            await self._cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": resolved, "modifiers": mod_mask,
            }, session_id=self._session_id)
            await self._cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": resolved, "modifiers": mod_mask,
            }, session_id=self._session_id)
        except Exception as e:
            logger.error("SendKeys failed: %s", e)

    # -----------------------------------------------------------------------
    # Select dropdown — uses DOM.resolveNode + Runtime.callFunctionOn
    # -----------------------------------------------------------------------

    async def _on_select_dropdown(self, event: SelectDropdownEvent) -> None:
        """Select a dropdown option via CDP DOM.resolveNode + Runtime.callFunctionOn.

        Uses proper CDP argument binding to avoid JavaScript injection.
        """
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
