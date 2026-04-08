"""BrowserCapability — CDP-based browser automation as a Capability.

Direct rewrite of the legacy ``BrowserSession`` into a class that
satisfies ``CapabilityBase``. Owns the entire CDP infrastructure
(CDPClient, ScopeGroup, Watchdog system) and exposes browser actions
plus per-fragment observation resolution to the framework.

Key design points:

- **Watchdog = Resolver**: ``DOMWatchdog`` resolves the ``dom`` fragment,
  ``ScreenshotWatchdog`` resolves the ``screenshot`` fragment, etc.
- **Lazy-batch resolution**: ``resolve_fragment()`` consults a per-tab
  per-cycle cache. The first call within a cycle fires *one*
  ``BrowserStateRequestEvent`` with selective ``include_dom`` /
  ``include_screenshot`` flags reflecting all fragments needed; later
  calls in the same cycle hit the cache. The cache is invalidated by
  ``execute_action()`` so action effects are never observed via stale
  data.
- **Multi-tab routing**: ``execute_action`` and ``resolve_fragment`` both
  accept a ``session_id`` keyword. The cache is keyed by ``session_id``.

See docs/architecture_design.md §4.9 and docs/execution_dag.md M13.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

from agent_cdp import EventScope, ScopeGroup
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase, ActionRegistrar
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.browser.actions import (  # noqa: F401 — registration
    ClickAction,
    ClickCoordinateAction,
    DoneAction,
    ExtractContentAction,
    GoBackAction,
    GoForwardAction,
    NavigateAction,
    ScrollAction,
    SelectDropdownAction,
    SendKeysAction,
    SwitchTabAction,
    TypeAction,
)
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.dom.models import DOMSelectorMap, DOMState
from spectragent.core.capabilities.browser.profiles import (
    BrowserProfileConfig,
    resolve_profile,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
)
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation.fragments import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)

logger = logging.getLogger(__name__)


# Default browser action set, used when YAML does not pin the
# ``actions`` field. Keep in sync with ``__wiring__["action"]`` below —
# the wiring is the authoritative declaration of what ``BrowserCapability``
# can provide; this list is the materialised default instantiation.
_DEFAULT_BROWSER_ACTIONS: tuple[str, ...] = (
    "click",
    "click_coordinate",
    "type",
    "navigate",
    "scroll",
    "send_keys",
    "go_back",
    "go_forward",
    "switch_tab",
    "select_dropdown",
    "extract_content",
    "done",
)


# Watchdog level classification.
_TAB_WATCHDOGS = {"dom", "action", "screenshot", "popups", "security"}
_BROWSER_WATCHDOGS = {"crash", "downloads"}

# Scope ID for browser-level (non-tab-specific) watchdogs.
_GLOBAL_SCOPE_ID = "__browser__"


# ── Cache entry ───────────────────────────────────────────────────────


@dataclass
class _ObserveCycleCache:
    """Per-tab cache of resolved fragments within a single observe cycle.

    The cache is created on the first ``resolve_fragment`` call after an
    ``execute_action`` and invalidated on the next ``execute_action``.
    Multiple resolve calls within the same cycle reuse the same
    ``BrowserStateRequestEvent`` results so the CDP cost stays constant
    regardless of how many fragments the agent requests per step.
    """

    dom: DOMObs | None = None
    screenshot: ScreenshotObs | None = None
    browser_meta: BrowserMetaObs | None = None
    fired: bool = False  # whether the batched event has run for this cycle


# ══════════════════════════════════════════════════════════════════════
# BrowserCapability
# ══════════════════════════════════════════════════════════════════════


@dataclass
class BrowserCapability(CapabilityBase):
    """Browser automation capability — CDP + multi-tab + watchdog system."""

    __registry_key__: ClassVar[str] = "browser"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["dom", "screenshot", "browser_meta"]),
        # Action provides — symmetric to observation. Keep in sync with
        # ``_DEFAULT_BROWSER_ACTIONS`` above and with the concrete classes
        # registered under ``spectragent.core.capabilities.browser.actions``.
        "action": (
            "action",
            [
                "click",
                "click_coordinate",
                "type",
                "navigate",
                "scroll",
                "send_keys",
                "go_back",
                "go_forward",
                "switch_tab",
                "select_dropdown",
                "extract_content",
                "done",
            ],
        ),
        # ``browser_provider`` is the existing conscribe registrar for
        # the underlying browser backend (local_chrome, lexmount,
        # browseruse_lite, ...). Mode 1 (auto-discovery) allows any
        # registered provider.
        "backend": "browser_provider",
    }

    # ── conscribe-visible config ──────────────────────────────────────
    cdp_url: Annotated[
        str | None,
        PydanticField(
            description="CDP WebSocket URL. If None, the configured backend will provide one at setup time.",
        ),
    ] = None

    profile: Annotated[
        str,
        PydanticField(description="Browser profile name (resolved via resolve_profile)"),
    ] = "standard"

    viewport_width: Annotated[
        int, PydanticField(description="Viewport width in pixels")
    ] = 1280

    viewport_height: Annotated[
        int, PydanticField(description="Viewport height in pixels")
    ] = 720

    # ── per-action capability-side config ────────────────────────────
    # Each element is an ActionBase subclass instance. YAML may inject
    # a discriminated-union list keyed by ``action_id``; when omitted,
    # ``__post_init__`` materialises the default set from
    # ``_DEFAULT_BROWSER_ACTIONS``.
    actions: list[ActionBase] = field(default_factory=list)

    # ── internal state (excluded from config) ─────────────────────────
    _actions_by_name: dict[str, ActionBase] = field(
        default_factory=dict, init=False, repr=False
    )
    _cdp: CDPClient | None = field(default=None, init=False, repr=False)
    _scope_group: ScopeGroup | None = field(default=None, init=False, repr=False)
    _scopes: dict[str, EventScope] = field(default_factory=dict, init=False, repr=False)
    _selector_maps: dict[str, DOMSelectorMap] = field(
        default_factory=dict, init=False, repr=False
    )
    _target_to_session: dict[str, str] = field(
        default_factory=dict, init=False, repr=False
    )
    _session_to_target: dict[str, str] = field(
        default_factory=dict, init=False, repr=False
    )
    _watchdogs: dict[str, list[Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _global_scope: EventScope | None = field(default=None, init=False, repr=False)
    _default_session_id: str | None = field(default=None, init=False, repr=False)
    _alive: bool = field(default=False, init=False, repr=False)
    _resolved_profile: BrowserProfileConfig | None = field(
        default=None, init=False, repr=False
    )

    # Per-tab observe-cycle cache. Keyed by session_id.
    _cycle_cache: dict[str, _ObserveCycleCache] = field(
        default_factory=dict, init=False, repr=False
    )

    # Optional injected client for tests.
    _injected_client: CDPClient | None = field(
        default=None, init=False, repr=False
    )

    # ── Init machinery ────────────────────────────────────────────────

    def __post_init__(self) -> None:
        """Materialise the default action set when YAML omits ``actions``.

        Also builds the per-name lookup used by ``execute_action``. This
        intentionally uses a direct registry lookup (not ``discover``) so
        the method is safe to call inside hot-path ``setup``/runtime code.
        """
        if not self.actions:
            self.actions = [
                ActionRegistrar.get(name)() for name in _DEFAULT_BROWSER_ACTIONS
            ]
        self._actions_by_name = {a.action_id: a for a in self.actions}

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def default_session_id(self) -> str:
        if self._default_session_id is None:
            raise RuntimeError("BrowserCapability not set up — call setup() first")
        return self._default_session_id

    @property
    def cdp(self) -> CDPClient:
        if self._cdp is None:
            raise RuntimeError("BrowserCapability not set up — call setup() first")
        return self._cdp

    # ── Test injection (private) ──────────────────────────────────────

    def _inject_cdp_client(self, client: CDPClient) -> None:
        """Inject a pre-built (typically mocked) CDPClient before setup().

        Used by tests to avoid spinning up a real CDP WebSocket. Must be
        called before ``setup()``.
        """
        self._injected_client = client

    # ── CapabilityBase contract ──────────────────────────────────────

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        """Derive descriptors from each registered action instance.

        Kept context-free for browser — no per-task action shape. Other
        capabilities (e.g. ``ToolSandboxCapability``) pass per-task tool
        schemas through ``**context`` to each action's ``descriptor``.
        """
        return [a.descriptor(**context) for a in self.actions]

    async def setup(
        self, config: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        """Initialize CDP connection, global scope, and the initial tab.

        ``kwargs`` is forwarded from AgentRuntime / TaskLoader and may
        include task context (``start_url`` etc.) — currently the only
        supported key is ``cdp_url`` which overrides the constructor
        value (used by backends that allocate a fresh URL per session).
        """
        # Resolve profile.
        self._resolved_profile = resolve_profile(self.profile)
        self._scope_group = ScopeGroup("browser-capability")

        # Resolve CDP URL: kwarg > config > constructor field.
        cdp_url = kwargs.get("cdp_url") or (config or {}).get("cdp_url") or self.cdp_url

        # 1. CDPClient (WebSocket) — either injected (tests) or fresh.
        if self._injected_client is not None:
            self._cdp = self._injected_client
        else:
            if cdp_url is None:
                raise RuntimeError(
                    "BrowserCapability.setup() requires a CDP URL — set "
                    "``cdp_url`` in config or pass it as a kwarg from the backend."
                )
            self._cdp = CDPClient(cdp_url)
            await self._cdp.start()

        # 2. Global scope — browser-level watchdogs.
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
            r = await self._cdp.send("Target.createTarget", {"url": "about:blank"})
            target_id = (r or {}).get("targetId")
            if not target_id:
                raise RuntimeError("Target.createTarget returned no targetId")
            sid = await self._attach_tab(target_id, "about:blank")

        self._default_session_id = sid
        self._alive = True

        logger.info(
            "BrowserCapability setup complete. Default session: %s",
            self._default_session_id,
        )

    async def teardown(self) -> None:
        """Tear down all scopes and close CDPClient if we own it."""
        self._alive = False
        if self._scope_group is not None:
            await self._scope_group.close_all()
        self._scopes.clear()
        self._selector_maps.clear()
        self._watchdogs.clear()
        self._target_to_session.clear()
        self._session_to_target.clear()
        self._cycle_cache.clear()
        self._global_scope = None
        self._default_session_id = None

        if self._cdp is not None and self._injected_client is None:
            await self._cdp.stop()
        self._cdp = None
        self._scope_group = None

        logger.info("BrowserCapability teardown complete")

    async def execute_action(
        self, action: Action, **kwargs: Any
    ) -> ActionResult:
        """Delegate action execution to the registered action instance.

        There is no central dispatch table or ``if/else`` chain here —
        each action class owns its own parameter handling and event
        emission. We invalidate the per-tab observe cache before
        dispatching so a subsequent ``resolve_fragment`` call will see
        the post-action page state.
        """
        sid = kwargs.get("session_id") or self._default_session_id
        if sid is None:
            return ActionResult(success=False, error="No active browser session")

        if sid not in self._scopes:
            return ActionResult(success=False, error=f"Unknown session_id: {sid}")

        impl = self._actions_by_name.get(action.name)
        if impl is None:
            return ActionResult(
                success=False, error=f"Unknown action: {action.name}",
            )

        # Invalidate the cache for this tab BEFORE executing the action.
        # The next resolve call will trigger a fresh BrowserStateRequest.
        self._cycle_cache.pop(sid, None)

        try:
            return await impl.execute(
                action.params, capability=self, session_id=sid,
            )
        except Exception as exc:
            logger.error("Action %s failed: %s", action.name, exc, exc_info=True)
            return ActionResult(success=False, error=str(exc))

    async def resolve_fragment(
        self, fragment_key: str, **kwargs: Any
    ) -> Any:
        """Resolve one observation fragment via the lazy-batch cache.

        First call after an ``execute_action`` triggers a single
        ``BrowserStateRequestEvent`` (with the appropriate include_*
        flags). Subsequent calls within the same cycle hit the cache.

        Supported fragment keys: ``dom``, ``screenshot``, ``browser_meta``.
        """
        sid = kwargs.get("session_id") or self._default_session_id
        if sid is None:
            raise RuntimeError("No active browser session")

        # Ensure cache is populated for this cycle.
        await self._populate_cycle(sid, fragment_key)

        cache = self._cycle_cache[sid]
        if fragment_key == "dom":
            return cache.dom
        if fragment_key == "screenshot":
            return cache.screenshot
        if fragment_key == "browser_meta":
            return cache.browser_meta
        raise ValueError(
            f"BrowserCapability cannot resolve fragment '{fragment_key}'"
        )

    # ── Cycle cache machinery ─────────────────────────────────────────

    async def _populate_cycle(self, sid: str, fragment_key: str) -> None:
        """Ensure the per-tab cache contains the requested fragment.

        The first call within a cycle fires one ``BrowserStateRequestEvent``
        with ``include_dom`` / ``include_screenshot`` set according to
        what this capability actually provides. In practice agents that
        request multiple fragments per step make the single-round-trip
        batch cheaper than firing one event per resolve_fragment call.

        ``browser_meta`` does not require a state event — it's served by
        a direct CDP call against ``Page.getNavigationHistory`` and
        ``Target.getTargets``. We resolve it on demand.

        Partial-failure semantics: if the batched event produces no
        successful ``BrowserStateSummary`` we do NOT mark the cache as
        fired. A subsequent resolve call in the same cycle will retry.
        """
        cache = self._cycle_cache.get(sid)
        if cache is None:
            cache = _ObserveCycleCache()
            self._cycle_cache[sid] = cache

        # browser_meta has its own resolver path.
        if fragment_key == "browser_meta":
            if cache.browser_meta is None:
                cache.browser_meta = await self._resolve_browser_meta(sid)
            return

        # dom / screenshot share a single state event per cycle.
        if cache.fired:
            return  # Cache already populated for this cycle.

        scope = self._scopes.get(sid)
        if scope is None:
            raise RuntimeError(f"Unknown session_id: {sid}")

        # Selective flags: only ask for what this capability provides
        # on a per-fragment basis. The ``provided_fragments`` set is
        # derived from ``__wiring__`` and covers dom + screenshot +
        # browser_meta. The event schema supports include_dom /
        # include_screenshot so we can skip watchdog work when the
        # fragment isn't needed.
        provided = self.provided_fragments
        event = BrowserStateRequestEvent(
            include_dom="dom" in provided,
            include_screenshot="screenshot" in provided,
        )
        scope.emit(event)
        await event

        dom_state: DOMState | None = None
        screenshot: bytes | None = None
        url: str | None = None
        page_title: str | None = None
        had_success = False

        for result_obj in event.event_results.values():
            if result_obj.is_success and isinstance(
                result_obj.result, BrowserStateSummary
            ):
                had_success = True
                summary: BrowserStateSummary = result_obj.result
                if summary.dom_state is not None:
                    dom_state = summary.dom_state
                if summary.screenshot is not None:
                    screenshot = summary.screenshot
                if summary.url is not None:
                    url = summary.url
                if summary.page_title is not None:
                    page_title = summary.page_title

        # M4: If no watchdog returned a successful summary, leave the
        # cache unfired so the next resolve_fragment call retries
        # instead of serving empty fragments forever.
        if not had_success:
            logger.warning(
                "observe cycle (sid=%s): BrowserStateRequestEvent produced "
                "no successful BrowserStateSummary — leaving cache unfired",
                sid,
            )
            return

        # Update per-tab selector_map (consumed by execute_action's
        # index → backend_node_id translation).
        if dom_state is not None:
            self._selector_maps[sid] = dict(dom_state.selector_map)

        # Build typed fragments and stash them in the cache.
        cache.dom = DOMObs(
            element_tree=dom_state,
            selector_map=dict(self._selector_maps.get(sid, {})),
        )
        cache.screenshot = ScreenshotObs(
            image_bytes=screenshot,
            viewport_width=self.viewport_width,
            viewport_height=self.viewport_height,
        )
        # Opportunistically populate browser_meta from the same event,
        # so a subsequent resolve_fragment("browser_meta") doesn't need
        # to fire its own CDP request.
        if cache.browser_meta is None and (url is not None or page_title is not None):
            tabs = await self._get_tabs_safe()
            cache.browser_meta = BrowserMetaObs(
                url=url or "",
                page_title=page_title or "",
                tabs=tabs,
            )
        cache.fired = True

    async def _resolve_browser_meta(self, sid: str) -> BrowserMetaObs:
        """Resolve ``browser_meta`` independently of the state event.

        Used when an agent only requests ``browser_meta`` and never
        triggers the dom/screenshot batch.
        """
        url = ""
        page_title = ""
        if self._cdp is not None:
            try:
                nav_history = await self._cdp.send(
                    "Page.getNavigationHistory", {}, session_id=sid,
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
        tabs = await self._get_tabs_safe()
        return BrowserMetaObs(url=url, page_title=page_title, tabs=tabs)

    async def _get_tabs_safe(self) -> list[dict[str, Any]]:
        """Best-effort tab enumeration; returns empty on error."""
        if self._cdp is None:
            return []
        try:
            result = await self._cdp.send("Target.getTargets", {})
            targets = (result or {}).get("targetInfos", [])
            tabs: list[dict[str, Any]] = []
            for t in targets:
                if t.get("type") == "page":
                    tid = t["targetId"]
                    sid = self._target_to_session.get(tid)
                    tabs.append(
                        {
                            "tab_id": tid,
                            "title": t.get("title", ""),
                            "url": t.get("url", ""),
                            "is_active": (sid == self._default_session_id),
                        }
                    )
            return tabs
        except Exception as exc:
            logger.debug("Failed to get tabs: %s", exc)
            return []

    # ── Tab management ────────────────────────────────────────────────

    async def create_tab(self, url: str = "about:blank") -> str:
        """Create a new tab and return its CDP session_id."""
        if self._cdp is None:
            raise RuntimeError("BrowserCapability not set up")
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

        if self._scope_group is not None:
            await self._scope_group.close_scope(target_id)

        self._scopes.pop(session_id, None)
        self._selector_maps.pop(session_id, None)
        self._watchdogs.pop(target_id, None)
        self._target_to_session.pop(target_id, None)
        self._session_to_target.pop(session_id, None)
        self._cycle_cache.pop(session_id, None)

        if self._cdp is not None:
            try:
                await self._cdp.send(
                    "Target.closeTarget", {"targetId": target_id}
                )
            except Exception as exc:
                logger.debug("Failed to close target %s: %s", target_id, exc)

    async def _attach_tab(self, target_id: str, url: str = "") -> str:
        """Flatten-attach to a target, build per-tab scope + watchdogs."""
        if self._cdp is None or self._scope_group is None:
            raise RuntimeError("BrowserCapability not set up")

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

    # ── Watchdog factories ────────────────────────────────────────────

    def _create_watchdogs_for_scope(
        self,
        level: str = "tab",
        cdp_session_id: str | None = None,
    ) -> list[Any]:
        """Instantiate watchdogs according to the profile and scope level."""
        from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
            ActionWatchdog,
        )
        from spectragent.core.capabilities.browser.watchdogs.crash_watchdog import (
            CrashWatchdog,
        )
        from spectragent.core.capabilities.browser.watchdogs.dom_watchdog import (
            DOMWatchdog,
        )
        from spectragent.core.capabilities.browser.watchdogs.downloads_watchdog import (
            DownloadsWatchdog,
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

        target_set = _TAB_WATCHDOGS if level == "tab" else _BROWSER_WATCHDOGS

        if level == "tab" and not cdp_session_id:
            raise ValueError("Tab-level watchdogs require a cdp_session_id")

        cdp = self._cdp
        if cdp is None:
            raise RuntimeError("BrowserCapability not set up")

        profile = self._resolved_profile
        if profile is None:
            raise RuntimeError("Profile not resolved — call setup() first")

        viewport = (self.viewport_width, self.viewport_height)

        # Per-tab selector_map getter for ActionWatchdog.
        def _selector_map_getter() -> DOMSelectorMap:
            if cdp_session_id is not None:
                return self._selector_maps.get(cdp_session_id, {})
            return {}

        _WATCHDOG_FACTORY: dict[str, Any] = {
            "dom": lambda: DOMWatchdog(
                cdp,
                cleaner_type=profile.cleaner or "browser_use",
                serializer_type=profile.serializer or "text",
                viewport=viewport,
            ),
            "action": lambda: ActionWatchdog(
                cdp, selector_map_getter=_selector_map_getter,
            ),
            "screenshot": lambda: ScreenshotWatchdog(cdp),
            "crash": lambda: CrashWatchdog(cdp),
            "downloads": lambda: DownloadsWatchdog(cdp),
            "popups": lambda: PopupsWatchdog(cdp),
            "security": lambda: SecurityWatchdog(
                allowed_domains=profile.security_allowed_domains,
                blocked_domains=profile.security_blocked_domains,
            ),
        }

        watchdogs: list[Any] = []
        for wd_type in profile.watchdogs:
            if wd_type not in target_set:
                continue
            factory = _WATCHDOG_FACTORY.get(wd_type)
            if factory:
                watchdogs.append(factory())
            else:
                logger.warning("Unknown watchdog type: %s", wd_type)
        return watchdogs

    def _attach_browser_level_watchdogs(self, scope: EventScope) -> None:
        """Attach browser-level watchdogs (crash, downloads) to global scope."""
        watchdogs = self._create_watchdogs_for_scope(level="browser")
        for wd in watchdogs:
            wd.attach(scope, session_id=None)
        self._watchdogs[_GLOBAL_SCOPE_ID] = watchdogs

