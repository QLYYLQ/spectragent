"""BrowserSession -- core browser automation session.

Wraps a CDP WebSocket connection + ScopeGroup (multi-tab) + watchdogs.
The session implements the ``Environment`` protocol from
``agents/env_interfaces.py``, translating ``observe()/act()`` into
agent-cdp events.

Key design: each tab gets an independent EventScope with its own watchdogs,
enabling parallel multi-agent operation on different tabs via session_id routing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent_cdp import (
    EventScope,
    ScopeGroup,
)

from spectragent.core.browser.cdp_client import CDPClient
from spectragent.core.browser.dom.models import DOMSelectorMap, DOMState
from spectragent.core.browser.profiles import BrowserProfileConfig, resolve_profile
from spectragent.core.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
    ClickCoordinateEvent,
    ClickElementEvent,
    CloseTabEvent,
    DoneEvent,
    ExtractContentEvent,
    GoBackEvent,
    GoForwardEvent,
    NavigateToUrlEvent,
    RefreshEvent,
    ScreenshotRequestEvent,
    ScrollEvent,
    SelectDropdownEvent,
    SendKeysEvent,
    SwitchTabEvent,
    TypeTextEvent,
)
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult

logger = logging.getLogger(__name__)

# Action name -> event class mapping.
_ACTION_EVENT_MAP: dict[str, type] = {
    "click": ClickElementEvent,
    "click_coordinate": ClickCoordinateEvent,
    "type": TypeTextEvent,
    "scroll": ScrollEvent,
    "navigate": NavigateToUrlEvent,
    "send_keys": SendKeysEvent,
    "go_back": GoBackEvent,
    "go_forward": GoForwardEvent,
    "refresh": RefreshEvent,
    "switch_tab": SwitchTabEvent,
    "close_tab": CloseTabEvent,
    "screenshot": ScreenshotRequestEvent,
    "select_dropdown": SelectDropdownEvent,
    "extract_content": ExtractContentEvent,
    "done": DoneEvent,
}

# Action descriptors exposed to agents.
_ACTION_DESCRIPTORS: list[ActionDescriptor] = [
    ActionDescriptor(
        name="click",
        description="Click a DOM element by its index in the selector map.",
        params={"index": "int -- element index from selector_map"},
    ),
    ActionDescriptor(
        name="click_coordinate",
        description="Click at absolute screen coordinates.",
        params={"coordinate_x": "int", "coordinate_y": "int"},
    ),
    ActionDescriptor(
        name="type",
        description="Type text into a DOM element.",
        params={"index": "int -- element index", "text": "str", "clear": "bool = True"},
    ),
    ActionDescriptor(
        name="scroll",
        description="Scroll the page or a specific element.",
        params={"direction": "up|down|left|right", "amount": "int = 300"},
    ),
    ActionDescriptor(
        name="navigate",
        description="Navigate to a URL.",
        params={"url": "str"},
    ),
    ActionDescriptor(
        name="send_keys",
        description="Send keyboard keys (e.g. Enter, Escape, Ctrl+A).",
        params={"keys": "str"},
    ),
    ActionDescriptor(
        name="go_back",
        description="Navigate back in browser history.",
        params={},
    ),
    ActionDescriptor(
        name="go_forward",
        description="Navigate forward in browser history.",
        params={},
    ),
    ActionDescriptor(
        name="switch_tab",
        description="Switch to a different browser tab.",
        params={"target_id": "str | None -- None = most recent"},
    ),
    ActionDescriptor(
        name="done",
        description="Signal that the task is complete.",
        params={"extracted_content": "str | None"},
    ),
]

# Watchdog level classification.
_TAB_WATCHDOGS = {"dom", "action", "screenshot", "popups", "security"}
_BROWSER_WATCHDOGS = {"crash", "downloads"}

# Scope ID for browser-level (non-tab-specific) watchdogs.
_GLOBAL_SCOPE_ID = "__browser__"

class BrowserSession:
    """Core browser session -- implements Environment protocol.

    Lifecycle::

        session = BrowserSession("ws://localhost:9222/devtools/browser/...")
        await session.setup()
        sid = session.default_session_id
        obs = await session.observe(sid)
        result = await session.act(Action(name="click", params={"index": 5}), sid)
        await session.teardown()

    Multi-tab parallel usage::

        sid_1 = session.default_session_id
        sid_2 = await session.create_tab("https://example.com")
        # Parallel -- safe, each session_id routes to independent scope
        obs_a, obs_b = await asyncio.gather(
            session.observe(sid_1),
            session.observe(sid_2),
        )
    """

    def __init__(
        self,
        cdp_url: str,
        *,
        cdp_headers: dict[str, str] | None = None,
        cdp_client: CDPClient | None = None,
        profile: str | BrowserProfileConfig = "standard",
        viewport: tuple[int, int] = (1280, 720),
    ) -> None:
        self._cdp_url = cdp_url
        self._cdp_headers = cdp_headers
        self._injected_client = cdp_client
        self._viewport = viewport

        # Resolve profile.
        if isinstance(profile, str):
            self._profile = resolve_profile(profile)
        else:
            self._profile = profile

        # ScopeGroup for multi-tab.
        self._scope_group = ScopeGroup("browser-session")

        # CDP client (set during setup).
        self._cdp: CDPClient | None = None

        # Per-tab state keyed by CDP session_id.
        self._scopes: dict[str, EventScope] = {}
        self._selector_maps: dict[str, DOMSelectorMap] = {}
        self._target_to_session: dict[str, str] = {}
        self._session_to_target: dict[str, str] = {}
        self._watchdogs: dict[str, list[Any]] = {}

        # Global scope for browser-level watchdogs.
        self._global_scope: EventScope | None = None

        # Default session_id (initial tab).
        self._default_session_id: str | None = None

        self._alive = False

    # -------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------

    @property
    def default_session_id(self) -> str:
        """Session ID of the initial tab -- convenience for single-agent usage."""
        if self._default_session_id is None:
            raise RuntimeError("Session not set up -- call setup() first")
        return self._default_session_id

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def cdp(self) -> CDPClient:
        """CDP client -- available after setup()."""
        if self._cdp is None:
            raise RuntimeError("Session not set up -- call setup() first")
        return self._cdp

    # -------------------------------------------------------------------
    # Environment Protocol
    # -------------------------------------------------------------------

    async def setup(self, **_kwargs: Any) -> None:
        """Initialize the session -- CDPClient + global scope + initial tab."""
        # 1. CDPClient (WebSocket).
        if self._injected_client is not None:
            self._cdp = self._injected_client
        else:
            self._cdp = CDPClient(
                self._cdp_url,
                additional_headers=self._cdp_headers,
            )
            await self._cdp.start()

        # 2. Global scope -- browser-level watchdogs.
        self._global_scope = await self._scope_group.create_scope(_GLOBAL_SCOPE_ID)
        self._attach_browser_level_watchdogs(self._global_scope)

        # 3. Initial tab.
        result = await self._cdp.send("Target.getTargets", {})
        targets = (result or {}).get("targetInfos", [])
        page_target = next(
            (t for t in targets if t.get("type") == "page"), None
        )

        if page_target:
            sid = await self._attach_tab(
                page_target["targetId"], page_target.get("url", "")
            )
        else:
            r = await self._cdp.send(
                "Target.createTarget", {"url": "about:blank"}
            )
            target_id = (r or {}).get("targetId")
            if not target_id:
                raise RuntimeError("Target.createTarget returned no targetId")
            sid = await self._attach_tab(target_id, "about:blank")

        self._default_session_id = sid
        self._alive = True

        logger.info(
            "BrowserSession setup complete. Default session: %s",
            self._default_session_id,
        )

    async def teardown(self) -> None:
        """Tear down all scopes and close CDPClient if we own it."""
        self._alive = False
        await self._scope_group.close_all()
        self._scopes.clear()
        self._selector_maps.clear()
        self._watchdogs.clear()
        self._target_to_session.clear()
        self._session_to_target.clear()
        self._global_scope = None
        self._default_session_id = None

        if self._cdp is not None and self._injected_client is None:
            await self._cdp.stop()
        self._cdp = None

        logger.info("BrowserSession teardown complete")

    async def observe(self, session_id: str | None = None) -> dict:
        """Observe the specified tab's browser state (DOM + screenshot).

        Multi-agent safe: each session_id routes to its own independent scope.
        Returns a plain dict — will be replaced by Fragment system in M13.
        """
        sid = session_id or self._default_session_id
        if sid is None:
            return {"timestamp": time.time()}

        scope = self._scopes.get(sid)
        if scope is None:
            raise ValueError(f"Unknown session_id: {sid}")

        event = BrowserStateRequestEvent(include_dom=True, include_screenshot=True)
        scope.emit(event)
        await event

        # Aggregate results from DOMWatchdog + ScreenshotWatchdog.
        dom_state: DOMState | None = None
        screenshot: bytes | None = None
        url: str | None = None
        page_title: str | None = None

        for result_obj in event.event_results.values():
            if result_obj.is_success and isinstance(result_obj.result, BrowserStateSummary):
                summary: BrowserStateSummary = result_obj.result
                if summary.dom_state is not None:
                    dom_state = summary.dom_state
                if summary.screenshot is not None:
                    screenshot = summary.screenshot
                if summary.url is not None:
                    url = summary.url
                if summary.page_title is not None:
                    page_title = summary.page_title

        # Update per-tab selector_map.
        if dom_state is not None:
            self._selector_maps[sid] = dict(dom_state.selector_map)
        elif sid not in self._selector_maps:
            logger.warning(
                "observe(%s): DOM capture returned no state -- selector_map is empty",
                sid,
            )

        # URL fallback via CDP -- routed to the correct tab.
        if url is None and self._cdp is not None:
            try:
                nav_history = await self._cdp.send(
                    "Page.getNavigationHistory",
                    {},
                    session_id=sid,
                )
                if nav_history is not None:
                    entries = nav_history.get("entries", [])
                    idx = nav_history.get("currentIndex", 0)
                    if entries and 0 <= idx < len(entries):
                        url = entries[idx].get("url", "")
                        page_title = entries[idx].get("title", "")
            except Exception as exc:
                logger.debug(
                    "Failed to get navigation history for %s: %s", sid, exc
                )

        tabs = await self.get_tabs()

        return {
            "dom": dom_state,
            "screenshot": screenshot,
            "url": url,
            "page_title": page_title,
            "tabs": tabs,
            "viewport": None,
            "timestamp": time.time(),
        }

    async def act(self, action: Action, session_id: str | None = None) -> ActionResult:
        """Execute an action on the specified tab.

        Multi-agent safe: each session_id routes to its own scope.
        """
        sid = session_id or self._default_session_id
        if sid is None:
            return ActionResult(success=False, error="No active browser session")

        scope = self._scopes.get(sid)
        if scope is None:
            return ActionResult(success=False, error=f"Unknown session_id: {sid}")

        event_cls = _ACTION_EVENT_MAP.get(action.name)
        if event_cls is None:
            return ActionResult(success=False, error=f"Unknown action: {action.name}")

        try:
            selector_map = self._selector_maps.get(sid, {})
            event_params = self._translate_params(action, selector_map)
            event = event_cls(**event_params)
            scope.emit(event)
            await event

            if action.name == "done":
                return ActionResult(
                    success=True,
                    is_done=True,
                    extracted_content=action.params.get("extracted_content"),
                )

            return ActionResult(success=True)

        except ValueError as e:
            return ActionResult(success=False, error=str(e))
        except Exception as e:
            logger.error("Action %s failed: %s", action.name, e, exc_info=True)
            return ActionResult(success=False, error=str(e))

    def get_action_space(self) -> list[ActionDescriptor]:
        return list(_ACTION_DESCRIPTORS)

    def selector_map(self, session_id: str | None = None) -> DOMSelectorMap:
        """Per-tab selector map (updated on each observe())."""
        sid = session_id or self._default_session_id
        if sid is None:
            return {}
        return self._selector_maps.get(sid, {})

    # -------------------------------------------------------------------
    # Tab management
    # -------------------------------------------------------------------

    async def create_tab(self, url: str = "about:blank") -> str:
        """Create a new tab and return its CDP session_id for agent use."""
        if self._cdp is None:
            raise RuntimeError("Session not set up")
        result = await self._cdp.send("Target.createTarget", {"url": url})
        target_id = (result or {}).get("targetId", "")
        return await self._attach_tab(target_id, url)

    async def close_tab(self, session_id: str) -> None:
        """Close a tab by its CDP session_id."""
        if session_id == self._default_session_id:
            raise ValueError(
                "Cannot close the default session tab. "
                "Create a new tab and reassign before closing."
            )
        target_id = self._session_to_target.get(session_id)
        if target_id is None:
            return

        # Close the scope.
        await self._scope_group.close_scope(target_id)

        # Clean up mappings.
        self._scopes.pop(session_id, None)
        self._selector_maps.pop(session_id, None)
        self._watchdogs.pop(target_id, None)
        self._target_to_session.pop(target_id, None)
        self._session_to_target.pop(session_id, None)

        # Close the browser target.
        if self._cdp is not None:
            try:
                await self._cdp.send(
                    "Target.closeTarget", {"targetId": target_id}
                )
            except Exception as exc:
                logger.debug("Failed to close target %s: %s", target_id, exc)

    async def get_tabs(self) -> list[dict]:
        """Query CDP for all open tabs (browser-level, no session_id).

        Returns plain dicts — will use proper models in M13.
        """
        if self._cdp is None:
            return []
        try:
            result = await self._cdp.send("Target.getTargets", {})
            targets = (result or {}).get("targetInfos", [])
            tabs = []
            for t in targets:
                if t.get("type") == "page":
                    tid = t["targetId"]
                    sid = self._target_to_session.get(tid)
                    tabs.append({
                        "tab_id": tid,
                        "title": t.get("title", ""),
                        "url": t.get("url", ""),
                        "is_active": (sid == self._default_session_id),
                    })
            return tabs
        except Exception as exc:
            logger.debug("Failed to get tabs: %s", exc)
            return []

    # -------------------------------------------------------------------
    # Internal: tab attach
    # -------------------------------------------------------------------

    async def _attach_tab(self, target_id: str, url: str = "") -> str:
        """Flatten-attach to a target, create scope + watchdogs. Returns CDP session_id."""
        if self._cdp is None:
            raise RuntimeError("Session not set up")

        # 1. Flatten attach.
        result = await self._cdp.send(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        cdp_session_id = (result or {}).get("sessionId")
        if not cdp_session_id:
            raise RuntimeError(
                f"Target.attachToTarget returned no sessionId for target {target_id}"
            )

        # 2. EventScope per tab.
        scope = await self._scope_group.create_scope(
            target_id,
            target_id=target_id,
            url=url,
            cdp_session_id=cdp_session_id,
        )

        # 3. Per-tab watchdogs.
        watchdogs = self._create_watchdogs_for_scope(
            level="tab", cdp_session_id=cdp_session_id,
        )
        for wd in watchdogs:
            wd.attach(scope, session_id=cdp_session_id)

        # 4. Register mappings.
        self._scopes[cdp_session_id] = scope
        self._selector_maps[cdp_session_id] = {}
        self._target_to_session[target_id] = cdp_session_id
        self._session_to_target[cdp_session_id] = target_id
        self._watchdogs[target_id] = watchdogs

        return cdp_session_id

    # -------------------------------------------------------------------
    # Internal: watchdog factories
    # -------------------------------------------------------------------

    def _create_watchdogs_for_scope(
        self,
        level: str = "tab",
        cdp_session_id: str | None = None,
    ) -> list[Any]:
        """Instantiate watchdogs according to the profile and scope level."""
        from spectragent.core.browser.watchdogs.action_watchdog import ActionWatchdog
        from spectragent.core.browser.watchdogs.crash_watchdog import CrashWatchdog
        from spectragent.core.browser.watchdogs.dom_watchdog import DOMWatchdog
        from spectragent.core.browser.watchdogs.downloads_watchdog import DownloadsWatchdog
        from spectragent.core.browser.watchdogs.popups_watchdog import PopupsWatchdog
        from spectragent.core.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
        from spectragent.core.browser.watchdogs.security_watchdog import SecurityWatchdog

        target_set = _TAB_WATCHDOGS if level == "tab" else _BROWSER_WATCHDOGS

        if level == "tab" and not cdp_session_id:
            raise ValueError("Tab-level watchdogs require a cdp_session_id")

        cdp = self._cdp
        if cdp is None:
            raise RuntimeError("Session not set up")

        # Per-tab selector_map getter for ActionWatchdog.
        def _selector_map_getter() -> DOMSelectorMap:
            if cdp_session_id is not None:
                return self._selector_maps.get(cdp_session_id, {})
            return {}

        _WATCHDOG_FACTORY: dict[str, Any] = {
            "dom": lambda: DOMWatchdog(
                cdp,
                cleaner_type=self._profile.cleaner or "browser_use",
                serializer_type=self._profile.serializer or "text",
                viewport=self._viewport,
            ),
            "action": lambda: ActionWatchdog(
                cdp, selector_map_getter=_selector_map_getter,
            ),
            "screenshot": lambda: ScreenshotWatchdog(cdp),
            "crash": lambda: CrashWatchdog(cdp),
            "downloads": lambda: DownloadsWatchdog(cdp),
            "popups": lambda: PopupsWatchdog(cdp),
            "security": lambda: SecurityWatchdog(
                allowed_domains=self._profile.security_allowed_domains,
                blocked_domains=self._profile.security_blocked_domains,
            ),
        }

        watchdogs = []
        for wd_type in self._profile.watchdogs:
            if wd_type not in target_set:
                continue
            factory = _WATCHDOG_FACTORY.get(wd_type)
            if factory:
                watchdogs.append(factory())
            else:
                logger.warning("Unknown watchdog type: %s", wd_type)
        return watchdogs

    def _attach_browser_level_watchdogs(self, scope: EventScope) -> None:
        """Attach browser-level watchdogs (crash, downloads) to the global scope."""
        watchdogs = self._create_watchdogs_for_scope(level="browser")
        for wd in watchdogs:
            wd.attach(scope, session_id=None)
        self._watchdogs[_GLOBAL_SCOPE_ID] = watchdogs

    # -------------------------------------------------------------------
    # Internal: param translation
    # -------------------------------------------------------------------

    @staticmethod
    def _translate_params(action: Action, selector_map: DOMSelectorMap) -> dict[str, Any]:
        """Translate Action params to event kwargs using the per-tab selector_map.

        selector_map is keyed by ``backend_node_id``.  The LLM outputs
        ``[backend_node_id]`` markers, so the agent should send the same ID
        as ``"index"``.  We look it up directly in the map.
        """
        params = dict(action.params)

        if "index" in params and action.name in (
            "click", "type", "scroll", "select_dropdown", "extract_content",
        ):
            idx = params.pop("index")
            # selector_map is keyed by backend_node_id.
            # The "index" value from the agent IS the backend_node_id
            # (since the serializer now outputs [backend_node_id]).
            node = selector_map.get(idx)
            if node is not None and node.backend_node_id is not None:
                params["backend_node_id"] = node.backend_node_id
            else:
                raise ValueError(f"Element {idx} not found in selector_map")

        return params
