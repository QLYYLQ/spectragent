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

from agent_cdp import CDPEventBridge, EventScope, ScopeGroup
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
    ScreenshotAction,
    ScrollAction,
    SelectDropdownAction,
    SendKeysAction,
    SwitchTabAction,
    TypeAction,
    WaitAction,
)
from spectragent.core.capabilities.browser.backends.base import BaseBrowserProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.dom.models import DOMSelectorMap, DOMState
from spectragent.core.capabilities.browser.profiles import (
    BrowserProfileConfig,
    resolve_profile,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
    DOMContentLoadedEvent,
    LifecycleEvent,
    PageLoadEvent,
)
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation.fragments import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.schemas.provider_session import ProviderSession

logger = logging.getLogger(__name__)


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
        "observation": (
            "observation",
            ["browser.dom", "browser.screenshot", "browser.browser_meta"],
        ),
        # Action provides — symmetric to observation. Keep in sync with
        # the concrete classes registered under
        # ``spectragent.core.capabilities.browser.actions``.
        "action": (
            "action",
            [
                "browser.click",
                "browser.click_coordinate",
                "browser.type",
                "browser.navigate",
                "browser.screenshot",
                "browser.scroll",
                "browser.send_keys",
                "browser.go_back",
                "browser.go_forward",
                "browser.switch_tab",
                "browser.select_dropdown",
                "browser.extract_content",
                "browser.wait",
                "browser.done",
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

    debug_highlight: Annotated[
        bool,
        PydanticField(
            description=(
                "If True, ActionWatchdog calls CDP Overlay.highlightNode + "
                "short sleep before each click/type/select so a human watching "
                "the headed Chrome can see which element the agent targeted. "
                "Off by default — adds ~300ms per interactive action."
            ),
        ),
    ] = False

    # ── per-action capability-side config ────────────────────────────
    # Wiring (``__wiring__["action"]``) is the sole source of truth for
    # *which* actions exist. ``action_overrides`` controls *how* each
    # action is instantiated — keyed by canonical action name, values are
    # dicts of constructor kwargs (e.g. ``{"scroll": {"max_amount": 5000}}``).
    # Omitted actions get default-constructed from the registry.
    action_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ── wired backend (IoC injection by ConfigComposer) ──────────────
    # ``backend`` is a wired receptor managed by conscribe's metaclass.
    # Conscribe replaces this annotation with a ``WiredField`` descriptor
    # at class creation; ``ConfigComposer`` then calls
    # ``setattr(cap, "backend", provider)`` to inject the chosen provider.
    # Same pattern as ``agent.llm`` / ``agent.memory`` and
    # ``env.capabilities``. Kept as a class-level annotation only (not a
    # dataclass ``field()``) so dataclass leaves it alone — runtime is
    # unaffected by the apparent "field after default" ordering.
    backend: BaseBrowserProvider  # type: ignore[misc]

    # ── internal state (excluded from config) ─────────────────────────
    _actions_by_name: dict[str, ActionBase] = field(
        default_factory=dict, init=False, repr=False
    )
    _cdp: CDPClient | None = field(default=None, init=False, repr=False)
    _session: ProviderSession | None = field(
        default=None, init=False, repr=False
    )
    _scope_group: ScopeGroup | None = field(default=None, init=False, repr=False)
    _scopes: dict[str, EventScope] = field(default_factory=dict, init=False, repr=False)
    _bridges: dict[str, CDPEventBridge] = field(
        default_factory=dict, init=False, repr=False
    )
    _main_frame_ids: dict[str, str] = field(
        default_factory=dict, init=False, repr=False
    )
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
        """Materialise actions from wiring + optional overrides.

        Wiring is the sole source of truth for *which* actions exist.
        ``action_overrides`` controls *how* individual actions are
        instantiated — unknown keys (not in wiring) raise immediately.
        """
        wiring_names: tuple[str, ...] = self.__wiring__["action"][1]

        # Reject overrides for actions not declared in the wiring.
        extras = set(self.action_overrides) - set(wiring_names)
        if extras:
            raise ValueError(
                f"{type(self).__name__} received action_overrides for actions "
                f"not in __wiring__: {sorted(extras)}. "
                f"Wiring declares: {sorted(wiring_names)}."
            )

        # Materialise every wiring-declared action, applying overrides.
        self._actions_by_name = {}
        for name in wiring_names:
            action_cls = ActionRegistrar.get(name)
            if action_cls is None:
                raise ValueError(
                    f"Wiring declares action '{name}' but no class is "
                    f"registered under ActionRegistrar. Did you forget to "
                    f"import the action module?"
                )
            kwargs = self.action_overrides.get(name, {})
            self._actions_by_name[name] = action_cls(**kwargs)

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

    # ── Backend access ────────────────────────────────────────────────

    def _resolved_backend(self) -> BaseBrowserProvider | None:
        """Return the wired backend instance, or ``None`` if not injected.

        ``self.backend`` is a conscribe ``WiredField`` descriptor until
        ``ConfigComposer`` setattr-injects the chosen provider. Reading
        an uninjected wired attribute raises ``AttributeError``; this
        helper turns that into ``None`` so :meth:`setup` can branch
        cleanly between "use the wired backend" and "no backend was
        configured".
        """
        try:
            backend = self.backend
        except AttributeError:
            return None
        # Defensive: if conscribe ever stops raising on uninjected reads
        # (or a duck-typed stand-in slips through), require the protocol
        # surface we actually use.
        if not callable(getattr(backend, "open", None)):
            return None
        return backend

    # ── CapabilityBase contract ──────────────────────────────────────

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        """Derive descriptors from each registered action instance.

        Kept context-free for browser — no per-task action shape. Other
        capabilities (e.g. ``ToolSandboxCapability``) pass per-task tool
        schemas through ``**context`` to each action's ``descriptor``.
        """
        return [a.descriptor(**context) for a in self._actions_by_name.values()]

    async def setup(
        self, config: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        """Initialize CDP connection, global scope, and the initial tab.

        ``kwargs`` is forwarded from AgentRuntime / TaskLoader and may
        include task context (``start_url`` etc.).

        CDP URL resolution priority:
          1. ``kwargs["cdp_url"]`` (per-call override)
          2. ``config["cdp_url"]`` (legacy explicit config dict)
          3. ``self.cdp_url`` (conscribe field)
          4. ``self.backend.open()`` → ``ProviderSession.cdp_url`` (IoC backend)

        The backend path activates only when no explicit cdp_url is given;
        ``self._session`` is retained so :meth:`teardown` can release it.
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
            # Last-resort: ask the wired backend to allocate a session.
            backend = self._resolved_backend()
            if cdp_url is None and backend is not None:
                self._session = await backend.open(
                    viewport={
                        "width": self.viewport_width,
                        "height": self.viewport_height,
                    },
                )
                cdp_url = self._session.cdp_url
                if cdp_url is None:
                    raise RuntimeError(
                        f"Backend {backend.provider_id!r} opened a "
                        f"session but did not return a cdp_url — provider "
                        f"is incompatible with BrowserCapability."
                    )
            if cdp_url is None:
                raise RuntimeError(
                    "BrowserCapability.setup() requires a CDP URL — set "
                    "``cdp_url`` in config, pass it as a kwarg, or wire "
                    "a ``browser_provider`` backend via __wiring__."
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
        """Tear down all scopes, close CDPClient, and release backend session."""
        self._alive = False

        # Close all CDP→scope bridges BEFORE closing scope_group, so late
        # CDP events don't try to dispatch into a half-torn-down scope.
        for sid, bridge in list(self._bridges.items()):
            try:
                bridge.close()
            except Exception as exc:
                logger.debug("Bridge close failed for %s: %s", sid, exc)
        self._bridges.clear()

        if self._scope_group is not None:
            await self._scope_group.close_all()
        self._scopes.clear()
        self._selector_maps.clear()
        self._watchdogs.clear()
        self._target_to_session.clear()
        self._session_to_target.clear()
        self._cycle_cache.clear()
        self._main_frame_ids.clear()
        self._global_scope = None
        self._default_session_id = None

        if self._cdp is not None and self._injected_client is None:
            await self._cdp.stop()
        self._cdp = None
        self._scope_group = None

        # Release the backend session if we opened one in setup().
        backend = self._resolved_backend()
        if self._session is not None and backend is not None:
            try:
                await backend.close(self._session)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Backend %s close() failed: %s",
                    backend.provider_id,
                    exc,
                )
        self._session = None

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
        if fragment_key == "browser.dom":
            return cache.dom
        if fragment_key == "browser.screenshot":
            return cache.screenshot
        if fragment_key == "browser.browser_meta":
            return cache.browser_meta
        raise ValueError(
            f"BrowserCapability cannot resolve fragment '{fragment_key}'"
        )

    def _action_watchdog_for(self, sid: str) -> Any | None:
        """Return the ActionWatchdog instance attached to ``sid``, or ``None``.

        Used by the observe cycle to ask the watchdog to hide its
        debug overlay before the DOM/screenshot scrape so the injected
        cursor + click marker don't leak into the agent's observation.
        """
        from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
            ActionWatchdog,
        )

        target_id = self._session_to_target.get(sid)
        if target_id is None:
            return None
        for wd in self._watchdogs.get(target_id, []):
            if isinstance(wd, ActionWatchdog):
                return wd
        return None

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
        if fragment_key == "browser.browser_meta":
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
            include_dom="browser.dom" in provided,
            include_screenshot="browser.screenshot" in provided,
        )

        # Hide the debug-highlight overlay (cursor / click marker /
        # label) before the watchdogs scrape DOM + screenshot, so it
        # doesn't pollute the agent's observation. Restored in the
        # finally below — the briefly-hidden frame is invisible to a
        # human watcher because the scrape is sub-100ms. No-op when
        # debug_highlight is off or the overlay was never installed.
        action_wd = self._action_watchdog_for(sid)
        if action_wd is not None:
            await action_wd.set_overlay_visible(False)
        try:
            scope.emit(event)
            await event
        finally:
            if action_wd is not None:
                await action_wd.set_overlay_visible(True)

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

        # Close the CDP→scope bridge BEFORE the scope so late CDP events
        # don't hit a half-torn-down scope.
        bridge = self._bridges.pop(session_id, None)
        if bridge is not None:
            try:
                bridge.close()
            except Exception as exc:
                logger.debug("Bridge close failed for %s: %s", session_id, exc)

        if self._scope_group is not None:
            await self._scope_group.close_scope(target_id)

        self._scopes.pop(session_id, None)
        self._selector_maps.pop(session_id, None)
        self._watchdogs.pop(target_id, None)
        self._target_to_session.pop(target_id, None)
        self._session_to_target.pop(session_id, None)
        self._cycle_cache.pop(session_id, None)
        self._main_frame_ids.pop(session_id, None)

        if self._cdp is not None:
            try:
                await self._cdp.send(
                    "Target.closeTarget", {"targetId": target_id}
                )
            except Exception as exc:
                logger.debug("Failed to close target %s: %s", target_id, exc)

    async def _attach_tab(self, target_id: str, url: str = "") -> str:
        """Flatten-attach to a target, build per-tab scope + watchdogs.

        Page lifecycle wiring (M13.5): once attached we send ``Page.enable``
        and ``Page.setLifecycleEventsEnabled`` so ``Page.loadEventFired`` /
        ``Page.domContentEventFired`` / ``Page.lifecycleEvent`` actually
        fire on this session, and we build a :class:`CDPEventBridge` that
        translates each into the matching agent-cdp ``BaseEvent``. This is
        what lets ``ActionWatchdog._on_navigate`` honour
        ``NavigateToUrlEvent.wait_until``. We also stash the main frame
        id so navigate handlers can filter sub-frame lifecycle noise.
        """
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

        # 2. Enable Page domain + lifecycle events for this session.
        # Without these calls, loadEventFired / lifecycleEvent never fire.
        # We do this before creating watchdogs so a subsequent
        # Page.navigate's lifecycle is observable.
        try:
            await self._cdp.send("Page.enable", {}, session_id=cdp_session_id)
            await self._cdp.send(
                "Page.setLifecycleEventsEnabled",
                {"enabled": True},
                session_id=cdp_session_id,
            )
        except Exception as exc:
            logger.warning(
                "Page.enable / setLifecycleEventsEnabled failed on %s: %s — "
                "lifecycle-aware navigation will degrade to fire-and-forget",
                cdp_session_id, exc,
            )

        # 3. Capture main frame id (best-effort; used by _on_navigate to
        # filter sub-frame lifecycle events).
        main_frame_id: str | None = None
        try:
            tree_result = await self._cdp.send(
                "Page.getFrameTree", {}, session_id=cdp_session_id,
            )
            if tree_result:
                main_frame_id = (
                    tree_result.get("frameTree", {})
                    .get("frame", {})
                    .get("id")
                )
        except Exception as exc:
            logger.debug(
                "Page.getFrameTree failed on %s: %s", cdp_session_id, exc,
            )
        if main_frame_id is not None:
            self._main_frame_ids[cdp_session_id] = main_frame_id

        # 4. EventScope per tab.
        scope = await self._scope_group.create_scope(
            target_id,
            target_id=target_id,
            url=url,
            cdp_session_id=cdp_session_id,
        )

        # 5. CDP → scope bridge for Page-domain lifecycle events.
        bridge = CDPEventBridge(self._cdp, scope, session_id=cdp_session_id)
        bridge.bridge(
            "Page.loadEventFired",
            lambda p: PageLoadEvent(
                loader_id=p.get("loaderId"),
                timestamp=p.get("timestamp"),
            ),
        )
        bridge.bridge(
            "Page.domContentEventFired",
            lambda p: DOMContentLoadedEvent(
                loader_id=p.get("loaderId"),
                timestamp=p.get("timestamp"),
            ),
        )
        bridge.bridge(
            "Page.lifecycleEvent",
            lambda p: LifecycleEvent(
                name=p.get("name", ""),
                frame_id=p.get("frameId"),
                loader_id=p.get("loaderId"),
                timestamp=p.get("timestamp"),
            ),
        )
        self._bridges[cdp_session_id] = bridge

        # 6. Per-tab watchdogs.
        watchdogs = self._create_watchdogs_for_scope(
            level="tab", cdp_session_id=cdp_session_id,
        )
        for wd in watchdogs:
            wd.attach(scope, session_id=cdp_session_id)

        # 7. Register mappings.
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

        # Per-tab main_frame_id getter — ``_on_navigate`` uses it to
        # filter sub-frame Page.lifecycleEvent so we only wait on the
        # main frame's load/networkidle.
        def _main_frame_id_getter() -> str | None:
            if cdp_session_id is not None:
                return self._main_frame_ids.get(cdp_session_id)
            return None

        _WATCHDOG_FACTORY: dict[str, Any] = {
            "dom": lambda: DOMWatchdog(
                cdp,
                cleaner_type=profile.cleaner or "browser_use",
                serializer_type=profile.serializer or "text",
                viewport=viewport,
            ),
            "action": lambda: ActionWatchdog(
                cdp,
                selector_map_getter=_selector_map_getter,
                main_frame_id_getter=_main_frame_id_getter,
                debug_highlight=self.debug_highlight,
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

