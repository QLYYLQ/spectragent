"""BrowserCapability — CDP-based browser automation as a Capability.

Direct rewrite of the legacy ``BrowserSession`` into a class that
satisfies ``CapabilityBase``. Owns the entire CDP infrastructure
(CDPClient, ScopeGroup, Watchdog system) and exposes browser actions
plus per-fragment observation resolution to the framework.

Key design points:

- **Watchdog = Resolver, for real**: watchdogs come from ``agent_cdp``'s
  ``WatchdogRegistrar``. Each declares ``level`` (tab / browser / shared) and
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
  cache. **Which cycle it is, is told to us**: ``Environment.observe``
  owns the counter and announces each observation through
  :meth:`BrowserCapability.begin_observation`, so the cache is keyed on
  that number and nothing here has to infer where one observation ends.
  ``execute_action()`` drops the acted tab's cycle as well, so an action's
  effects are never observed through the capture that preceded them.
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
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from agent_cdp import CDPEventBridge, EventScope, ScopeGroup
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase, ActionRegistrar
from spectragent.core.archive import ArchiveBatch, ArchiverBase
from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities._registrar import CapabilityBase, negotiated_fragment_keys
from spectragent.core.capabilities.base import CaptureComparison
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
from spectragent.core.capabilities.browser.archive import (  # noqa: F401 — registration
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveRun,
    DomRawArchiver,
    NetworkArchiver,
    ParkedRaw,
    ReplayLogArchiver,
    StorageArchiver,
)
from spectragent.core.capabilities.browser.backends.base import BaseBrowserProvider
from spectragent.core.capabilities.browser.backends.browser_defaults import (
    BrowserDefaults,
    measure_browser_defaults,
)
from spectragent.core.capabilities.browser.backends.capabilities import LAUNCH_ARGS
from spectragent.core.capabilities.browser.capture_check import compare_capture
from spectragent.core.capabilities.browser.cdp_client import CDPClient, transport_options
from spectragent.core.capabilities.browser.cooperation import (
    BrowserCooperationError,
    binding_problems,
    browser_archivers,
    cooperation_problems,
    launch_args,
    launch_env,
    persistent_profile,
    planned_browser_defaults,
    planned_launch_flags,
    recorder_needs,
)
from spectragent.core.capabilities.browser.dom.budget import DOMBudget
from spectragent.core.capabilities.browser.dom.cleaning import (
    DOMCleanerProtocol,
    default_cleaner,
)
from spectragent.core.capabilities.browser.dom.cleaning.browser_use import (  # noqa: F401 — registration
    BrowserUseCleaner,
)
from spectragent.core.capabilities.browser.dom.cleaning.skyvern import (  # noqa: F401 — registration
    SkyvernCleaner,
)
from spectragent.core.capabilities.browser.dom.cleaning.skyvern_hit_test import (  # noqa: F401 — registration
    SkyvernHitTestCleaner,
)
from spectragent.core.capabilities.browser.dom.models import DOMSelectorMap, TargetCapture
from spectragent.core.capabilities.browser.dom.serializers import (
    DOMSerializerProtocol,
    default_serializer,
)
from spectragent.core.capabilities.browser.dom.serializers.html_serializer import (  # noqa: F401 — registration
    HTMLSerializer,
)
from spectragent.core.capabilities.browser.dom.serializers.text_serializer import (  # noqa: F401 — registration
    TextSerializer,
)
from spectragent.core.capabilities.browser.input import (
    InstantRhythm,
    InstantTrajectory,
    MouseTrajectoryProtocol,
    TypingRhythmProtocol,
)
from spectragent.core.capabilities.browser.navigation import (
    MainFrameLoad,
    MainFrameNavigation,
    NavigationFeed,
)
from spectragent.core.capabilities.browser.observations import BrowserCaptureFragment
from spectragent.core.capabilities.browser.profiles import (
    BrowserProfileConfig,
    resolve_profile,
)
from spectragent.core.capabilities.browser.recorder import (
    ArchiveChannel,
    RecorderConnection,
    RecorderLostError,
    RecorderParticipant,
)
from spectragent.core.capabilities.browser.replay import (  # noqa: F401 — registration
    ArchiveNetworkSource,
    LiveNetworkSource,
    NetworkSourceProtocol,
    NoneReplayability,
    PinnedReplayability,
    ReplayabilityPlan,
    ReplayabilityProtocol,
    StrictMissPolicy,
    default_network_source,
    default_replayability,
)
from spectragent.core.capabilities.browser.replay.archive_index import (
    RecordedArchive,
    load_fingerprint,
)
from spectragent.core.capabilities.browser.replay.consistency import (
    browser_default_problems,
    consistency_notes,
    consistency_problems,
    replay_t0,
    version_warnings,
)
from spectragent.core.capabilities.browser.replay.server import ReplayContext, ReplayServer
from spectragent.core.capabilities.browser.restoration import measure, retarget_action
from spectragent.core.capabilities.browser.settle import (
    DEFAULT_SETTLE_MAX_WAIT_S,
    DEFAULT_SETTLE_QUIET_MS,
    PageSettle,
    wait_for_settle,
)
from spectragent.core.capabilities.browser.start_page import start_page_problem

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
    DocumentStamp,
    DOMContentLoadedEvent,
    LifecycleEvent,
    PageLoadEvent,
    describe_stamps,
    documents_disagree,
)
from spectragent.schemas._types import UNLIMITED, Unlimited, limit_value
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.provider_session import ProviderSession
from spectragent.schemas.restoration import RestorationEvidence, Retarget
from spectragent.schemas.task import REPLAY_ARCHIVE_KEY

logger = logging.getLogger(__name__)


# Scope ID for browser-level (non-tab-specific) watchdogs.
_GLOBAL_SCOPE_ID = "__browser__"
# Scope ID of the browser-level archive scope (§4.14): the recorder's
# lifecycle, created only while archivers are bound.
_ARCHIVE_SCOPE_ID = "__archive__"

# How many times one observe cycle re-fires ``BrowserStateRequestEvent``
# before giving up. A DOM/screenshot scrape can lose a race against an
# in-flight navigation (the target is swapping documents and every CDP
# call in the batch fails); re-firing once costs one round trip and
# recovers that case. Exhausting the attempts raises — never serves an
# empty fragment.
_OBSERVE_MAX_ATTEMPTS = 2

# How long one capture attempt waits for the focused tab's in-flight
# main-frame navigation to commit before capturing anyway (§4.14, M33). A
# click on a form's submit button returns before the navigation it starts,
# and a screenshot sent between that request and its commit is refused or
# never answered under pinned's ``--disable-back-forward-cache``. Nothing in
# flight means no wait at all; the bound is per attempt.
_NAVIGATION_SETTLE_TIMEOUT_S = 10.0

# How long each command measuring the browser's defaults right after launch
# may take (§4.14): the first tab's renderer may still be starting.
_BROWSER_DEFAULTS_TIMEOUT_S = 5.0

# How long ``create_tab`` waits for the recorder connection to prepare the
# tab it created before navigating it (§4.14). An ``about:blank`` tab is
# prepared in tens of milliseconds.
_NEW_TAB_PREPARED_TIMEOUT_S = 10.0


# ── Cache entry ───────────────────────────────────────────────────────


@dataclass
class _ObserveCycleCache:
    """One tab's capture for one observation cycle.

    ``fragments`` is keyed by fully-qualified fragment key. It used to be
    one hard-coded attribute per fragment, which forced both a schema
    edit and an ``if/elif`` branch in ``resolve_fragment`` for every new
    fragment; a dict needs neither.

    ``cycle`` is the number :meth:`BrowserCapability.begin_observation`
    was last told, stamped at capture. An entry from an older cycle is not
    served — it is replaced by a fresh capture. It replaced an inference:
    the cache used to track which keys it had already *served* and treat a
    second read of one key as the start of the next observation. That
    guess was right for the loop's access pattern and wrong for anything
    else (a hook or an action reading a fragment twice silently paid for a
    second scrape), and it existed only because nothing ever told this
    capability where an observation began.

    ``cycle`` is **not** the only invalidation path, and assuming it is
    would lose the other one: :meth:`BrowserCapability.execute_action`
    pops the acted tab's entry outright, without advancing the cycle,
    because an action's effects must never be read through the capture
    that preceded them and the loop does not open a new cycle between its
    act and whatever reads next. Cycle number = "this observation is
    over"; pop = "this tab changed under me".

    ``error`` is the third piece, and it is what makes a *failed* cycle
    cost the same as a successful one. This capability's capture is
    all-or-nothing: if the batched event did not produce every negotiated
    fragment, ``_populate_cycle`` raises and leaves ``fired`` False. Since
    ``Environment.observe`` isolates failures per fragment, it goes on to
    ask for the next key of the same slice — which would re-enter the
    whole ``_OBSERVE_MAX_ATTEMPTS`` retry budget, once per fragment, so a
    dom+screenshot+meta run burned six full DOM extractions to fail one
    observation. Recording the failure on the cycle means the retry budget
    is spent once and every other fragment of that cycle is refused with
    the same exception, immediately.

    Two failures the history is worth keeping:

    - The cycle once lived until the next ``execute_action``. The page
      does not wait for one — a spinner resolves into a challenge, a modal
      fades in — so a Loop step that ran no action (a reply that failed to
      parse) was handed the previous step's screenshot byte for byte, 5.5
      seconds after the page had changed, and the agent clicked on what
      was no longer there.
    - ``Environment.observe`` asks for every negotiated fragment of one
      cycle in sequence, so they share one ``BrowserStateRequestEvent``
      and the CDP cost of a step stays constant however many fragments the
      agent requested.
    """

    #: Observation cycle this capture belongs to.
    cycle: int = 0
    fragments: dict[str, Any] = field(default_factory=dict)
    fired: bool = False  # whether the batched event has run for this cycle
    #: Why this cycle's capture failed, if it did. Re-raised for every
    #: other fragment of the same cycle instead of re-attempting it.
    error: BaseException | None = None
    #: The pending raw-DOM slot (§4.12): captures parked by the DOM resolver's
    #: sink or ``browser.dom_raw``'s self-capture during the current attempt.
    #: Emptied when an attempt starts; committed to the archive only when the
    #: cycle succeeds, so the archive holds exactly what the agent was served.
    raw: list[ParkedRaw] = field(default_factory=list)


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
    ``stamps`` holds each watchdog's document stamp, by handler, for the
    same-document check (§4.14).
    """

    fragments: dict[str, Any] = field(default_factory=dict)
    handler_count: int = 0
    errors: list[str] = field(default_factory=list)
    stamps: dict[str, DocumentStamp] = field(default_factory=dict)


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
        # ``typing_rhythm`` is the keyboard's half of the same idea, on its
        # own axis — a run may want a curved cursor and a bulk insert, or the
        # reverse — and with the same rule: the strategy *is* the switch,
        # there is no boolean beside it to disagree with it. ``instant`` is
        # the legacy single ``Input.insertText``, which fires no key events
        # at all; ``human`` dispatches a real keydown/keyup per character on
        # modelled flight and hold times, at a cost of roughly 250 ms per
        # character — meant for anti-bot-sensitive evals, not throughput.
        "typing_rhythm": ("typing_rhythm", ["human", "instant"]),
        # ``dom_cleaner`` is a receptor for the same reason: the strategy
        # arrives constructed, carrying the knobs that decide what the
        # agent gets to see (``descendants_occlude``,
        # ``max_occlusion_nodes``, ``include_shadow_dom``, ...). It used to
        # be a name on ``BrowserProfileConfig``, which meant ``DOMWatchdog``
        # looked the class up and default-constructed it — the generated
        # ``BrowserUseDom_CleanerConfig`` existed and nothing could reach it.
        # ``skyvern_hit_test`` (M32): Skyvern plus an in-page hit test per
        # indexed element, recorded as ``hit_test``; ``covered_elements``.
        "dom_cleaner": ("dom_cleaner", ["browser_use", "skyvern", "skyvern_hit_test"]),
        # ``dom_serializer`` is the third instance of that identical
        # defect and closes it: the serializer decides what the *prompt*
        # says the page is, and ``TextSerializer.max_text_length`` /
        # ``HTMLSerializer.max_href_length`` were declared, generated into
        # ``TextDom_SerializerConfig`` / ``HtmlDom_SerializerConfig``, and
        # unreachable, because ``profile.serializer`` was a name and
        # ``DOMWatchdog`` did ``DOMSerializerRegistrar.get(name)()``.
        # ``tests/.../test_receptor_hygiene.py`` is the structural guard
        # against a fourth.
        "dom_serializer": ("dom_serializer", ["text", "html"]),
        # ``replayability`` (§4.14) decides how far a run is pinned for replay:
        # ``none`` (the default, the no-op) or ``pinned`` (seeded randomness, a
        # stepped clock from T0, fixed time zone / locale / user agent / DPR,
        # its own Chrome launch switches). Same shape as ``typing_rhythm``:
        # the strategy is the switch, so YAML names one in every capability
        # block, and nothing beside it can disagree with it. Anything but
        # ``none`` opens the recorder connection.
        "replayability": ("replayability", ["none", "pinned"]),
        # ``network_source`` (§4.14 "回放") decides where the network comes
        # from: ``live`` (the default, the no-op) or ``archive`` (every request
        # answered from a recorded run's archive at the Fetch Request stage,
        # never continued to the network; its ``miss_policy`` receptor answers
        # what the recording lacks). Same shape again: the strategy is the
        # switch — whether the browser must be isolated from the network is the
        # source's own property, never a provider boolean beside it. Anything
        # but ``live`` opens the recorder connection.
        "network_source": ("network_source", ["archive", "live"]),
        # Archive provides (§4.5, §4.14) — the third axis, a negotiation-only
        # declaration like ``observation``: what a run records is the trace
        # hook's ``archive`` list, bound here by ``bind_archivers``. Keep in
        # sync with the classes in ``browser/archive/`` (imported above, so
        # the keys resolve when conscribe reads this entry).
        "archiver": (
            "archiver",
            ["browser.dom_raw", "browser.network", "browser.replay_log", "browser.storage"],
        ),
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
    typing_rhythm: TypingRhythmProtocol  # type: ignore[misc]
    dom_cleaner: DOMCleanerProtocol  # type: ignore[misc]
    dom_serializer: DOMSerializerProtocol  # type: ignore[misc]
    replayability: ReplayabilityProtocol  # type: ignore[misc]
    network_source: NetworkSourceProtocol  # type: ignore[misc]
    backend: BaseBrowserProvider  # type: ignore[misc]

    # ── conscribe-visible config (assigned in __init__) ──────────────
    cdp_url: str | None
    profile: str
    viewport_width: int
    viewport_height: int
    debug_highlight: bool
    screenshot_format: Literal["png", "jpeg"]
    screenshot_quality: int
    dom_max_elements: int | Unlimited
    dom_max_serialized_chars: int | Unlimited
    dom_interactive_only: bool
    dom_viewport_only: bool
    settle_quiet_ms: int
    settle_max_wait_s: float
    retarget_tolerance_px: float

    # ── internal state, never config (assigned in __init__) ──────────
    # Declared here as type-only annotations so both pyright and the
    # conscribe stub writer see them; the values are set in ``__init__``.
    _actions_by_name: dict[str, ActionBase]
    _cdp: CDPClient | None
    _session: ProviderSession | None
    _scope_group: ScopeGroup | None
    #: Observation scopes by tab CDP session.
    _scopes: dict[str, EventScope]
    #: Independent JS execution scopes for the same tab sessions.
    _js_scopes: dict[str, EventScope]
    _bridges: dict[str, CDPEventBridge]
    _main_frame_ids: dict[str, str]
    #: Each tab's main-frame navigation state and the feed keeping it current,
    #: by tab CDP session (§4.14, M33). Only tabs whose main frame id is known.
    _navigation_feeds: dict[str, NavigationFeed]
    _selector_maps: dict[str, DOMSelectorMap]
    _target_to_session: dict[str, str]
    _session_to_target: dict[str, str]
    _watchdogs: dict[str, list[BrowserWatchdog]]
    _shared_watchdogs: list[BrowserWatchdog]
    _global_scope: EventScope | None
    _default_session_id: str | None
    _alive: bool
    _resolved_profile: BrowserProfileConfig | None
    #: Per-tab observe-cycle cache. Keyed by session_id.
    _cycle_cache: dict[str, _ObserveCycleCache]
    #: The observation cycle this capability was last told about.
    _observation_cycle: int
    #: Optional injected client for tests.
    _injected_client: CDPClient | None
    #: Optional transport factory for tests (control and recorder clients).
    _cdp_factory: Callable[..., Any] | None
    #: The recorder connection, while one is open (§4.14).
    _recorder: RecorderConnection | None
    #: Why the recorder connection was lost, once it was.
    _recorder_error: RecorderLostError | None
    #: This run's replayability plan (a recorder participant).
    _replay_plan: ReplayabilityPlan | None
    #: This run's replay server, when the network source intercepts (M30).
    _replay_server: ReplayServer | None
    #: The bound archivers (``bind_archivers``), in binding order.
    _archivers: list[BrowserArchiverBase]
    #: The archive side of the current recording, between setup and teardown,
    #: when archivers were bound at setup (§4.14).
    _archive: BrowserArchiveRun | None
    #: The replay log's ``diverged_total`` at the last ``verify_restoration``
    #: — what makes each measurement count only the misses since the previous one.
    _diverged_seen: int
    #: The time zone and locale a frame no override reaches falls back to,
    #: measured right after launch when archivers are bound (§4.14).
    _browser_defaults: BrowserDefaults | None

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
        dom_max_elements: Annotated[
            Annotated[int, PydanticField(ge=1)] | Unlimited,
            PydanticField(
                description=(
                    "Cap on indexed elements in the browser.dom observation "
                    "fragment, kept in document order. The DOM half of "
                    "screenshot_quality: a listing page indexes 400+ elements "
                    "and every one of them costs prompt tokens on every step. "
                    "What a dropped element loses is its index: the selector "
                    "map is capped with it, so no action can reach it. It may "
                    "still appear in the serialization as unindexed structure "
                    "— a scroll container or an iframe still prints its tag — "
                    "which is why this caps cost rather than hiding a page. "
                    "A number of at least 1, or 'unlimited' (the default): no "
                    "cap. Whenever this cuts, the fragment says so in "
                    "DOMObs.budget."
                ),
            ),
        ] = UNLIMITED,
        dom_max_serialized_chars: Annotated[
            Annotated[int, PydanticField(ge=1)] | Unlimited,
            PydanticField(
                description=(
                    "Budget for the serialized DOM string, cut at the last "
                    "line boundary inside it. Applied after the element "
                    "caps, so it is the last resort rather than the first. "
                    "The kept text ends with a '[dom budget: N of M "
                    "characters shown]' note, so the model is told it is "
                    "reading a cut page, and the cut is recorded in "
                    "DOMObs.budget for the trace. The note is metadata about "
                    "the cut and is not charged against this budget, so the "
                    "final string runs ~45 characters over it: this bounds "
                    "page content, it is not a hard cap on the field. A "
                    "number of at least 1, or 'unlimited' (the default): no "
                    "budget."
                ),
            ),
        ] = UNLIMITED,
        dom_interactive_only: Annotated[
            bool,
            PydanticField(
                description=(
                    "Keep only what the agent can act on: drop indexed scroll "
                    "containers that are not themselves interactive, and drop "
                    "from the tree every node that is neither indexed nor an "
                    "ancestor of an indexed one. That second half removes the "
                    "page's prose, which is where the tokens are — and "
                    "removes it for a reading agent too, so leave it off for "
                    "tasks whose answer is in the text."
                ),
            ),
        ] = False,
        dom_viewport_only: Annotated[
            bool,
            PydanticField(
                description=(
                    "Drop indexed elements whose bounds do not intersect the "
                    "scrolled viewport, so a long page costs what is on "
                    "screen. The agent must scroll to reach the rest. An "
                    "element with no measured bounds is kept: this never cuts "
                    "what it cannot measure."
                ),
            ),
        ] = False,
        settle_quiet_ms: Annotated[
            int,
            PydanticField(
                ge=0,
                le=10_000,
                description=(
                    "Before every observation the capture waits for the page "
                    "to settle: the document loaded, then no DOM change and no "
                    "resource finishing loading for this many milliseconds, "
                    "measured from the moment the wait reaches the page. The "
                    "same wait serves every browser fragment (DOM, screenshot, "
                    "metadata), in recording and in replay alike. 0 skips the "
                    "quiet window and waits for the load only. Default 500."
                ),
            ),
        ] = DEFAULT_SETTLE_QUIET_MS,
        settle_max_wait_s: Annotated[
            float,
            PydanticField(
                ge=0,
                le=60,
                description=(
                    "The longest the settle wait may take before an "
                    "observation captures the page anyway, in seconds: a page "
                    "that never goes quiet (a carousel, a video, a poll) "
                    "costs this much on each step. 0 turns the wait off: the "
                    "observation captures at once. Each fragment records how "
                    "long its capture waited and whether this cap ended the "
                    "wait. Default 5."
                ),
            ),
        ] = DEFAULT_SETTLE_MAX_WAIT_S,
        retarget_tolerance_px: Annotated[
            float,
            PydanticField(
                ge=0,
                description=(
                    "Checkpoint restoration (branch runs): how far, in CSS "
                    "pixels of page coordinates, each of the four bounds "
                    "values (x, y, width, height) of a restored element may "
                    "lie from the recorded element's for the restored one to "
                    "be a candidate when a recorded element index is "
                    "re-found. Candidates are then confirmed by the element "
                    "signature (tag, id, name, aria-label, role, text hash). "
                    "Read only by a restorer; an ordinary run never uses it."
                ),
            ),
        ] = 2.0,
        actions: Annotated[
            list[ActionBase] | None,
            PydanticField(
                description=(
                    "Actions this capability instantiates, each with its own "
                    "side-effect configuration (scroll distance, navigation "
                    "wait mode, post-action settle delay, ...). Omitted or null "
                    "(the default): every action the wiring declares. Listing a "
                    "subset is an ablation: the actions you leave out are not "
                    "built, are not advertised to the model, and will fail "
                    "action negotiation if an agent requires one of them. [] "
                    "builds no action at all."
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
        self.dom_max_elements = dom_max_elements
        self.dom_max_serialized_chars = dom_max_serialized_chars
        self.dom_interactive_only = dom_interactive_only
        self.dom_viewport_only = dom_viewport_only
        if settle_max_wait_s > 0 and settle_quiet_ms > settle_max_wait_s * 1000:
            raise ValueError(
                f"settle_quiet_ms ({settle_quiet_ms}) is longer than settle_max_wait_s "
                f"({settle_max_wait_s:g} s): the quiet window could never be observed "
                "before the cap, so every observation would wait the whole cap. Shorten "
                "the window, raise the cap, or set settle_max_wait_s: 0 to turn the wait off."
            )
        self.settle_quiet_ms = settle_quiet_ms
        self.settle_max_wait_s = settle_max_wait_s
        self.retarget_tolerance_px = retarget_tolerance_px

        # In-Python default for the wired trajectory receptor. YAML must
        # name one (a single-instance receptor is required in the
        # generated schema, exactly like ``backend``); constructing the
        # capability directly gets the no-op strategy, which is the
        # legacy instant-jump behaviour. ``ConfigComposer`` setattrs the
        # configured instance over this, shadowing the descriptor.
        self.mouse_trajectory = InstantTrajectory()
        # Same contract for the keyboard's rhythm and for the DOM cleaning
        # strategy: YAML names one, a hand-built capability gets the default
        # strategy with its defaults.
        self.typing_rhythm = InstantRhythm()
        # Via the strategy packages' own ``default_*`` factories, not by
        # naming the class here: ``DOMWatchdog`` needs the same default for
        # a hand-built watchdog, and one fact written in two files drifts.
        self.dom_cleaner = default_cleaner()
        self.dom_serializer = default_serializer()
        self.replayability = default_replayability()
        self.network_source = default_network_source()

        # ── internal state (never config) ─────────────────────────
        self._actions_by_name = {}
        self._cdp = None
        self._session = None
        self._scope_group = None
        self._scopes = {}
        self._js_scopes = {}
        self._bridges = {}
        self._main_frame_ids = {}
        self._navigation_feeds = {}
        self._selector_maps = {}
        self._target_to_session = {}
        self._session_to_target = {}
        self._watchdogs = {}
        self._shared_watchdogs = []
        self._global_scope = None
        self._default_session_id = None
        self._alive = False
        self._cycle_cache = {}
        self._observation_cycle = 0
        self._injected_client = None
        self._cdp_factory = None
        self._recorder = None
        self._recorder_error = None
        self._replay_plan = None
        self._replay_server = None
        self._archivers = []
        self._archive = None
        self._diverged_seen = 0
        self._browser_defaults = None

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

        ``None`` means "everything the wiring offers": no ``actions:`` in
        YAML, or ``actions: null`` (the Composer leaves an omitted receptor
        to this default when no top-level ``action:`` section offers any).
        A list is taken literally: actions the config did not name are not
        instantiated, which is the design doc's ablation narrowing, and
        ``[]`` builds none (``docs/architecture_design.md`` §4.2.1: ``[]``
        always means empty).
        """
        offered = self._wired_action_names()
        if value is None:
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
    def dom_budget(self) -> DOMBudget:
        """The four DOM cost knobs, as one value the resolver can hold.

        Assembled here rather than stored, so the scalars stay the config
        surface (four ``Annotated`` ``__init__`` params conscribe can see)
        and the resolver still receives one object rather than four
        arguments it has to keep in sync.
        """
        return DOMBudget(
            max_elements=limit_value(self.dom_max_elements),
            max_serialized_chars=limit_value(self.dom_max_serialized_chars),
            interactive_only=self.dom_interactive_only,
            viewport_only=self.dom_viewport_only,
        )

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
        called before ``setup()``. Control connection only: an injected
        client is a browser this capability did not launch, so a
        configuration that needs the recorder connection refuses it at
        setup (the cooperation check).
        """
        self._injected_client = client

    def _inject_cdp_factory(self, factory: Callable[..., Any]) -> None:
        """Replace the transport for both connections (tests).

        ``factory(url, **options)`` stands in for ``CDPClient(url, **options)``
        and receives exactly the options a real client would. Unlike
        :meth:`_inject_cdp_client` it changes only the transport: the browser
        still comes from the backend, so ownership is still decided by the
        backend's declared ``__capabilities__`` and the recorder connection
        can be exercised against a fake browser.
        """
        self._cdp_factory = factory

    def _new_cdp_client(self, url: str, **options: Any) -> CDPClient:
        """A CDP client for ``url`` — real, or from the injected factory."""
        if self._cdp_factory is not None:
            return self._cdp_factory(url, **options)
        return CDPClient(url, **options)

    # ── Backend access ────────────────────────────────────────────────

    def resolved_backend(self) -> BaseBrowserProvider | None:
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

        The DPR is the ``replayability`` tier's: ``none`` answers ``0``, which
        keeps the platform's real DPR (the DOM watchdog queries it live per
        observe); ``pinned`` answers its own, so a recording and its replay
        render at the same scale. This control connection is the only owner
        of the viewport override (§4.14) — the recorder never sends it.
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
                    "deviceScaleFactor": self.replayability.device_scale_factor,
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

    # ── Archive axis (§4.5, §4.9, §4.14) ─────────────────────────────

    @property
    def bound_archivers(self) -> tuple[BrowserArchiverBase, ...]:
        """The archivers bound by :meth:`bind_archivers`, in binding order."""
        return tuple(self._archivers)

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        """Replace the binding; refuse, before changing anything, what cannot cooperate.

        Acquires nothing (a dry run binds too). The binding rows of the
        cooperation check — provider ownership, ``require_replayable`` vs the
        tier, session state, the profile's watchdogs, which archivers fit the
        network source (a replay's log, live traffic) and its isolation — are
        raised here before anything changes. ``prepare()`` has refused them
        already (:meth:`check_setup`, which runs every row, just before it
        binds); setup checks everything again.
        Bound before setup (the runtime's order), the archivers decide which
        capture channels setup opens; bound again after setup, only who
        receives events and who is collected changes. An archiver bound after
        a setup that opened no archive channel could capture nothing, so that
        is refused.
        """
        bound = browser_archivers(archivers)
        problems = binding_problems(
            self,
            bound,
            cdp_url=self.cdp_url,
            injected_client=self._injected_client is not None,
        )
        if problems:
            raise BrowserCooperationError(problems)
        if bound and self._alive and self._archive is None:
            raise RuntimeError(
                f"BrowserCapability cannot start archiving "
                f"{[a.archiver_id for a in bound]} after setup: no archiver was "
                f"bound at setup, so the recorder opened no archive channel. Bind before setup()."
            )
        self._archivers = bound
        if self._archive is not None:
            self._archive.bind(bound)

    async def collect_archive(self) -> ArchiveBatch:
        """Hand over everything captured since the last collection (§4.14).

        Liveness first — a recorder lost since the last collection raises the
        same ``RecorderLostError`` as every other entry point — then the
        archive channel's barrier, each bound archiver's session in binding
        order, and the fingerprint run record. Never set up, or nothing
        bound: an empty batch.
        """
        archive = self._archive
        if archive is None or not self._archivers:
            return ArchiveBatch()
        self._ensure_recording()
        server = self._replay_server
        return await archive.collect(
            plan_fingerprint=self._replay_plan.fingerprint() if self._replay_plan else {},
            session_metadata=self._session.metadata if self._session is not None else None,
            fresh_profile=self._fresh_profile(),
            clock=time.time,
            network_fingerprint=(
                server.fingerprint() if server is not None else self.network_source.fingerprint()
            ),
            browser_defaults=self._browser_defaults,
        )

    def _fresh_profile(self) -> bool:
        """Whether the browser started from a fresh profile (§5.6's ``logged_in``)."""
        return persistent_profile(self.resolved_backend()) is None

    # ── Checkpoint restoration (RestorationSupport, §4.9 / §4.14) ─────

    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget:
        """How a recorded ``browser.*`` action runs in this restored browser.

        Element indexes are re-found by geometry and signature, tab ids by
        tab order and URL; everything else runs as recorded
        (:func:`~spectragent.core.capabilities.browser.restoration.retarget_action`).
        Computes only — the restorer executes the answer through ``guarded_act``.
        """
        return retarget_action(action, recorded, restored, tolerance_px=self.retarget_tolerance_px)

    async def verify_restoration(self, recorded: Any, restored: Any) -> RestorationEvidence:
        """How far the restored observation is from the recorded one.

        URL, the interactive-element signature multiset and the screenshot
        hash distance are read from the two observations
        (:func:`~spectragent.core.capabilities.browser.restoration.measure`);
        ``document_misses`` is this run's replay server's count of main-frame
        document misses since the previous call (``None`` when the network is
        live) — reported once, so a restorer measures each restored
        observation exactly once (the ``RestorationSupport`` contract).
        Thresholds are the restorer's.
        """
        return measure(
            recorded,
            restored,
            capability_id=self.capability_id,
            document_misses=self._document_misses_since_last_check(),
        )

    def _document_misses_since_last_check(self) -> int | None:
        """Main-frame document misses since the last call; ``None`` when nothing intercepts."""
        server = self._replay_server
        if server is None:
            return None
        total = server.log.diverged_total
        misses, self._diverged_seen = total - self._diverged_seen, total
        return misses

    # ── Recorded capture vs this configuration (CaptureCheck, §4.9) ──

    def compare_capture(self, recorded: Mapping[str, Any]) -> CaptureComparison:
        """Whether this configuration's screenshot pipeline captures what *recorded* holds.

        Viewport, format, device scale factor and image size of
        ``browser.screenshot``
        (:func:`~spectragent.core.capabilities.browser.capture_check.compare_capture`).
        Reads configuration only — never the browser, which the training
        export that asks never starts.
        """
        return compare_capture(
            recorded,
            viewport=(self.viewport_width, self.viewport_height),
            screenshot_format=self.screenshot_format,
            device_scale_factor=self.replayability.device_scale_factor,
        )

    # ── What setup would refuse, refused at prepare() (SetupCheck, §4.14) ──

    def check_setup(
        self,
        context: Mapping[str, Any],
        *,
        negotiation: Any = None,
        archivers: Sequence[ArchiverBase] = (),
    ) -> None:
        """Refuse, acquiring nothing, every reason :meth:`setup` would refuse this run.

        ``AgentRuntime.prepare()`` calls this through
        ``Environment.check_setup`` — before the trace is opened and before
        anything is launched, on a dry run too — with this capability's
        slice of the task's setup context, the observation negotiation and
        the archivers about to be bound. :meth:`setup` calls it again with
        the archivers bound by then (a defensive re-check of the same
        function). Every problem is reported at once, each naming both
        parties:

        - the profile's watchdogs vs the negotiated fragments (a fragment
          the run negotiated that no installed watchdog resolves);
        - the task's ``start_url``: setup opens it, and only one complete
          http(s) URL can be opened (``start_page.py``) — a start page with no
          scheme, another scheme, no host or whitespace is named here;
        - every row of the cooperation check (``cooperation.py``): the
          archivers, the ``replayability`` tier and the ``network_source``
          against the provider, its knobs and its profile, the explicit
          ``cdp_url`` (the context's, else the knob) and the task's
          ``replay_archive``;
        - for a replay, the source run's consistency with this configuration
          (``replay/consistency.py``), read from its environment fingerprint
          alone (``load_fingerprint``: no index line, no WARC record) —
          including the browser's defaults, the time zone and locale this
          host's OS says the browser will fall back to
          (``planned_browser_defaults``), against the recording's.

        Raises:
            BrowserCooperationError: Every problem found.
            ArchiveOpenError: ``replay_archive`` names no committed run
                directory.
            TypeError: An archiver that is not a browser archiver.
        """
        bound = browser_archivers(archivers)
        cdp_url = context.get("cdp_url") or self.cdp_url
        replay_archive = context.get(REPLAY_ARCHIVE_KEY)
        injected = self._injected_client is not None
        problems = self._unresolvable_fragments(negotiation)
        start_page = start_page_problem(context.get("start_url"))
        if start_page is not None:
            problems.append(start_page)
        problems.extend(
            cooperation_problems(
                self,
                cdp_url=cdp_url,
                injected_client=injected,
                replay_archive=replay_archive,
                archivers=bound,
            )
        )
        if self.network_source.intercepts and replay_archive:
            problems.extend(
                consistency_problems(
                    load_fingerprint(open_archive(replay_archive)),
                    replayability=self.replayability,
                    viewport=(self.viewport_width, self.viewport_height),
                    planned_launch_flags=planned_launch_flags(
                        self, cdp_url=cdp_url, injected_client=injected
                    ),
                    browser_defaults=planned_browser_defaults(
                        self, cdp_url=cdp_url, injected_client=injected
                    ),
                )
            )
        if problems:
            raise BrowserCooperationError(problems)

    def _unresolvable_fragments(self, negotiation: Any) -> list[str]:
        """The profile row: negotiated fragments of this capability no installed watchdog resolves.

        The profile is resolved afresh (it may have been reassigned since
        construction, and negotiation read the construction-time one); an
        unknown watchdog name in it raises here, naming the watchdog. Only
        fragments this capability owns count — a composite environment
        negotiates fragments other capabilities serve.
        """
        negotiated = negotiated_fragment_keys(negotiation)
        profile = resolve_profile(self.profile)
        installed = browser_watchdog_classes(
            exclude=validate_watchdog_names(profile.watchdogs_exclude)
        )
        if negotiated is None:
            return []
        resolvable = fragments_provided_by(installed.values())
        ours = fragments_provided_by(browser_watchdog_classes().values())
        missing = sorted((negotiated & ours) - resolvable)
        if not missing:
            return []
        return [
            f"profile {self.profile!r} (watchdogs_exclude "
            f"{sorted(profile.watchdogs_exclude)}) ↔ negotiated fragment(s) {missing}: no "
            f"installed watchdog ({sorted(installed)}) resolves them; drop the fragment from "
            f"the agent's __wiring__ or stop excluding the watchdog that provides it"
        ]

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
        replay_archive: str | None = None,
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
        first observe, with ``browser.navigate``: one complete http(s) URL,
        which :meth:`check_setup` checks before anything is launched (a page
        that commits but never fires ``load`` is opened with a warning; one
        that never commits fails setup).

        ``replay_archive`` is the source run directory an
        intercepting ``network_source`` (``archive``, M30) replays: opened
        through ``core/archive``'s ``open_archive`` and loaded whole, to be
        served, before anything is launched (§4.14).
        The remaining keys are the rest of :class:`BrowserSetup`'s
        vocabulary; they are accepted so a task carrying them routes
        cleanly, and reported at WARNING because this capability does not
        act on them yet.

        Before anything is acquired, :meth:`check_setup` — the check
        ``prepare()`` ran — runs again with the archivers bound by now: a
        caller may set up without preparing, and a test may have injected a
        control client since.
        """
        self.apply_negotiation(negotiation)

        # Re-resolve in case ``profile`` was mutated after construction
        # (``check_setup`` below resolves it the same way and fails loudly on
        # an unknown watchdog name, rather than mid-run).
        self._resolved_profile = resolve_profile(self.profile)

        # Resolve CDP URL: per-call override > constructor field.
        cdp_url = cdp_url or self.cdp_url

        # Can the configured parts cooperate at all — and, for a replay, does
        # the source run match this configuration? The check ``prepare()``
        # ran, again, before anything is launched: every problem reported at
        # once, each naming both parties (§4.14).
        self.check_setup(
            {"cdp_url": cdp_url, REPLAY_ARCHIVE_KEY: replay_archive, "start_url": start_url},
            negotiation=negotiation,
            archivers=self.bound_archivers,
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

        injected = self._injected_client is not None
        planned = planned_launch_flags(self, cdp_url=cdp_url, injected_client=injected)
        # What this host's OS says the browser will fall back to, read before
        # launch; compared with Chrome's own answer when archivers measure it.
        expected_defaults = (
            planned_browser_defaults(self, cdp_url=cdp_url, injected_client=injected)
            if self._archivers
            else None
        )
        recorded: RecordedArchive | None = None
        if self.network_source.intercepts and replay_archive:
            # The whole archive, to serve: the check read its fingerprint only.
            recorded = self.network_source.open_replay(open_archive(replay_archive))
        self._recorder_error = None
        # The run's start: T0 of a pinned tier's clock — or the recording's,
        # under base_time: from_archive, resolved before any shim is built.
        now_ms = int(time.time() * 1000)
        t0_ms = replay_t0(recorded, self.replayability, now_ms) if recorded else now_ms

        self._scope_group = ScopeGroup("browser-capability")

        # 1. CDPClient (WebSocket) — either injected (tests) or fresh.
        if self._injected_client is not None:
            self._cdp = self._injected_client
        else:
            # Last-resort: ask the wired backend to allocate a session.
            backend = self.resolved_backend()
            if cdp_url is None and backend is not None:
                # ``open()`` gets no framework-level kwargs beyond
                # ``launch_args`` and ``launch_env``, and those only when the
                # provider declares ``LAUNCH_ARGS`` and the configuration has
                # switches or an environment for it (the replayability tier's,
                # then a replay's isolation switches — ``cooperation.launch_args``
                # / ``launch_env``). Every cloud provider declares an explicit
                # keyword-only signature of *its own* options (skyvern:
                # timeout_minutes/adblock/..., airtop: profile_name/...,
                # lexmount: browser_mode/...), so passing a generic
                # ``viewport=`` here was a TypeError for all of them and a
                # silently-ignored kwarg for LocalChrome. The viewport is a
                # browser-level concern and is applied per tab via
                # ``Emulation.setDeviceMetricsOverride`` in _attach_tab.
                switches, env = launch_args(self), launch_env(self)
                if (switches or env) and LAUNCH_ARGS in getattr(
                    type(backend), "__capabilities__", ()
                ):
                    self._session = await backend.open(launch_args=switches, launch_env=env)
                else:
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
            self._cdp = self._new_cdp_client(
                cdp_url,
                additional_headers=self._session_cdp_headers(),
                **transport_options(cdp_url),
            )
            await self._cdp.start()

        # 1b. The recorder connection (§4.14), built but not yet attached: its
        # client, the archive scope and channel, this run's plan and the bound
        # archivers' sessions.
        if recorder_needs(self):
            assert cdp_url is not None  # the cooperation check refused anything else
            await self._connect_recorder(
                cdp_url,
                t0_ms=t0_ms,
                recorded=recorded,
                notes=consistency_notes(recorded, planned_launch_flags=planned)
                if recorded is not None
                else [],
                expected_defaults=expected_defaults,
            )

        # 2. Global scope — browser-level watchdogs. Before the recorder
        # attaches: the ArchiveWatchdog (a shared watchdog, installed by
        # binding) must consume the archive scope from the recorder's first
        # event on, or the collection barrier counts those events as dropped.
        self._global_scope = await self._scope_group.create_scope(_GLOBAL_SCOPE_ID)
        self._attach_browser_level_watchdogs(self._global_scope)
        self._shared_watchdogs = self._create_watchdogs_for_scope(level="shared")
        for wd in self._shared_watchdogs:
            wd.attach(self._global_scope)

        # 2b. The recorder attaches before the first tab: a popup or worker must
        # be prepared before its first request, and the control connection
        # adopts a tab only when it attaches to it.
        if self._recorder is not None:
            await self._recorder.open()

        # 3. Initial tab.
        result = await self._cdp.send("Target.getTargets", {})
        targets = (result or {}).get("targetInfos", [])
        page_target = next((t for t in targets if t.get("type") == "page"), None)

        if page_target:
            sid = await self._attach_tab(page_target["targetId"], page_target.get("url", ""))
        else:
            r = await self._cdp.send("Target.createTarget", {"url": "about:blank"})
            target_id = (r or {}).get("targetId")
            if not target_id:
                raise RuntimeError("Target.createTarget returned no targetId")
            sid = await self._attach_tab(target_id, "about:blank")

        self._default_session_id = sid
        self._alive = True

        # The task's landing page. Routed here by name from
        # ``TaskLoader.get_task_context()``; it once arrived in a
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

    async def _connect_recorder(
        self,
        cdp_url: str,
        *,
        t0_ms: int,
        recorded: RecordedArchive | None = None,
        notes: Sequence[str] = (),
        expected_defaults: BrowserDefaults | None = None,
    ) -> None:
        """Build the recorder connection; :meth:`setup` opens it after the watchdogs.

        Its own client: no compression (large frames are 13–48x slower with
        permessage-deflate), no proxy (loopback CDP would tunnel through
        ``http_proxy``), no frame limit (a paused upload arrives twice). The
        connection is kept on ``self`` before it opens, so a failure part way
        is still cleaned up by :meth:`teardown`.

        With archivers bound, the browser's defaults are measured here, before
        the recorder prepares any target (:meth:`_measure_browser_defaults`).

        Replaying *recorded* (M30), the network source's server is built here
        — pure construction, before the archive sessions, whose context
        carries its log — and the source run's tolerated mismatches
        (*notes*, the Chrome version) become its warnings.
        """
        client = self._new_cdp_client(
            cdp_url,
            additional_headers=self._session_cdp_headers(),
            max_frame_size=None,
            compression=None,
            proxy=None,
        )
        await client.start()
        # With archivers bound: the browser-level archive scope and its channel,
        # before the recorder exists (it emits into the channel from its first
        # attach). Shim-only (no archiver): neither.
        channel: ArchiveChannel | None = None
        if self._archivers:
            assert self._scope_group is not None
            scope = await self._scope_group.create_scope(_ARCHIVE_SCOPE_ID)
            channel = ArchiveChannel(scope, self._recorder_participants)
            self._archive = BrowserArchiveRun(channel=channel, archivers=self._archivers)
        recorder = RecorderConnection(
            client,
            participants=self._recorder_participants,
            channel=channel,
            on_lost=self._recorder_lost,
        )
        self._recorder = recorder
        version = await recorder.browser_version()
        if self._archivers:
            self._browser_defaults = await self._measure_browser_defaults(
                recorder, expected=expected_defaults, recorded=recorded
            )
        self._replay_plan = self.replayability.plan(
            t0_ms=t0_ms, browser_user_agent=str(version.get("userAgent", ""))
        )
        if recorded is not None:
            server = self.network_source.serve(
                recorded,
                ReplayContext(recorder=recorder, cycle=lambda: self._observation_cycle),
            )
            for warning in [
                *notes,
                *version_warnings(
                    recorded, browser_version=version, replayability=self.replayability
                ),
            ]:
                server.warn(warning)
            self._replay_server = server
            self._diverged_seen = 0
        if self._archive is not None and channel is not None:
            # The bound archivers' run-time sessions — participants before the
            # first target is prepared.
            self._archive.start(
                BrowserArchiveContext(
                    recorder=recorder,
                    channel=channel,
                    raw_captures=self._archive.raw_captures,
                    park_raw=self._park_self_capture,
                    fragments=self.provided_fragments,
                    viewport=(self.viewport_width, self.viewport_height),
                    cycle=lambda: self._observation_cycle,
                    fresh_profile=self._fresh_profile(),
                    replay_log=(
                        self._replay_server.log if self._replay_server is not None else None
                    ),
                ),
                self._archivers,
            )

    async def _measure_browser_defaults(
        self,
        recorder: RecorderConnection,
        *,
        expected: BrowserDefaults | None,
        recorded: RecordedArchive | None,
    ) -> BrowserDefaults | None:
        """Chrome's default time zone and locale, asked right after launch (§4.14).

        Read in the first tab before the recorder prepared it, so no override
        reached it: what a frame no override reaches falls back to. The
        fingerprint records it. A difference from what the OS said before
        launch (*expected*) is logged — the check ``prepare()`` ran may then
        be wrong on this host. Replaying *recorded*, every value is compared
        with the recording's here, so what the OS could not say before launch
        is decided too; a difference, or a measurement that failed, refuses
        the replay before any target is prepared. Recording, a failed
        measurement is logged and recorded as no defaults, which refuses any
        replay of this run.
        """
        try:
            measured = await measure_browser_defaults(recorder, timeout=_BROWSER_DEFAULTS_TIMEOUT_S)
        except Exception as exc:
            if recorded is not None:
                raise BrowserCooperationError(
                    [
                        f"source run {recorded.run_id!r} recorded browser defaults ↔ this "
                        f"browser's defaults could not be measured after launch ({exc!r}), so "
                        f"nothing says the replay falls back to the same time zone and locale"
                    ]
                ) from exc
            logger.warning(
                "BrowserCapability: the browser's default time zone and locale could not be "
                "measured (%r); the fingerprint records none, and no replay of this run will "
                "be accepted",
                exc,
            )
            return None
        if expected is not None:
            differ = [
                f"{name} {expected.value(name)!r} (read before launch) vs {measured.value(name)!r}"
                for name in BrowserDefaults.NAMES
                if expected.value(name) is not None and expected.value(name) != measured.value(name)
            ]
            if differ:
                logger.warning(
                    "BrowserCapability: Chrome falls back to other defaults than this host's OS "
                    "said before launch (%s); a replay's check before launch cannot be trusted "
                    "here — docs/architecture_design.md §4.14",
                    "; ".join(differ),
                )
        if recorded is not None:
            problems = browser_default_problems(recorded, measured)
            if problems:
                raise BrowserCooperationError(problems)
        return measured

    def _recorder_participants(self) -> list[RecorderParticipant]:
        """Who contributes to every new target's preparation, in order: plan, server, sessions."""
        participants: list[RecorderParticipant] = []
        if self._replay_plan is not None:
            participants.append(self._replay_plan)
        if self._replay_server is not None:
            participants.append(self._replay_server)
        if self._archive is not None:
            participants.extend(self._archive.sessions)
        return participants

    # ── raw DOM for the archive (§4.12): parked on the cycle, committed with it ──

    def _raw_capture_sink(self, sid: str) -> Callable[[TargetCapture, float], None]:
        """The DOM resolver's sink for tab *sid*: park each capture on its cycle."""

        def sink(capture: TargetCapture, device_pixel_ratio: float) -> None:
            self._park_raw(sid, capture, device_pixel_ratio, "observation")

        return sink

    def _park_self_capture(
        self, sid: str, capture: TargetCapture, device_pixel_ratio: float
    ) -> None:
        """``browser.dom_raw``'s self-capture of tab *sid*, parked the same way."""
        self._park_raw(sid, capture, device_pixel_ratio, "archive")

    def _park_raw(
        self,
        sid: str,
        capture: TargetCapture,
        device_pixel_ratio: float,
        source: Literal["observation", "archive"],
    ) -> None:
        archive = self._archive
        cache = self._cycle_cache.get(sid)
        if archive is None or cache is None or cache.fired or not archive.wants("raw_dom"):
            return
        cache.raw.append(ParkedRaw(capture, device_pixel_ratio, time.time(), source))

    def _recorder_lost(self, error: RecorderLostError) -> None:
        """The recorder connection died on its own: fail closed, now.

        Pushed by the recorder client's close callback. The capability stops
        being alive, every command still waiting on the control connection
        fails with the same error, and every later observation or action
        raises it (:meth:`_ensure_recording`). A recording never continues
        unrecorded; teardown still kills the browser, which is ours.
        """
        if self._recorder_error is None:
            self._recorder_error = error
        self._alive = False
        logger.error("BrowserCapability: %s", error)
        if self._cdp is not None:
            self._cdp.fail_pending(error)

    def _ensure_recording(self) -> None:
        """Raise the recorder loss, if there was one."""
        if self._recorder_error is not None:
            raise self._recorder_error

    async def teardown(self) -> None:
        """Tear down all scopes, close CDPClient, and release backend session.

        The recorder connection goes first, with its fixed cleanup order
        (§4.14): a recorder that disconnects while still holding paused
        targets or requests wedges the browser, so nothing below may run
        before it has let go.
        """
        self._alive = False

        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            try:
                await recorder.close()
            except Exception as exc:  # pragma: no cover — close() is tolerant itself
                logger.warning("Recorder close failed: %s", exc)
        self._replay_plan = None
        self._browser_defaults = None
        # The replay server after the recorder (which failed every request
        # still held — never continued it): stop answering, drop its state.
        server, self._replay_server = self._replay_server, None
        self._diverged_seen = 0
        if server is not None:
            server.close()
        # The archive sessions next (their scope closes with the scope group).
        archive, self._archive = self._archive, None
        if archive is not None:
            await archive.aclose()

        # Close all CDP→scope bridges BEFORE closing scope_group, so late
        # CDP events don't try to dispatch into a half-torn-down scope.
        for sid, bridge in list(self._bridges.items()):
            try:
                bridge.close()
            except Exception as exc:
                logger.debug("Bridge close failed for %s: %s", sid, exc)
        self._bridges.clear()
        for feed in self._navigation_feeds.values():
            feed.close()
        self._navigation_feeds.clear()

        # Watchdogs own bridges too (PopupsWatchdog, CrashWatchdog). Those
        # live outside ``self._bridges``, so closing only the page-lifecycle
        # bridges above left their callbacks bound to a scope that is about
        # to close.
        for wd_list in list(self._watchdogs.values()):
            self._detach_watchdogs(wd_list)
        self._detach_watchdogs(self._shared_watchdogs)

        if self._scope_group is not None:
            await self._scope_group.close_all()
        self._scopes.clear()
        self._js_scopes.clear()
        self._selector_maps.clear()
        self._watchdogs.clear()
        self._shared_watchdogs.clear()
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
        backend = self.resolved_backend()
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
                logger.debug("Watchdog %s detach failed: %s", type(wd).__name__, exc)

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
        self._ensure_recording()
        sid = self._default_session_id
        if sid is None:
            return ActionResult(success=False, error="No active browser session")

        if sid not in self._scopes:
            return ActionResult(success=False, error=f"Unknown session_id: {sid}")

        impl = self._actions_by_name.get(action.name)
        if impl is None:
            return ActionResult(
                success=False,
                error=f"Unknown action: {action.name}",
            )

        # Invalidate the cache for this tab BEFORE executing the action.
        # The next resolve call will trigger a fresh BrowserStateRequest.
        self._cycle_cache.pop(sid, None)

        try:
            result = await impl.execute(action.params, capability=self)
        except RecorderLostError:
            raise
        except Exception as exc:
            self._ensure_recording()
            logger.error("Action %s failed: %s", action.name, exc, exc_info=True)
            return ActionResult(success=False, error=str(exc))
        # An action that completed while the recorder died acted unrecorded.
        self._ensure_recording()
        return result

    async def begin_observation(self, cycle: int) -> None:
        """Open observation cycle *cycle* — the next read re-captures.

        Recording the number is the whole implementation: a cache entry
        stamped with an older cycle is not served (see
        :meth:`_populate_cycle`), so this costs nothing until a fragment
        is actually asked for, and a tab nobody observes this cycle is not
        scraped at all.

        No CDP call. The one thing that can fail here is the recorder: once
        its connection was lost, no observation of this run is served
        (§4.14), and the environment treats the raise as this capability's
        whole slice failing.
        """
        self._ensure_recording()
        self._observation_cycle = cycle

    async def resolve_fragment(self, fragment_key: str) -> Any:
        """Resolve one observation fragment via the lazy-batch cache.

        The first call of a cycle fires a single
        ``BrowserStateRequestEvent`` carrying the negotiated fragment set;
        every other fragment of the same cycle is served from that one
        capture, however many times each is asked for. The next cycle is
        announced by :meth:`begin_observation`, not guessed at.

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

        # Ensure this tab holds a capture belonging to the current cycle.
        await self._populate_cycle(sid)

        return self._cycle_cache[sid].fragments.get(fragment_key)

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

        **A cycle's failure is recorded and re-raised, not re-attempted.**
        ``Environment.observe`` isolates failures per *fragment*, which is
        right for a capability that captures per fragment and wrong for
        this one: the capture is shared, so after it fails the environment
        asks for the next key of the same slice and — without
        ``cache.error`` — that re-entered the whole retry budget again. A
        three-fragment run paid six DOM extractions, six overlay toggles
        and six warning lines to fail one observation. The budget is spent
        once per cycle now; the rest of the slice gets the same exception
        back, immediately.

        The raise reaches ``Environment.observe``, which decides what it
        costs: fatal if this run declared the fragment required, recorded
        as a ``FragmentFailure`` if it was optional. This method does not
        get to make that call, and must never soften it by serving a
        fragment it could not capture.
        """
        cache = self._cycle_cache.get(sid)
        if cache is None or cache.cycle != self._observation_cycle:
            # No capture for this tab, or one belonging to an observation
            # that is over. Either way the page must be read again.
            cache = _ObserveCycleCache(cycle=self._observation_cycle)
            self._cycle_cache[sid] = cache

        if cache.fired:
            return  # This cycle's capture is already in hand.

        if cache.error is not None:
            # This cycle's capture already failed, and it is one capture
            # for every fragment: re-attempting it per fragment would
            # multiply the cost of a bad step by the number of fragments
            # the agent negotiated. Same exception object, so the
            # environment reports one cause for the whole slice — which is
            # the truth about what happened.
            raise cache.error

        try:
            outcome = await self._capture_once(sid)
        except Exception as exc:
            # Remembered on the cycle, so the rest of this slice is
            # refused rather than re-captured. Anything that reaches here
            # — a lost race, a dead scope, a CDP transport error — costs
            # the same once.
            cache.error = exc
            raise

        cache.fragments.update(outcome.fragments)
        cache.fired = True
        if cache.raw and self._archive is not None:
            # Only a successful cycle's capture reaches the archive (§4.12).
            self._archive.commit_raw(
                cache.raw,
                cycle=cache.cycle,
                tab=self._session_to_target.get(sid, ""),
                viewport=(self.viewport_width, self.viewport_height),
                cleaner=str(
                    getattr(self.dom_cleaner, "cleaner_type", type(self.dom_cleaner).__name__)
                ),
            )
            cache.raw.clear()

    async def _capture_once(self, sid: str) -> _StateEventOutcome:
        """One tab's capture attempt-with-retries, or raise.

        Split out of :meth:`_populate_cycle` so every way a capture can
        fail passes through one ``except`` there and lands on
        ``cache.error``. Inlining it would make the retry ``for/else`` and
        the failure bookkeeping share a block, which is how the second
        failure path gets forgotten.

        Each attempt first waits, up to ``_NAVIGATION_SETTLE_TIMEOUT_S``, for
        the tab's in-flight main-frame navigation to end
        (:class:`~spectragent.core.capabilities.browser.navigation.MainFrameNavigation`,
        §4.14 M33): a click that submits a form returns before its
        navigation, and a capture sent between the request and the commit is
        lost. At the bound it captures anyway; with nothing in flight it does
        not wait at all.

        Then it waits for the page to settle (:meth:`_settle`, ``settle.py``,
        §4.14): the document loaded, then the page quiet for
        ``settle_quiet_ms`` — at most ``settle_max_wait_s``, after which it
        captures anyway. The wait is part of the observation: it runs before
        the one event that captures every negotiated fragment, whichever
        they are, and every fragment of the served capture records it
        (``BrowserCaptureFragment.settle``).
        """
        scope = self._scopes.get(sid)
        if scope is None:
            raise RuntimeError(f"Unknown session_id: {sid}")

        wanted = self.provided_fragments
        cache = self._cycle_cache.get(sid)
        feed = self._navigation_feeds.get(sid)
        outcome: _StateEventOutcome | None = None
        for attempt in range(1, _OBSERVE_MAX_ATTEMPTS + 1):
            # Never capture between a main-frame navigation's request and its
            # commit (§4.14, M33); nothing in flight returns without a wait.
            if feed is not None and not await feed.navigation.wait_settled(
                _NAVIGATION_SETTLE_TIMEOUT_S
            ):
                logger.warning(
                    "observe cycle (sid=%s): attempt %d/%d: the main frame's navigation "
                    "did not end within %gs; capturing anyway",
                    sid,
                    attempt,
                    _OBSERVE_MAX_ATTEMPTS,
                    _NAVIGATION_SETTLE_TIMEOUT_S,
                )
            # The page settles before anything of it is captured (§4.14): the
            # same wait for every fragment, recorded on each of them.
            settle = await self._settle(sid, feed)
            if cache is not None:
                cache.raw.clear()  # a re-fired attempt's capture replaces the last one's
            outcome = await self._fire_state_event(sid, scope, wanted)
            # A recorder lost during the capture: nothing of it is served,
            # and the error says why rather than "no watchdog produced …".
            self._ensure_recording()
            if wanted <= outcome.fragments.keys():
                if not documents_disagree(outcome.stamps):
                    for fragment in outcome.fragments.values():
                        if isinstance(fragment, BrowserCaptureFragment):
                            fragment.settle = settle
                    return outcome
                # The page navigated during the capture: the DOM, the
                # screenshot and the URL describe different documents. Same
                # rule as a missing fragment — never serve it (§4.14, M18).
                logger.warning(
                    "observe cycle (sid=%s): attempt %d/%d captured different "
                    "documents (%s); re-firing",
                    sid,
                    attempt,
                    _OBSERVE_MAX_ATTEMPTS,
                    describe_stamps(outcome.stamps),
                )
                continue
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

        assert outcome is not None  # loop body runs at least once
        if wanted <= outcome.fragments.keys():
            raise RuntimeError(
                f"observe cycle (sid={sid}): the evidence of every attempt described "
                f"different documents after {_OBSERVE_MAX_ATTEMPTS} attempts: "
                f"{describe_stamps(outcome.stamps)}."
            )
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

    async def _settle(self, sid: str, feed: NavigationFeed | None) -> PageSettle | None:
        """Wait for the focused tab's page to settle before a capture (``settle.py``, §4.14).

        ``None`` when the wait is off (``settle_max_wait_s: 0``). Never
        raises for the page's sake: a page whose state cannot be read is
        captured at once, and the record says so (``outcome: unknown``). A
        page the cap ended the wait on is captured as it is.
        """
        if self.settle_max_wait_s <= 0 or self._cdp is None:
            return None
        settle = await wait_for_settle(
            self._cdp,
            session_id=sid,
            frame_id=self._main_frame_ids.get(sid),
            navigation=feed.navigation if feed is not None else None,
            load=feed.load if feed is not None else None,
            quiet_ms=self.settle_quiet_ms,
            max_wait_s=self.settle_max_wait_s,
        )
        if settle.capped:
            logger.info(
                "observe cycle (sid=%s): the page did not settle within %gs (waiting for %s); "
                "capturing it as it is",
                sid,
                self.settle_max_wait_s,
                ", ".join(settle.waiting_for),
            )
        return settle

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
            if result_obj.is_success and isinstance(result_obj.result, BrowserStateSummary):
                summary: BrowserStateSummary = result_obj.result
                outcome.fragments.update(summary.fragments)
                if summary.document is not None:
                    # By handler; a name two connections share keeps both stamps.
                    key = result_obj.handler_name
                    if key in outcome.stamps:
                        key = f"{key}#{result_obj.connection_id}"
                    outcome.stamps[key] = summary.document
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
        """Open a new tab at *url* and return its CDP session_id.

        The tab is always created at ``about:blank`` and navigated afterwards
        (§4.14): ``Target.createTarget`` with the URL itself starts the
        navigation before the recorder connection attaches, so that first
        request is neither paused, recorded nor pinned (measured, Chrome 155).
        With a recorder connection the navigation waits until the recorder
        has prepared the new tab (:meth:`RecorderConnection.wait_prepared`).
        """
        if self._cdp is None:
            raise RuntimeError("BrowserCapability not set up")
        result = await self._cdp.send("Target.createTarget", {"url": "about:blank"})
        target_id = (result or {}).get("targetId", "")
        if not target_id:
            raise RuntimeError("Target.createTarget returned no targetId")
        try:
            sid = await self._attach_tab(target_id, "about:blank")
        except BaseException:
            # This method created the target, so a failed attach must close
            # it.  _attach_tab also serves page-owned targets discovered by
            # switch_tab, where closing the page would be wrong.
            try:
                await self._cdp.send("Target.closeTarget", {"targetId": target_id})
            except Exception as exc:
                logger.warning(
                    "Failed to close newly created tab %s after attach error: %s", target_id, exc
                )
            raise
        if url == "about:blank":
            return sid
        recorder = self._recorder
        if recorder is not None and not await recorder.wait_prepared(
            target_id, _NEW_TAB_PREPARED_TIMEOUT_S
        ):
            logger.warning(
                "create_tab: the recorder connection did not prepare tab %s within %gs; "
                "navigating it to %s anyway",
                target_id,
                _NEW_TAB_PREPARED_TIMEOUT_S,
                url,
            )
        reply = await self._cdp.send("Page.navigate", {"url": url}, session_id=sid)
        error = (reply or {}).get("errorText")
        if error:
            raise RuntimeError(f"create_tab: navigating the new tab to {url!r} failed: {error}")
        return sid

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
                raise ValueError("switch_tab: no other tab is open — only the current tab exists.")
            target_id = candidates[-1]
        elif target_id not in page_targets:
            raise ValueError(f"switch_tab: unknown tab {target_id!r}. Open tabs: {page_targets}")

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
        feed = self._navigation_feeds.pop(session_id, None)
        if feed is not None:
            feed.close()

        # Same for the bridges the tab's own watchdogs opened.
        self._detach_watchdogs(self._watchdogs.get(target_id, []))

        # A shared watchdog may hold session ownership for this tab and its
        # child targets. Revoke that authority before closing either scope.
        for wd in self._shared_watchdogs:
            wd.unbind_tab(session_id)

        if self._scope_group is not None:
            await self._scope_group.close_scope(target_id)
            await self._scope_group.close_scope(f"{target_id}:js")

        self._scopes.pop(session_id, None)
        self._js_scopes.pop(session_id, None)
        self._selector_maps.pop(session_id, None)
        self._watchdogs.pop(target_id, None)
        self._target_to_session.pop(target_id, None)
        self._session_to_target.pop(session_id, None)
        self._cycle_cache.pop(session_id, None)
        self._main_frame_ids.pop(session_id, None)

        if self._cdp is not None:
            try:
                await self._cdp.send("Target.closeTarget", {"targetId": target_id})
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
        if target_id in self._target_to_session:
            raise ValueError(f"Tab {target_id!r} is already attached")

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

        created_scope_ids: list[str] = []
        try:
            return await self._finish_tab_attach(target_id, url, cdp_session_id, created_scope_ids)
        except BaseException:
            # An attach may fail after the scopes and CDP bridges exist.  All
            # provisional resources must go before a retry can adopt the tab.
            await self._rollback_tab_attach(target_id, cdp_session_id, created_scope_ids)
            raise

    async def _finish_tab_attach(
        self,
        target_id: str,
        url: str,
        cdp_session_id: str,
        created_scope_ids: list[str],
    ) -> str:
        """Finish an attached CDP session; caller rolls back on any failure."""
        assert self._cdp is not None and self._scope_group is not None

        # 2. Enable the Page domain for this session. Without it,
        # loadEventFired and the navigation events never fire. Lifecycle
        # events are enabled in step 4b, once the main frame's feed listens.
        try:
            await self._cdp.send("Page.enable", {}, session_id=cdp_session_id)
        except Exception as exc:
            logger.warning(
                "Page.enable failed on %s: %s — lifecycle-aware navigation will "
                "degrade to fire-and-forget",
                cdp_session_id,
                exc,
            )

        # 3. Apply the configured viewport to this tab.
        await self._apply_viewport_override(cdp_session_id)

        # 4. Capture main frame id (best-effort; used by _on_navigate to
        # filter sub-frame lifecycle events).
        main_frame_id: str | None = None
        try:
            tree_result = await self._cdp.send(
                "Page.getFrameTree",
                {},
                session_id=cdp_session_id,
            )
            if tree_result:
                main_frame_id = tree_result.get("frameTree", {}).get("frame", {}).get("id")
        except Exception as exc:
            logger.debug(
                "Page.getFrameTree failed on %s: %s",
                cdp_session_id,
                exc,
            )
        if main_frame_id is not None:
            self._main_frame_ids[cdp_session_id] = main_frame_id
            # Follow the main frame's navigations and its document's load on
            # this connection, so an observation never captures between a
            # navigation's request and its commit, nor before the page settled
            # (§4.14). Unknown main frame: nothing to follow.
            self._navigation_feeds[cdp_session_id] = NavigationFeed(
                self._cdp,
                cdp_session_id,
                MainFrameNavigation(main_frame_id),
                MainFrameLoad(main_frame_id),
            )

        # 4b. Lifecycle events, now that the feed listens: Chrome reports the
        # current document's events again when they are enabled, so a tab
        # adopted mid-load is known to the settle wait. We do this before
        # creating watchdogs so a subsequent Page.navigate's lifecycle is
        # observable.
        try:
            await self._cdp.send(
                "Page.setLifecycleEventsEnabled",
                {"enabled": True},
                session_id=cdp_session_id,
            )
        except Exception as exc:
            logger.warning(
                "Page.setLifecycleEventsEnabled failed on %s: %s — lifecycle-aware "
                "navigation will degrade to fire-and-forget, and the settle wait will "
                "not wait for the document's load",
                cdp_session_id,
                exc,
            )

        # 5. Two independent processing domains for this tab.  DOM and
        # action watchdogs use the observation scope.  A shared JS executor
        # listens to it but runs its handler in the separate JS queue.
        scope = await self._scope_group.create_scope(
            target_id,
            target_id=target_id,
            url=url,
            cdp_session_id=cdp_session_id,
        )
        created_scope_ids.append(target_id)
        js_scope = await self._scope_group.create_scope(
            f"{target_id}:js",
            target_id=target_id,
            url=url,
            cdp_session_id=cdp_session_id,
        )
        created_scope_ids.append(f"{target_id}:js")
        self._js_scopes[cdp_session_id] = js_scope

        # 6. CDP → scope bridge for Page-domain lifecycle events.
        bridge = CDPEventBridge(self._cdp, scope, session_id=cdp_session_id)
        self._bridges[cdp_session_id] = bridge
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
        # 7. Per-tab watchdogs.
        watchdogs = self._create_watchdogs_for_scope(
            level="tab",
            cdp_session_id=cdp_session_id,
        )
        self._watchdogs[target_id] = watchdogs
        for wd in watchdogs:
            wd.attach(scope, session_id=cdp_session_id)

        # Shared watchdogs may fan in events from many tabs.  The
        # binding hook is generic: each watchdog decides whether it needs
        # either scope, and closing a scope disconnects its connections.
        for wd in self._shared_watchdogs:
            wd.bind_tab_scopes(scope, js_scope, session_id=cdp_session_id)

        # 8. Register mappings.
        self._scopes[cdp_session_id] = scope
        self._selector_maps[cdp_session_id] = {}
        self._target_to_session[target_id] = cdp_session_id
        self._session_to_target[cdp_session_id] = target_id

        return cdp_session_id

    async def _rollback_tab_attach(
        self, target_id: str, cdp_session_id: str, created_scope_ids: list[str]
    ) -> None:
        """Release every provisional resource from one failed tab attach."""
        bridge = self._bridges.pop(cdp_session_id, None)
        if bridge is not None:
            try:
                bridge.close()
            except Exception as exc:
                logger.debug("Attach rollback: bridge close failed for %s: %s", cdp_session_id, exc)
        feed = self._navigation_feeds.pop(cdp_session_id, None)
        if feed is not None:
            feed.close()

        for wd in self._watchdogs.pop(target_id, []):
            try:
                wd.detach()
            except Exception as exc:
                logger.debug("Attach rollback: watchdog detach failed for %s: %s", target_id, exc)
        for wd in self._shared_watchdogs:
            try:
                wd.unbind_tab(cdp_session_id)
            except Exception as exc:
                logger.debug("Attach rollback: shared unbind failed for %s: %s", target_id, exc)

        if self._scope_group is not None:
            for scope_id in created_scope_ids:
                try:
                    await self._scope_group.close_scope(scope_id)
                except Exception as exc:
                    logger.debug("Attach rollback: scope close failed for %s: %s", scope_id, exc)

        self._scopes.pop(cdp_session_id, None)
        self._js_scopes.pop(cdp_session_id, None)
        self._selector_maps.pop(cdp_session_id, None)
        self._target_to_session.pop(target_id, None)
        self._session_to_target.pop(cdp_session_id, None)
        self._cycle_cache.pop(cdp_session_id, None)
        self._main_frame_ids.pop(cdp_session_id, None)

        if self._cdp is not None:
            try:
                await self._cdp.send("Target.detachFromTarget", {"sessionId": cdp_session_id})
            except Exception as exc:
                logger.debug("Attach rollback: CDP detach failed for %s: %s", cdp_session_id, exc)

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
            js_scope=self._js_scopes.get(cdp_session_id) if cdp_session_id is not None else None,
            debug_highlight=self.debug_highlight,
            mouse_trajectory=self.mouse_trajectory,
            typing_rhythm=self.typing_rhythm,
            dom_cleaner=self.dom_cleaner,
            dom_serializer=self.dom_serializer,
            dom_budget=self.dom_budget,
            screenshot_format=self.screenshot_format,
            screenshot_quality=self.screenshot_quality,
            selector_map_getter=_selector_map_getter,
            selector_map_setter=_selector_map_setter,
            main_frame_id_getter=_main_frame_id_getter,
            tabs_getter=self._get_tabs_safe,
            archive=self._recorder.channel if self._recorder is not None else None,
            # A real sink only when setup opened the raw-DOM channel (§4.12);
            # otherwise the default no-op, and the resolver never knows.
            **(
                {"raw_capture_sink": self._raw_capture_sink(cdp_session_id)}
                if cdp_session_id is not None
                and self._archive is not None
                and "raw_dom" in self._archive.needs
                else {}
            ),
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
            if wd_cls.level == level and wd_cls.should_install(ctx)
        ]

    def _attach_browser_level_watchdogs(self, scope: EventScope) -> None:
        """Attach browser-level watchdogs (crash) to the global scope."""
        watchdogs = self._create_watchdogs_for_scope(level="browser")
        for wd in watchdogs:
            wd.attach(scope, session_id=None)
        self._watchdogs[_GLOBAL_SCOPE_ID] = watchdogs
