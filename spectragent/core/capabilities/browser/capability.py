"""BrowserCapability — CDP-based browser automation as a Capability.

Direct rewrite of the legacy ``BrowserSession`` into a class that
satisfies ``CapabilityBase``. Owns the entire CDP infrastructure
(CDPClient, ScopeGroup, Watchdog system) and exposes browser actions
plus per-fragment observation resolution to the framework.

Key design points:

- **Watchdog = Resolver, for real**: watchdogs come from ``agent_cdp``'s
  ``WatchdogRegistrar``. Each declares ``level`` (tab / browser) and
  ``provides`` (the fragment keys it resolves) and is constructed from a
  single :class:`WatchdogContext`. This module names no watchdog class
  and holds no per-fragment branch, so adding an observation fragment is
  one new watchdog class — ``capability.py`` does not change.
- **Negotiation-driven cost**: the ``BrowserStateRequestEvent`` carries
  the *negotiated* fragment set, not the capability's full provides set.
  A vision-only agent that asked for ``browser.screenshot`` and not
  ``browser.dom`` never triggers a DOM extraction.
- **Lazy-batch resolution**: ``resolve_fragment()`` consults a per-tab
  per-cycle cache. The first call within a cycle fires *one*
  ``BrowserStateRequestEvent``; later calls in the same cycle hit the
  cache. A cycle is one *observation*: a fragment asked for a second time
  opens the next one, and ``execute_action()`` drops the cycle — so
  neither an action's effects nor the page moving on by itself is ever
  observed via stale data.
- **Multi-tab focus is capability state**: the CDP session id of the
  focused tab lives in ``_default_session_id`` and is changed through
  :meth:`BrowserCapability.switch_tab`. It is *not* a parameter of
  ``execute_action`` / ``resolve_fragment``, and nothing above this
  package knows it exists — a CDP tab handle is a browser concept, and
  threading it through the framework's Action / Environment / Runtime /
  Loop signatures gave every other domain a mandatory argument it could
  neither produce nor use. The per-cycle cache is keyed by that same id.

See docs/architecture_design.md §4.9 and docs/execution_dag.md M13.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from agent_cdp import CDPEventBridge, EventScope, ScopeGroup
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase, ActionRegistrar
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.browser.actions import (  # noqa: F401 — registration
    ClickAction,
    ClickCoordinateAction,
    DoneAction,
    DragAction,
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
    TypeCoordinateAction,
    WaitAction,
)
from spectragent.core.capabilities.browser.backends.base import BaseBrowserProvider
from spectragent.core.capabilities.browser.cdp_client import CDPClient
from spectragent.core.capabilities.browser.dom.cleaning import DOMCleanerProtocol
from spectragent.core.capabilities.browser.dom.cleaning.browser_use import BrowserUseCleaner
from spectragent.core.capabilities.browser.dom.cleaning.skyvern import (  # noqa: F401 — registration
    SkyvernCleaner,
)
from spectragent.core.capabilities.browser.dom.models import DOMSelectorMap
from spectragent.core.capabilities.browser.input import (
    InstantTrajectory,
    MouseTrajectoryProtocol,
)
from spectragent.core.capabilities.browser.profiles import (
    BrowserProfileConfig,
    resolve_profile,
)

# Importing the package registers every watchdog under agent_cdp's
# ``WatchdogRegistrar``. This module then looks them up by key — it never
# names a watchdog class, which is what makes "one class per fragment"
# true rather than aspirational.
from spectragent.core.capabilities.browser.watchdogs import (
    ActionWatchdog,
    BrowserWatchdog,
    WatchdogContext,
    browser_watchdog_classes,
    fragments_provided_by,
    validate_watchdog_names,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    BrowserStateRequestEvent,
    BrowserStateSummary,
    DOMContentLoadedEvent,
    LifecycleEvent,
    PageLoadEvent,
)
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.provider_session import ProviderSession

logger = logging.getLogger(__name__)


# Scope ID for browser-level (non-tab-specific) watchdogs.
_GLOBAL_SCOPE_ID = "__browser__"

# How many times one observe cycle re-fires ``BrowserStateRequestEvent``
# before giving up. A DOM/screenshot scrape can lose a race against an
# in-flight navigation (the target is swapping documents and every CDP
# call in the batch fails); re-firing once costs one round trip and
# recovers that case. Exhausting the attempts raises — never serves an
# empty fragment.
_OBSERVE_MAX_ATTEMPTS = 2


# ── Cache entry ───────────────────────────────────────────────────────


@dataclass
class _ObserveCycleCache:
    """Per-tab cache of the fragments one observation resolves.

    ``fragments`` is keyed by fully-qualified fragment key. It used to be
    one hard-coded attribute per fragment, which forced both a schema
    edit and an ``if/elif`` branch in ``resolve_fragment`` for every new
    fragment; a dict needs neither.

    ``Environment.observe`` asks for every negotiated fragment exactly
    once, so the fragments of one observation share one
    ``BrowserStateRequestEvent`` and the CDP cost stays constant however
    many the agent requests. A fragment asked for *again* is therefore
    the next observation, and ``resolve_fragment`` opens a new cycle for
    it; ``execute_action`` drops the cycle as well.

    The cycle used to live until the next ``execute_action``. The page
    does not wait for one — a spinner resolves into a challenge, a modal
    fades in — so a Loop step that ran no action (a reply that failed to
    parse) was handed the previous step's screenshot byte for byte, 5.5
    seconds after the page had changed, and the agent clicked on what was
    no longer there.
    """

    fragments: dict[str, Any] = field(default_factory=dict)
    fired: bool = False  # whether the batched event has run for this cycle
    #: Fragment keys already handed out by this cycle.
    served: set[str] = field(default_factory=set)


def _build_wired_action(name: str) -> ActionBase:
    """Default-construct the action registered under ``name``."""
    action_cls = ActionRegistrar.get(name)
    if action_cls is None:
        raise ValueError(
            f"Wiring declares action '{name}' but no class is registered "
            f"under ActionRegistrar. Did you forget to import the action "
            f"module?"
        )
    return action_cls()


@dataclass
class _StateEventOutcome:
    """Aggregated replies to one ``BrowserStateRequestEvent`` emission.

    ``fragments`` merges every responding watchdog's contribution.
    ``handler_count`` / ``errors`` exist purely so the failure message can
    distinguish "no watchdog is listening" (a config problem) from "the
    watchdogs ran and every CDP call failed" (a live-page problem).
    """

    fragments: dict[str, Any] = field(default_factory=dict)
    handler_count: int = 0
    errors: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════
# BrowserCapability
# ══════════════════════════════════════════════════════════════════════


class _BrowserReceptors:
    """Annotation-only declaration of the ``actions`` receptor slot.

    ``ConfigComposer`` reads the *class* annotation to decide whether a
    wired slot is single-instance or list-shaped
    (:func:`~spectragent.core.composer._slot_annotation`), so the
    annotation has to exist somewhere in the MRO. It lives on this mixin
    rather than in the class body because ``BrowserCapability`` exposes
    ``actions`` as a property — the same split
    :class:`~spectragent.agents._registrar.AgentReceptors` uses.
    """

    actions: list[ActionBase]


class BrowserCapability(_BrowserReceptors, CapabilityBase):
    """Browser automation capability — CDP + multi-tab + watchdog system.

    Deliberately **not** a ``@dataclass``, unlike :class:`CapabilityBase`
    itself. conscribe's metaclass decides whether a ``__wiring__`` entry
    is already a constructor parameter by reading
    ``inspect.signature(cls.__init__)`` at *class creation* time — before
    ``@dataclass`` has synthesised anything. On a dataclass capability
    every wiring key therefore looks unreceived and conscribe installs a
    ``WiredField`` descriptor over whatever the class body bound, which
    both destroys the field default and makes the generated config field
    required. Hand-writing ``__init__`` in the class body is what lets
    ``actions`` be an *optional* receptor carrying a discriminated union
    of per-action configs — the same trick :class:`EnvironmentBase` uses
    for ``capabilities``.
    """

    __registry_key__: ClassVar[str] = "browser"
    __wiring__: ClassVar[dict[str, Any]] = {
        # Observation provides is *derived*, never listed. It is the union
        # of ``provides`` over every registered browser watchdog, so a new
        # fragment cannot drift out of sync with the class that resolves
        # it — and adding one does not edit this file. Evaluated at class
        # creation; ``watchdogs`` is imported above for exactly that.
        "observation": (
            "observation",
            sorted(fragments_provided_by(browser_watchdog_classes().values())),
        ),
        # Action provides — symmetric to observation. Keep in sync with
        # the concrete classes registered under
        # ``spectragent.core.capabilities.browser.actions``.
        #
        # The wiring key is the *attribute* the Composer fills
        # (``self.actions``); the registry it draws from is ``"action"``.
        # The listed keys are the full set this capability can offer; a
        # YAML ``actions:`` block narrows it (ablation), it never widens
        # it.
        "actions": (
            "action",
            [
                "browser.click",
                "browser.click_coordinate",
                "browser.type",
                "browser.type_coordinate",
                "browser.drag",
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
        # ``mouse_trajectory`` is a receptor, not a scalar: the chosen
        # strategy arrives as a *constructed instance* carrying its own
        # knobs (windmouse gravity/wind, bezier control points, ...), so
        # every one of them is reachable and validated from YAML. It used
        # to be a ``Literal`` plus an untyped ``mouse_trajectory_overrides``
        # dict, which conscribe dropped from the schema entirely — the
        # knobs existed and nothing could set them.
        #
        # It is also the only humanization switch. ``instant`` is the
        # legacy instant jump; any other strategy humanizes every mouse
        # action (curved path, per-point timing, eased wheel, cursor
        # approach before type/select) at a cost of roughly 200-2000 ms
        # per action — meant for anti-bot-sensitive evals, not throughput
        # benchmarks. A separate ``humanize_mouse: bool`` used to sit next
        # to this receptor and silently overrode it when false.
        "mouse_trajectory": (
            "mouse_trajectory",
            ["bezier", "instant", "learned", "windmouse"],
        ),
        # ``dom_cleaner`` is a receptor for the same reason: the strategy
        # arrives constructed, carrying the knobs that decide what the
        # agent gets to see (``descendants_occlude``,
        # ``max_occlusion_nodes``, ``include_shadow_dom``, ...). It used to
        # be a name on ``BrowserProfileConfig``, which meant ``DOMWatchdog``
        # looked the class up and default-constructed it — the generated
        # ``BrowserUseDom_CleanerConfig`` existed and nothing could reach it.
        "dom_cleaner": ("dom_cleaner", ["browser_use", "skyvern"]),
        # ``browser_provider`` is the existing conscribe registrar for
        # the underlying browser backend (local_chrome, lexmount,
        # browseruse_lite, ...). Mode 1 (auto-discovery) allows any
        # registered provider.
        "backend": "browser_provider",
    }

    # ── wired receptors ───────────────────────────────────────────────
    # ``actions`` is declared on :class:`_BrowserReceptors`; the other two
    # are annotation-only here, so conscribe installs a ``WiredField``
    # descriptor and the Composer setattr-injects them after construction.
    mouse_trajectory: MouseTrajectoryProtocol  # type: ignore[misc]
    dom_cleaner: DOMCleanerProtocol  # type: ignore[misc]
    backend: BaseBrowserProvider  # type: ignore[misc]

    # ── conscribe-visible config (assigned in __init__) ──────────────
    cdp_url: str | None
    profile: str
    viewport_width: int
    viewport_height: int
    debug_highlight: bool
    screenshot_format: Literal["png", "jpeg"]
    screenshot_quality: int

    # ── internal state, never config (assigned in __init__) ──────────
    # Declared here as type-only annotations so both pyright and the
    # conscribe stub writer see them; the values are set in ``__init__``.
    _actions_by_name: dict[str, ActionBase]
    _cdp: CDPClient | None
    _session: ProviderSession | None
    _scope_group: ScopeGroup | None
    _scopes: dict[str, EventScope]
    _bridges: dict[str, CDPEventBridge]
    _main_frame_ids: dict[str, str]
    _selector_maps: dict[str, DOMSelectorMap]
    _target_to_session: dict[str, str]
    _session_to_target: dict[str, str]
    _watchdogs: dict[str, list[BrowserWatchdog]]
    _global_scope: EventScope | None
    _default_session_id: str | None
    _alive: bool
    _resolved_profile: BrowserProfileConfig | None
    #: Per-tab observe-cycle cache. Keyed by session_id.
    _cycle_cache: dict[str, _ObserveCycleCache]
    #: Optional injected client for tests.
    _injected_client: CDPClient | None

    # ── Init machinery ────────────────────────────────────────────────

    def __init__(
        self,
        id: Annotated[
            str | None,
            PydanticField(
                description=(
                    "Instance identity. Unique across the whole config; "
                    "defaults to '<capability_id>#<ordinal>' when omitted."
                ),
            ),
        ] = None,
        cdp_url: Annotated[
            str | None,
            PydanticField(
                description=(
                    "CDP WebSocket URL. If None, the configured backend will "
                    "provide one at setup time."
                ),
            ),
        ] = None,
        profile: Annotated[
            str,
            PydanticField(description="Browser profile name (resolved via resolve_profile)"),
        ] = "standard",
        viewport_width: Annotated[
            int, PydanticField(description="Viewport width in pixels")
        ] = 1280,
        viewport_height: Annotated[
            int, PydanticField(description="Viewport height in pixels")
        ] = 720,
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
        ] = False,
        screenshot_format: Annotated[
            Literal["png", "jpeg"],
            PydanticField(
                description=(
                    "Encoding of the browser.screenshot observation fragment. "
                    "'png' (default) is lossless and unchanged. 'jpeg' is the "
                    "lever on a vision run's step time: a 1280x720 article is "
                    "240KB of PNG and 107KB of JPEG q70, and Chrome encodes it "
                    "in half the time, before the upload the model waits on. "
                    "The browser.screenshot *action* has its own format — this "
                    "decides only what every observation ships."
                ),
            ),
        ] = "png",
        screenshot_quality: Annotated[
            int,
            PydanticField(
                ge=1,
                le=100,
                description=(
                    "JPEG quality for the browser.screenshot observation "
                    "fragment; ignored when screenshot_format is 'png'. 70-80 "
                    "is where a page screenshot stops shrinking much and text "
                    "is still crisp."
                ),
            ),
        ] = 80,
        actions: Annotated[
            list[ActionBase] | None,
            PydanticField(
                description=(
                    "Actions this capability instantiates, each with its own "
                    "side-effect configuration (scroll distance, navigation "
                    "wait mode, post-action settle delay, ...). Omit to get "
                    "every action the wiring declares. Listing a subset is an "
                    "ablation: the actions you leave out are not built, are "
                    "not advertised to the model, and will fail action "
                    "negotiation if an agent requires one of them."
                ),
            ),
        ] = None,
    ) -> None:
        super().__init__(id=id)

        self.cdp_url = cdp_url
        self.profile = profile
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.debug_highlight = debug_highlight
        self.screenshot_format = screenshot_format
        self.screenshot_quality = screenshot_quality

        # In-Python default for the wired trajectory receptor. YAML must
        # name one (a single-instance receptor is required in the
        # generated schema, exactly like ``backend``); constructing the
        # capability directly gets the no-op strategy, which is the
        # legacy instant-jump behaviour. ``ConfigComposer`` setattrs the
        # configured instance over this, shadowing the descriptor.
        self.mouse_trajectory = InstantTrajectory()
        # Same contract for the DOM cleaning strategy: YAML names one, a
        # hand-built capability gets the default strategy with its defaults.
        self.dom_cleaner = BrowserUseCleaner()

        # ── internal state (never config) ─────────────────────────
        self._actions_by_name = {}
        self._cdp = None
        self._session = None
        self._scope_group = None
        self._scopes = {}
        self._bridges = {}
        self._main_frame_ids = {}
        self._selector_maps = {}
        self._target_to_session = {}
        self._session_to_target = {}
        self._watchdogs = {}
        self._global_scope = None
        self._default_session_id = None
        self._alive = False
        self._cycle_cache = {}
        self._injected_client = None

        self.actions = actions

        # Resolve the profile eagerly: which watchdogs are installed
        # decides which fragments this capability can resolve, and
        # negotiation asks that question *before* setup() runs.
        self._resolved_profile = resolve_profile(profile)

    # ── Action set ────────────────────────────────────────────────────

    @classmethod
    def _wired_action_names(cls) -> tuple[str, ...]:
        """Fully-qualified action keys ``__wiring__`` offers."""
        return tuple(cls.__wiring__["actions"][1])

    @property
    def actions(self) -> list[ActionBase]:
        """The action instances this capability will actually execute."""
        return list(self._actions_by_name.values())

    @actions.setter
    def actions(self, value: list[ActionBase] | None) -> None:
        """Adopt an explicit action list, or derive the full wired set.

        ``None`` / ``[]`` means "everything the wiring offers" — that is
        both the no-``actions:``-block YAML case and the Composer's
        deferred-receptor pass, which setattrs an empty list when no
        top-level ``action:`` section exists. A non-empty list is taken
        literally: actions the config did not name are not instantiated,
        which is the design doc's ablation narrowing.
        """
        offered = self._wired_action_names()
        if not value:
            resolved = [_build_wired_action(name) for name in offered]
        else:
            unknown = sorted({a.action_id for a in value} - set(offered))
            if unknown:
                raise ValueError(
                    f"{type(self).__name__} was configured with actions it does "
                    f"not provide: {unknown}. __wiring__ offers: "
                    f"{sorted(offered)}."
                )
            resolved = list(value)
        self._actions_by_name = {a.action_id: a for a in resolved}

    # ── Properties ────────────────────────────────────────────────────

    @property
    def profile_config(self) -> BrowserProfileConfig:
        """The resolved profile. Available from construction onwards."""
        if self._resolved_profile is None:  # pragma: no cover — defensive
            self._resolved_profile = resolve_profile(self.profile)
        return self._resolved_profile

    def _watchdog_classes(self) -> dict[str, type[BrowserWatchdog]]:
        """Watchdog classes this profile installs, keyed by registry key.

        Every registered watchdog minus the profile's exclusions. An
        unregistered exclusion name is a hard error naming the watchdog —
        it used to be a ``logger.warning`` that let the run continue and
        resurface much later as a pydantic ``ValidationError`` about a
        *fragment*, pointing at entirely the wrong layer.
        """
        excluded = validate_watchdog_names(self.profile_config.watchdogs_exclude)
        return browser_watchdog_classes(exclude=excluded)

    @property
    def declared_fragments(self) -> frozenset[str]:
        """Fragments the installed watchdogs can resolve.

        Derived from the watchdogs the profile leaves enabled, not from
        ``__wiring__``: the ``vision_only`` profile genuinely cannot
        serve ``browser.dom``, so an agent that requires it must fail
        negotiation instead of receiving an empty tree every step.
        """
        return fragments_provided_by(self._watchdog_classes().values())

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

    def _session_cdp_headers(self) -> dict[str, str] | None:
        """Auth headers the backend requires on the CDP WebSocket handshake.

        Providers that gate their CDP endpoint (Skyvern's ``x-api-key``,
        Airtop's ``Authorization: Bearer``) return them in
        ``ProviderSession.metadata["cdp_headers"]``. Without forwarding
        them to :class:`CDPClient` the handshake is rejected for missing
        auth and the run dies before the first observe.

        Returns ``None`` (rather than ``{}``) when there is nothing to
        send, so ``websockets.connect`` sees its own default.

        NOTE: the values are credentials — never log this dict.
        """
        if self._session is None:
            return None
        raw = self._session.metadata.get("cdp_headers")
        if not isinstance(raw, dict) or not raw:
            return None
        return {str(k): str(v) for k, v in raw.items()}

    async def _apply_viewport_override(self, cdp_session_id: str) -> None:
        """Make ``viewport_width`` / ``viewport_height`` real for this tab.

        Without this the configured viewport was pure fiction: it was
        forwarded to the DOM cleaner (bounds / visibility filtering) and
        reported on ``ScreenshotObs``, but nothing ever told the browser
        about it — the true CSS viewport was whatever the provider or the
        local window chrome happened to give us, so "in viewport" filtering
        was computed against the wrong rectangle.

        ``deviceScaleFactor: 0`` keeps the platform's real DPR, which the
        DOM watchdog queries live per observe — we must not pin it here.
        Best-effort: a provider that rejects Emulation should degrade to the
        previous behaviour, not kill the run.
        """
        if self._cdp is None:
            return
        try:
            await self._cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": self.viewport_width,
                    "height": self.viewport_height,
                    "deviceScaleFactor": 0,
                    "mobile": False,
                },
                session_id=cdp_session_id,
            )
        except Exception as exc:
            logger.warning(
                "Emulation.setDeviceMetricsOverride(%dx%d) failed on %s: %s — "
                "the real viewport may differ from the configured one, so "
                "viewport-relative DOM filtering will be approximate",
                self.viewport_width,
                self.viewport_height,
                cdp_session_id,
                exc,
            )

    # ── CapabilityBase contract ──────────────────────────────────────

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        """Derive descriptors from each registered action instance.

        ``**context`` is forwarded verbatim. For browser it carries this
        run's active observation fragments (``AgentRuntime.prepare`` passes
        ``fragments=``), which is what lets ``browser.scroll`` advertise its
        ``index`` param in a DOM run and not in a vision one — the param
        addresses the selector map, so a run without ``browser.dom`` must
        not be told it exists. Other capabilities use the same channel for
        their own per-task shapes (``ToolSandboxCapability`` passes tool
        schemas).
        """
        return [a.descriptor(**context) for a in self._actions_by_name.values()]

    async def setup(
        self,
        *,
        negotiation: Any = None,
        cdp_url: str | None = None,
        start_url: str | None = None,
        login_required: bool | None = None,
        login_type: str | None = None,
        credentials_ref: str | None = None,
        risk_control: bool | None = None,
        risk_control_types: list[str] | None = None,
    ) -> None:
        """Initialize CDP connection, global scope, and the initial tab.

        Every keyword here is a declared part of this capability's setup
        vocabulary — that signature is what ``EnvironmentBase.setup``
        reads to decide which task-context keys it may route here, and a
        key nobody declares is now an error rather than a value all
        capabilities quietly discard.

        ``negotiation`` is the observation negotiation result. It narrows
        :attr:`provided_fragments`, which is what every
        ``BrowserStateRequestEvent`` this session fires is built from —
        so an agent that negotiated only ``browser.screenshot`` never
        triggers DOM extraction. Passing ``None`` keeps the full set.

        CDP URL resolution priority:
          1. ``cdp_url`` (per-call override)
          2. ``self.cdp_url`` (conscribe field)
          3. ``self.backend.open()`` → ``ProviderSession.cdp_url`` (IoC backend)

        The backend path activates only when no explicit cdp_url is given;
        ``self._session`` is retained so :meth:`teardown` can release it.

        ``start_url`` opens the task's landing page before the agent's
        first observe. The remaining keys are the rest of
        :class:`BrowserSetup`'s vocabulary; they are accepted so a task
        carrying them routes cleanly, and reported at WARNING because
        this capability does not act on them yet.
        """
        self.apply_negotiation(negotiation)

        # Re-resolve in case ``profile`` was mutated after construction,
        # and fail loudly here on an unknown watchdog name rather than
        # mid-run.
        self._resolved_profile = resolve_profile(self.profile)
        installed = self._watchdog_classes()

        # A fragment the run negotiated but no installed watchdog
        # resolves would surface as a missing-field ValidationError on
        # the negotiated observation model, several layers away from the
        # profile that caused it. Say so now.
        if self._negotiated_fragments is not None:
            resolvable = fragments_provided_by(installed.values())
            # Only fragments this capability owns at all — a composite
            # environment negotiates fragments other capabilities serve.
            ours = fragments_provided_by(browser_watchdog_classes().values())
            missing = sorted((self._negotiated_fragments & ours) - resolvable)
            if missing:
                raise RuntimeError(
                    f"Browser profile {self.profile!r} installs watchdogs "
                    f"{sorted(installed)} which cannot resolve negotiated "
                    f"fragment(s) {missing}. Either drop the fragment from "
                    f"the agent's __wiring__ or stop excluding the watchdog "
                    f"that provides it "
                    f"(profile excludes: "
                    f"{sorted(self.profile_config.watchdogs_exclude)})."
                )

        unhandled = sorted(
            name
            for name, value in (
                ("login_required", login_required),
                ("login_type", login_type),
                ("credentials_ref", credentials_ref),
                ("risk_control", risk_control),
                ("risk_control_types", risk_control_types),
            )
            if value
        )
        if unhandled:
            logger.warning(
                "BrowserCapability: task context declares %s, which this "
                "capability accepts but does not act on yet — the run will "
                "proceed without it.",
                unhandled,
            )

        self._scope_group = ScopeGroup("browser-capability")

        # Resolve CDP URL: per-call override > constructor field.
        cdp_url = cdp_url or self.cdp_url

        # 1. CDPClient (WebSocket) — either injected (tests) or fresh.
        if self._injected_client is not None:
            self._cdp = self._injected_client
        else:
            # Last-resort: ask the wired backend to allocate a session.
            backend = self._resolved_backend()
            if cdp_url is None and backend is not None:
                # ``open()`` is called with no framework-level kwargs. Every
                # cloud provider declares an explicit keyword-only signature
                # of *its own* options (skyvern: timeout_minutes/adblock/...,
                # airtop: profile_name/..., lexmount: browser_mode/...), so
                # passing a generic ``viewport=`` here was a TypeError for all
                # of them and a silently-ignored kwarg for LocalChrome. The
                # viewport is a browser-level concern and is applied per tab
                # via ``Emulation.setDeviceMetricsOverride`` in _attach_tab.
                self._session = await backend.open()
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
            self._cdp = CDPClient(
                cdp_url, additional_headers=self._session_cdp_headers()
            )
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

        # The task's landing page. Routed here by name from
        # ``TaskLoader.get_task_context()``; before F8 it arrived in a
        # ``**kwargs`` sink and was dropped, so every browser benchmark
        # started the agent on about:blank.
        if start_url:
            result = await self.execute_action(
                Action(name="browser.navigate", params={"url": start_url})
            )
            if not result.success:
                raise RuntimeError(
                    f"BrowserCapability.setup(): could not open start_url "
                    f"{start_url!r} — {result.error}"
                )

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

        # Watchdogs own bridges too (PopupsWatchdog, CrashWatchdog). Those
        # live outside ``self._bridges``, so closing only the page-lifecycle
        # bridges above left their callbacks bound to a scope that is about
        # to close.
        for wd_list in list(self._watchdogs.values()):
            self._detach_watchdogs(wd_list)

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

    @staticmethod
    def _detach_watchdogs(watchdogs: list[BrowserWatchdog]) -> None:
        """Release CDP callbacks held by watchdogs that opened their own bridge.

        ``detach()`` is part of :class:`BrowserWatchdog` (a no-op unless
        the watchdog opened a ``CDPEventBridge``), so this calls it
        unconditionally. It used to be probed for with ``getattr``
        because agent-cdp's ``WatchdogProtocol`` only requires ``attach``.
        """
        for wd in watchdogs:
            try:
                wd.detach()
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug(
                    "Watchdog %s detach failed: %s", type(wd).__name__, exc
                )

    async def execute_action(self, action: Action) -> ActionResult:
        """Delegate action execution to the registered action instance.

        There is no central dispatch table or ``if/else`` chain here —
        each action class owns its own parameter handling and event
        emission. We invalidate the per-tab observe cache before
        dispatching so a subsequent ``resolve_fragment`` call will see
        the post-action page state.

        The tab acted on is the focused one (:attr:`default_session_id`),
        changed via :meth:`switch_tab`.
        """
        sid = self._default_session_id
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
            return await impl.execute(action.params, capability=self)
        except Exception as exc:
            logger.error("Action %s failed: %s", action.name, exc, exc_info=True)
            return ActionResult(success=False, error=str(exc))

    async def resolve_fragment(self, fragment_key: str) -> Any:
        """Resolve one observation fragment via the lazy-batch cache.

        The first call of a cycle fires a single
        ``BrowserStateRequestEvent`` carrying the negotiated fragment set;
        the other fragments of the same observation hit the cache. Asking
        for a fragment this cycle already handed out starts the next
        observation with a fresh capture — see :class:`_ObserveCycleCache`.

        Which keys are supported is decided by the installed watchdogs'
        ``provides`` — there is no branch per fragment here. The tab
        observed is the focused one (:attr:`default_session_id`).
        """
        sid = self._default_session_id
        if sid is None:
            raise RuntimeError("No active browser session")

        provided = self.provided_fragments
        if fragment_key not in provided:
            raise ValueError(
                f"BrowserCapability cannot resolve fragment "
                f"{fragment_key!r}. Active fragments for this run: "
                f"{sorted(provided)}."
            )

        cache = self._cycle_cache.get(sid)
        if cache is not None and fragment_key in cache.served:
            # Already handed out by this cycle: this read is the next
            # observation, and must see the page as it is now.
            del self._cycle_cache[sid]

        # Ensure cache is populated for this cycle.
        await self._populate_cycle(sid)

        cache = self._cycle_cache[sid]
        cache.served.add(fragment_key)
        return cache.fragments.get(fragment_key)

    def _action_watchdog_for(self, sid: str) -> Any | None:
        """Return the ActionWatchdog instance attached to ``sid``, or ``None``.

        Used by the observe cycle to ask the watchdog to hide its
        debug overlay before the DOM/screenshot scrape so the injected
        cursor + click marker don't leak into the agent's observation.
        """
        target_id = self._session_to_target.get(sid)
        if target_id is None:
            return None
        for wd in self._watchdogs.get(target_id, []):
            if isinstance(wd, ActionWatchdog):
                return wd
        return None

    # ── Cycle cache machinery ─────────────────────────────────────────

    async def _populate_cycle(self, sid: str) -> None:
        """Fire one batched state event per tab per cycle and cache the result.

        The event carries :attr:`provided_fragments` — the *negotiated*
        set, not the capability's full provides set. Every fragment the
        run needs is therefore collected in a single round trip, and
        nothing else is collected at all.

        Partial-failure semantics: the event is re-fired up to
        ``_OBSERVE_MAX_ATTEMPTS`` times while any requested fragment is
        still missing, then raises naming the missing fragments.
        Returning quietly (the pre-fix behaviour) left the cache entry
        unset, and since env fragments are required fields on the
        negotiated observation model the caller got an opaque pydantic
        ``ValidationError`` about a missing field instead of the cause.
        """
        cache = self._cycle_cache.get(sid)
        if cache is None:
            cache = _ObserveCycleCache()
            self._cycle_cache[sid] = cache

        if cache.fired:
            return  # Cache already populated for this cycle.

        scope = self._scopes.get(sid)
        if scope is None:
            raise RuntimeError(f"Unknown session_id: {sid}")

        wanted = self.provided_fragments
        outcome: _StateEventOutcome | None = None
        for attempt in range(1, _OBSERVE_MAX_ATTEMPTS + 1):
            outcome = await self._fire_state_event(sid, scope, wanted)
            if wanted <= outcome.fragments.keys():
                break
            logger.warning(
                "observe cycle (sid=%s): BrowserStateRequestEvent attempt "
                "%d/%d did not produce %s (handlers responded: %d%s)",
                sid,
                attempt,
                _OBSERVE_MAX_ATTEMPTS,
                sorted(wanted - outcome.fragments.keys()),
                outcome.handler_count,
                f"; errors: {outcome.errors}" if outcome.errors else "",
            )
        else:
            assert outcome is not None  # loop body runs at least once
            detail = (
                "; ".join(outcome.errors)
                if outcome.errors
                else "none recorded — see the watchdog errors logged above"
            )
            raise RuntimeError(
                f"observe cycle (sid={sid}): no watchdog produced "
                f"{sorted(wanted - outcome.fragments.keys())} after "
                f"{_OBSERVE_MAX_ATTEMPTS} attempts. Handlers responded: "
                f"{outcome.handler_count}. Errors: {detail}."
            )

        cache.fragments.update(outcome.fragments)
        cache.fired = True

    async def _fire_state_event(
        self, sid: str, scope: EventScope, wanted: frozenset[str]
    ) -> _StateEventOutcome:
        """Emit one ``BrowserStateRequestEvent`` and aggregate the replies.

        A fresh event instance per call — an awaited event carries its
        handlers' results, so re-firing the same object would re-read the
        previous attempt's answers.

        ``wanted`` is the negotiated fragment set. Each watchdog returns
        early unless its own ``provides`` intersects it, so the CDP cost
        of a step is exactly the fragments the agent asked for. This used
        to be computed from the capability's full provides set, which
        meant a vision-only agent paid for a full DOM extraction every
        step and negotiation saved nothing.
        """
        event = BrowserStateRequestEvent(fragments=wanted)

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

        # Merge every responding watchdog's fragments. The capability has
        # no idea what any of them mean — that is the point.
        outcome = _StateEventOutcome()
        for result_obj in event.event_results.values():
            outcome.handler_count += 1
            error = getattr(result_obj, "error", None)
            if error is not None:
                outcome.errors.append(f"{result_obj.handler_name}: {error}")
            if result_obj.is_success and isinstance(
                result_obj.result, BrowserStateSummary
            ):
                summary: BrowserStateSummary = result_obj.result
                outcome.fragments.update(summary.fragments)
        return outcome

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

    async def _page_target_ids(self) -> list[str]:
        """Every open page target id, in ``Target.getTargets`` order."""
        if self._cdp is None:
            raise RuntimeError("BrowserCapability not set up")
        result = await self._cdp.send("Target.getTargets", {})
        return [
            t["targetId"]
            for t in (result or {}).get("targetInfos", [])
            if t.get("type") == "page" and t.get("targetId")
        ]

    async def switch_tab(self, target_id: str | None = None) -> str:
        """Move agent focus to another tab; returns its CDP session id.

        ``target_id`` is a CDP ``targetId`` — the same string the agent
        already sees as ``tab_id`` in the ``browser_meta`` fragment.
        ``None`` means "the most recently opened tab other than the
        current one", matching the action's advertised semantics.

        Why this is a capability method and not a watchdog handler: agent
        focus is capability-level state (``_default_session_id``), and a
        watchdog is bound to exactly one tab's ``EventScope``. A handler
        living inside tab A can attach to tab B but has no way to repoint
        the capability at it — which is why the previous event-based
        implementation could not have worked even with a handler attached.

        Raises ``ValueError`` for an unknown or non-existent tab; the
        caller turns that into a failed ``ActionResult`` so the agent can
        pick a different tab instead of believing a no-op succeeded.
        """
        if self._cdp is None:
            raise RuntimeError("BrowserCapability not set up")

        current_target = (
            self._session_to_target.get(self._default_session_id)
            if self._default_session_id is not None
            else None
        )
        page_targets = await self._page_target_ids()

        if target_id is None:
            candidates = [t for t in page_targets if t != current_target]
            if not candidates:
                raise ValueError(
                    "switch_tab: no other tab is open — only the current "
                    "tab exists."
                )
            target_id = candidates[-1]
        elif target_id not in page_targets:
            raise ValueError(
                f"switch_tab: unknown tab {target_id!r}. Open tabs: "
                f"{page_targets}"
            )

        # Attach lazily — a tab opened by the page itself (target=_blank)
        # has no scope/watchdogs until we adopt it here.
        sid = self._target_to_session.get(target_id)
        if sid is None:
            sid = await self._attach_tab(target_id)

        # Bring it to the front so the page runs foreground timers,
        # renders, and screenshots the way a user-visible tab would.
        await self._cdp.send("Target.activateTarget", {"targetId": target_id})

        self._default_session_id = sid
        # The new tab may carry a cache from a previous cycle; the agent is
        # about to observe it fresh.
        self._cycle_cache.pop(sid, None)
        logger.info("Agent focus switched to tab %s (session %s)", target_id, sid)
        return sid

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

        # Same for the bridges the tab's own watchdogs opened.
        self._detach_watchdogs(self._watchdogs.get(target_id, []))

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

        # 3. Apply the configured viewport to this tab.
        await self._apply_viewport_override(cdp_session_id)

        # 4. Capture main frame id (best-effort; used by _on_navigate to
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

        # 5. EventScope per tab.
        scope = await self._scope_group.create_scope(
            target_id,
            target_id=target_id,
            url=url,
            cdp_session_id=cdp_session_id,
        )

        # 6. CDP → scope bridge for Page-domain lifecycle events.
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

        # 7. Per-tab watchdogs.
        watchdogs = self._create_watchdogs_for_scope(
            level="tab", cdp_session_id=cdp_session_id,
        )
        for wd in watchdogs:
            wd.attach(scope, session_id=cdp_session_id)

        # 8. Register mappings.
        self._scopes[cdp_session_id] = scope
        self._selector_maps[cdp_session_id] = {}
        self._target_to_session[target_id] = cdp_session_id
        self._session_to_target[cdp_session_id] = target_id
        self._watchdogs[target_id] = watchdogs

        return cdp_session_id

    # ── Watchdog construction ─────────────────────────────────────────

    def _watchdog_context(self, cdp_session_id: str | None) -> WatchdogContext:
        """Assemble the single injection object every watchdog is built from.

        Replaces the per-watchdog lambda table: the closures below are
        the only thing that was ever watchdog-specific, and both of them
        exist because ``DOMWatchdog`` produces the per-tab selector map
        that ``ActionWatchdog`` consumes.
        """
        cdp = self._cdp
        if cdp is None:
            raise RuntimeError("BrowserCapability not set up")

        def _selector_map_getter() -> DOMSelectorMap:
            if cdp_session_id is not None:
                return self._selector_maps.get(cdp_session_id, {})
            return {}

        def _selector_map_setter(selector_map: DOMSelectorMap) -> None:
            if cdp_session_id is not None:
                self._selector_maps[cdp_session_id] = selector_map

        # Per-tab main_frame_id getter — ``_on_navigate`` uses it to
        # filter sub-frame Page.lifecycleEvent so we only wait on the
        # main frame's load/networkidle.
        def _main_frame_id_getter() -> str | None:
            if cdp_session_id is not None:
                return self._main_frame_ids.get(cdp_session_id)
            return None

        return WatchdogContext(
            cdp=cdp,
            profile=self.profile_config,
            viewport=(self.viewport_width, self.viewport_height),
            cdp_session_id=cdp_session_id,
            debug_highlight=self.debug_highlight,
            mouse_trajectory=self.mouse_trajectory,
            typing_rhythm=self.typing_rhythm,
            dom_cleaner=self.dom_cleaner,
            screenshot_format=self.screenshot_format,
            screenshot_quality=self.screenshot_quality,
            selector_map_getter=_selector_map_getter,
            selector_map_setter=_selector_map_setter,
            main_frame_id_getter=_main_frame_id_getter,
            tabs_getter=self._get_tabs_safe,
        )

    def _create_watchdogs_for_scope(
        self,
        level: str = "tab",
        cdp_session_id: str | None = None,
    ) -> list[BrowserWatchdog]:
        """Build the profile's watchdogs for one scope level.

        Registry-driven: no name is written here, so a new watchdog needs
        no edit. ``level`` and ``provides`` come off the class.
        """
        if level == "tab" and not cdp_session_id:
            raise ValueError("Tab-level watchdogs require a cdp_session_id")

        ctx = self._watchdog_context(cdp_session_id)
        return [
            wd_cls.build(ctx)
            for wd_cls in self._watchdog_classes().values()
            if wd_cls.level == level
        ]

    def _attach_browser_level_watchdogs(self, scope: EventScope) -> None:
        """Attach browser-level watchdogs (crash) to the global scope."""
        watchdogs = self._create_watchdogs_for_scope(level="browser")
        for wd in watchdogs:
            wd.attach(scope, session_id=None)
        self._watchdogs[_GLOBAL_SCOPE_ID] = watchdogs

