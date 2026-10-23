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
  focused tab lives in ``_default_session_id``. The agent changes it with
  :meth:`BrowserCapability.switch_tab`; the capability changes it itself
  at the start of an observation — to a tab the page opened during the
  agent's actions (``new_tabs: follow``, the default, as a real browser
  brings a new tab to the front), or off a focused tab that closed itself
  (``tabs.py``, §4.14 "页面新开的标签页"). It is *not* a parameter of
  ``execute_action`` / ``resolve_fragment``, and nothing above this
  package knows it exists — a CDP tab handle is a browser concept, and
  threading it through the framework's Action / Environment / Runtime /
  Loop signatures gave every other domain a mandatory argument it could
  neither produce nor use. The per-cycle cache is keyed by that same id.
  Under ``background_tabs: freeze`` the tab the focus leaves is frozen, and
  set running again before the focus returns to it (§4.14 "离开焦点的 tab").

See docs/architecture_design.md §4.9 and docs/execution_dag.md M13.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal, cast

from agent_cdp import CDPCommandProtocol, CDPEventBridge, EventScope, Participation, ScopeGroup
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase, ActionRegistrar
from spectragent.core.actions.base import BaseAction
from spectragent.core.archive import ArchiveBatch, ArchiverBase
from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities._registrar import CapabilityBase, negotiated_fragment_keys
from spectragent.core.capabilities.base import CaptureComparison, PreparedSetupContext
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
    FramesArchiver,
    NetworkArchiver,
    ParkedRaw,
    ReplayLogArchiver,
    StorageArchiver,
)
from spectragent.core.capabilities.browser.archive.encoding import canonical_json
from spectragent.core.capabilities.browser.backends.base import (
    BaseBrowserProvider,
    SharedBrowserProvider,
)
from spectragent.core.capabilities.browser.backends.browser_defaults import (
    BrowserDefaults,
    measure_browser_defaults,
)
from spectragent.core.capabilities.browser.backends.capabilities import LAUNCH_ARGS
from spectragent.core.capabilities.browser.capture_check import compare_capture
from spectragent.core.capabilities.browser.cdp_client import CDPClient, transport_options
from spectragent.core.capabilities.browser.client_hints import ClientHints, measure_client_hints
from spectragent.core.capabilities.browser.color_scheme import ColorScheme, measure_color_scheme
from spectragent.core.capabilities.browser.context_client import BrowserContextClient
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
from spectragent.core.capabilities.browser.dom.capture import FrameTargets
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
from spectragent.core.capabilities.browser.dom.models import (
    DOMElementNode,
    DOMSelectorMap,
    TargetCapture,
)
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
from spectragent.core.capabilities.browser.media import (
    PendingAlignment,
    align_media,
    alignment_raised,
)
from spectragent.core.capabilities.browser.navigation import (
    MainFrameLoad,
    MainFrameNavigation,
    NavigationFeed,
)
from spectragent.core.capabilities.browser.observations import BrowserCaptureFragment
from spectragent.core.capabilities.browser.platform import InputPlatform, measure_input_platform
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
    client_hint_problems,
    consistency_notes,
    consistency_problems,
    input_platform_problems,
    replay_color_scheme,
    replay_t0,
    version_warnings,
)
from spectragent.core.capabilities.browser.replay.server import ReplayContext, ReplayServer
from spectragent.core.capabilities.browser.restoration import (
    measure,
    media_measure,
    retarget_action,
    screenshot_fragment,
)
from spectragent.core.capabilities.browser.sessions import TabSessions
from spectragent.core.capabilities.browser.settle import (
    DEFAULT_SETTLE_MAX_WAIT_S,
    DEFAULT_SETTLE_QUIET_MS,
    PageSettle,
    wait_for_settle,
)
from spectragent.core.capabilities.browser.shared_browser import BrowserContextLease
from spectragent.core.capabilities.browser.start_page import start_page_problem
from spectragent.core.capabilities.browser.storage_seed import (
    StorageSeed,
    capture_profile_initial,
    hydrate_storage,
    read_initial_seed,
)
from spectragent.core.capabilities.browser.tabs import (
    FocusMove,
    FocusMoveReason,
    FollowWindow,
    PageTab,
    TabRegistry,
    TabSelection,
    tabs_after_close,
    tabs_to_follow,
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
    DocumentStamp,
    DOMContentLoadedEvent,
    LifecycleEvent,
    PageLoadEvent,
    describe_stamps,
    documents_disagree,
)
from spectragent.schemas._types import UNLIMITED, Unlimited, limit_value
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.browser_storage import BrowserStorageState
from spectragent.schemas.provider_session import ProviderSession
from spectragent.schemas.restoration import MediaAlignment, RestorationEvidence, Retarget
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

#: What one capture attempt leaves, in seconds, for the probes whose limits are
#: YAML knobs — ``capture_state_timeout_s`` once and ``media_state_timeout_s``
#: twice (before and after the screenshot). A tab captures its fragments one
#: after another inside one 30 s ``BrowserStateRequestEvent``; the fixed bounds
#: take 26 s of it (the DOM capture 10 s, the screenshot command 10 s, three
#: small reads 2 s each; §4.14 "命令上限"). A sum at or past this room would let
#: a slow probe fail the whole capture, which a probe must never do, so the
#: capability refuses it (``test_browser_observation_bounds.py`` derives it).
CAPTURE_PROBE_ROOM_S = 4.0

# How long each command measuring the browser's defaults right after launch
# may take (§4.14): the first tab's renderer may still be starting.
_BROWSER_DEFAULTS_TIMEOUT_S = 5.0

# How long reading the browser's own client hints right after launch may take
# (§4.14, ``client_hints.py``): a temporary tab, its renderer started for it.
_CLIENT_HINTS_TIMEOUT_S = 5.0

# The initial page's media theme, before any target receives an override.
_COLOR_SCHEME_TIMEOUT_S = 5.0

# How long ``create_tab`` waits for the recorder connection to prepare the
# tab it created before navigating it (§4.14). An ``about:blank`` tab is
# prepared in tens of milliseconds.
_NEW_TAB_PREPARED_TIMEOUT_S = 10.0

# How long moving the focus to a tab the page opened may take to adopt it
# (§4.14 "页面新开的标签页"). A tab whose first navigation is still in flight
# answers its first page-side command (``Page.enable``) only once that
# navigation commits — measured, Chrome 155: a document answered after 2 s,
# ``Page.enable`` after 2.0 s, whether or not the tab can reach its opener —
# so adopting it waits for its first commit, under the bound an observation
# already waits for a main-frame navigation.
_FOLLOW_ADOPT_TIMEOUT_S = _NAVIGATION_SETTLE_TIMEOUT_S

# How many times one observation moves the focus before it captures where it
# is: a page that opens a tab every time it is followed must not hold the
# observation forever.
_FOCUS_MOVES_PER_OBSERVATION = 3

# How long the tab list read that closes an observation's follow window may
# take (``Target.getTargets``, answered by the browser itself).
_TAB_LIST_TIMEOUT_S = 2.0

# How long freezing a tab the focus left, or setting it running again, may take
# (``Page.setWebLifecycleState``, ``background_tabs: freeze``, §4.14 "离开焦点的
# tab"). The browser process answers it, not the page: measured, Chrome 155,
# about 1 ms, also while the tab's main thread was inside a 4 s task. The bound
# only catches a connection that stopped answering.
_LIFECYCLE_TIMEOUT_S = 2.0


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
    active_request_id: str | None = None


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

    An explicit constructor initializes per-run browser state and accepts
    optional configured actions. conscribe reflects both its parameters and
    annotation-only strategy receptors; dataclass receptors are also supported.
    """

    __registry_key__: ClassVar[str] = "browser"
    __wiring__: ClassVar[dict[str, Any]] = {
        # Observation provides is *derived*, never listed. It is the union
        # of ``provides`` over every registered browser watchdog, so a new
        # fragment cannot drift out of sync with the class that resolves
        # it — and adding one does not edit this file. Evaluated at class
        # creation; ``watchdogs`` is imported above for exactly that.
        "observation": {
            "registry": "observation",
            "keys": sorted(fragments_provided_by(browser_watchdog_classes().values())),
            "declare_only": True,
        },
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
        "archiver": {
            "registry": "archiver",
            "keys": [
                "browser.dom_raw",
                "browser.frames",
                "browser.network",
                "browser.replay_log",
                "browser.storage",
            ],
            "declare_only": True,
        },
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
    screenshot_cursor: bool
    screenshot_cursor_size: int
    dom_max_elements: int | Unlimited
    dom_max_serialized_chars: int | Unlimited
    dom_interactive_only: bool
    dom_viewport_only: bool
    settle_quiet_ms: int
    settle_max_wait_s: float
    action_effect_timeout_s: float
    type_focus_wait_s: float
    capture_state_timeout_s: float
    media_state_timeout_s: float
    retarget_tolerance_px: float
    new_tabs: Literal["follow", "stay"]
    background_tabs: Literal["run", "freeze"]
    expected_platform: InputPlatform | None

    # ── internal state, never config (assigned in __init__) ──────────
    # Declared here as type-only annotations so both pyright and the
    # conscribe stub writer see them; the values are set in ``__init__``.
    _actions_by_name: dict[str, ActionBase]
    _cdp: CDPClient | BrowserContextClient | None
    _session: ProviderSession | None
    _context_lease: BrowserContextLease | None
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
    #: The media alignment a restorer requested for the next observation, and
    #: what its capture did, until the next ``verify_restoration`` takes it
    #: (``align_next_observation``, §4.14).
    _media_alignment: PendingAlignment | None
    #: Each tab's out-of-process frames as CDP sessions, by tab CDP session:
    #: one registry per tab, shared by the DOM resolver (when the profile
    #: captures cross-site frames) and the media reads and alignments
    #: (``media.py``, read whatever the profile). Never two per tab: Chrome
    #: reports a session's existing frames only to the first
    #: ``Target.setAutoAttach`` on it, so a second registry would see none.
    _frame_targets: dict[str, FrameTargets]
    #: The time zone and locale a frame no override reaches falls back to,
    #: measured right after launch when archivers are bound (§4.14).
    _browser_defaults: BrowserDefaults | None
    _input_platform: InputPlatform | None
    #: Every tab, in the order they opened, from the control connection's
    #: target discovery (``tabs.py``) — between setup and teardown, when the
    #: browser accepted the discovery.
    _tabs: TabRegistry | None
    #: The discovery callbacks registered on the control connection.
    _tab_callbacks: list[tuple[str, Callable[..., None]]]
    #: The agent's actions since the last observation (opened by the first of
    #: them, closed by the next capture): a tab they make the focused page open
    #: is followed (``new_tabs: follow``).
    _follow_window: FollowWindow | None
    #: The observation cycle whose focus has been settled, and what it did.
    _focus_cycle: int | None
    _focus_from: str | None
    _focus_moves: int
    _focus_moved: FocusMove | None
    #: Tabs the focus could not be moved to (adoption failed); never tried again.
    _unfollowable: set[str]
    #: Tabs frozen when the focus left them (``background_tabs: freeze``), by
    #: target id; each is set running again before the focus returns to it.
    _frozen: set[str]

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
        expected_platform: Annotated[
            InputPlatform | None,
            PydanticField(
                description="Expected actual browser input platform. None accepts any measured supported platform; a mismatch is refused, never emulated."
            ),
        ] = None,
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
        screenshot_cursor: Annotated[
            bool,
            PydanticField(
                description="Show a highlighted cursor in the model image view; clean screenshot evidence is unchanged."
            ),
        ] = False,
        screenshot_cursor_size: Annotated[
            int,
            PydanticField(ge=16, le=96, description="Model image cursor height in CSS pixels."),
        ] = 32,
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
                    "measured from the moment the wait reaches the page, with "
                    "something visible in the viewport (text, an image, a "
                    "video, a canvas, an SVG drawing, a frame or a form control). "
                    "The same wait serves every browser fragment (DOM, "
                    "screenshot, metadata), in recording and in replay alike. "
                    "0 skips the quiet window and waits for the load and for "
                    "visible content only. Default 500."
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
                    "that never goes quiet (a carousel, a video, a poll) or "
                    "whose viewport stays empty costs this much on each step. "
                    "0 turns the wait off: the observation captures at once. "
                    "Each fragment records how long its capture waited and "
                    "whether this cap ended the wait. Default 5."
                ),
            ),
        ] = DEFAULT_SETTLE_MAX_WAIT_S,
        action_effect_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0,
                le=10,
                description=(
                    "The most time, in seconds, one action may spend measuring its own "
                    "effect (ActionResult.effect): the point it pressed and the element "
                    "there, the focused control, whether a field's value changed, scroll "
                    "positions, the URL and load state around a navigation. Only the "
                    "probes count against it, not the action itself; waiting for a wheel "
                    "to land counts, and so does type's wait for focus to land on a text "
                    "field (type_focus_wait_s). A fact whose probe runs out of it is "
                    "recorded as unknown, and an unknown fact never decides the action's "
                    "success or error. One measured fact does: type and type_coordinate "
                    "fail, sending no key, when the focus reading found that no text "
                    "field has focus (a reading that runs out types as asked). Measured "
                    "in headless Chrome: a click, a type or a key "
                    "press spends 2-4 ms (at most ~26 ms), a navigation ~5 ms, a scroll "
                    "40-80 ms waiting for the wheel to land (the first wheel on a fresh page "
                    "was seen landing after ~0.9 s). Default 1, which covers that first "
                    "wheel and bounds a scroll that never lands: a scroll that could still "
                    "move but was not seen moving by then reports its position after as "
                    "unknown, not as zero."
                ),
            ),
        ] = 1.0,
        type_focus_wait_s: Annotated[
            float,
            PydanticField(
                ge=0,
                le=5,
                description=(
                    "How long, in seconds, type and type_coordinate wait for focus to "
                    "land on a text field after the click or focus that should have put "
                    "it there, before they fail without sending a key: a widget may "
                    "focus its input on a timer or after an opening animation. Spent "
                    "only when no text field has focus yet (a field that already has "
                    "focus is typed into at once), read again every 50 ms. It is part "
                    "of action_effect_timeout_s and must be less than it, so the "
                    "probes after the wait keep the rest; a larger value is refused, "
                    "naming both fields. Default 0.5, at most 5."
                ),
            ),
        ] = 0.5,
        capture_state_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0,
                le=3,
                description=(
                    "The most time, in seconds, the browser.capture_state fragment may "
                    "spend on its in-page probe (the visible images that failed to load) "
                    "in each observation. A probe that runs out is recorded as unknown, "
                    "never as zero; the rest of the observation is unaffected. Settle and "
                    "tab changes are read from the same capture and cost nothing here. "
                    "The probe itself takes a few milliseconds (headless Chrome: about 1 ms "
                    "on simple pages, at most 10 ms at the 95th percentile with 5,000 images "
                    "or on image-heavy sites); it runs on the page's main thread, so this "
                    "limit is reached only when page scripts hold that thread, and then it "
                    "is what the observation pays. Default 1, at most 3. Together with the two "
                    "media reads it must leave the capture its budget: this plus twice "
                    "media_state_timeout_s must stay under 4 s, the part of a capture's 30 s "
                    "the fixed bounds of the DOM capture, the screenshot and three small "
                    "reads (26 s) leave; a larger sum is refused, naming both fields."
                ),
            ),
        ] = 1.0,
        media_state_timeout_s: Annotated[
            float,
            PydanticField(
                gt=0,
                le=1.5,
                description=(
                    "The most time, in seconds, each of the two reads of the page's media "
                    "playback state may take: one just before and one just after every "
                    "observation screenshot (every <video> and <audio> element's exact "
                    "position, paused state, duration, frame, order and place, kept with "
                    "the screenshot). A read that runs out is recorded as unknown; the "
                    "screenshot never fails for it, and nothing on the page is changed. The "
                    "read runs on the page's main thread and takes a few milliseconds, so "
                    "this limit is reached only when page scripts hold that thread. In a "
                    "branch run's restored prefix the same limit bounds the read the media "
                    "alignment starts from. Default 1, at most 1.5. It is spent twice per "
                    "capture, so twice this plus capture_state_timeout_s must stay under 4 s, "
                    "the part of a capture's 30 s the fixed bounds of the DOM capture, the "
                    "screenshot and three small reads (26 s) leave; a larger sum is refused, "
                    "naming both fields."
                ),
            ),
        ] = 1.0,
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
        new_tabs: Annotated[
            Literal["follow", "stay"],
            PydanticField(
                description=(
                    "What the agent sees when the page it acts on opens a new "
                    "tab during its actions (a target=_blank link, window.open, "
                    "a form that opens in a new tab). 'follow' (the default): "
                    "the new tab comes to the front, as in a real browser, and "
                    "the next observation shows it; a tab Chrome reports as "
                    "opened without the user's activation (a popup a page opens "
                    "as it loads or from a timer), which a real browser's popup "
                    "blocker stops, is not followed. 'stay': the agent stays on "
                    "its tab; the new one is listed in browser_meta's tabs and "
                    "reachable with browser.switch_tab. Either way, when the tab "
                    "the agent is on closes itself, the agent goes to the tab "
                    "that opened it, or to the most recently opened tab still "
                    "open. Recording, replay and branching apply the same rule, "
                    "so all three use one value."
                ),
            ),
        ] = "follow",
        background_tabs: Annotated[
            Literal["run", "freeze"],
            PydanticField(
                description=(
                    "What becomes of a tab the agent's focus leaves: the tab a followed new "
                    "tab came from, the tab browser.switch_tab left. 'run' (the default): it "
                    "keeps running in the background, as in a real browser. 'freeze': it is "
                    "frozen (Chrome's page lifecycle freeze), so none of its timers, scripts, "
                    "storage events or network callbacks run, until the focus comes back to "
                    "it; it is set running again before it is shown. This stops tabs of one "
                    "site that keep each other in step through localStorage from rewriting "
                    "each other without end (iqiyi.com with five tabs open: every renderer "
                    "pegged and the observation's screenshot got no reply). What it changes "
                    "for the page: media that was playing in the tab is paused when it is "
                    "frozen and is still paused when the agent comes back (measured, Chrome "
                    "155). A tab the focus was never on (a popup that was not followed, a "
                    "new tab under new_tabs: stay) is not frozen. Recording, replay and "
                    "branching must use one value: it is part of the environment "
                    "fingerprint, and an offline replay refuses a recording made with the "
                    "other."
                ),
            ),
        ] = "run",
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
        self.expected_platform = expected_platform
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.debug_highlight = debug_highlight
        self.screenshot_format = screenshot_format
        self.screenshot_quality = screenshot_quality
        self.screenshot_cursor = screenshot_cursor
        self.screenshot_cursor_size = screenshot_cursor_size
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
        self.action_effect_timeout_s = action_effect_timeout_s
        if type_focus_wait_s >= action_effect_timeout_s:
            raise ValueError(
                f"type_focus_wait_s ({type_focus_wait_s:g} s) is not less than "
                f"action_effect_timeout_s ({action_effect_timeout_s:g} s): the wait for "
                "focus is spent from that budget, and the probes after it (whether the "
                "field changed, the focus afterwards) need the rest. Lower the wait or "
                "raise the budget."
            )
        self.type_focus_wait_s = type_focus_wait_s
        probes = capture_state_timeout_s + 2 * media_state_timeout_s
        if probes >= CAPTURE_PROBE_ROOM_S:
            raise ValueError(
                f"capture_state_timeout_s ({capture_state_timeout_s:g} s) plus twice "
                f"media_state_timeout_s ({media_state_timeout_s:g} s) is {probes:g} s, not "
                f"under the {CAPTURE_PROBE_ROOM_S:g} s one capture leaves for them: the DOM "
                "capture, the screenshot and three small reads may take 26 s of its 30 s, "
                "so a probe running out would fail the whole capture. Lower one of them."
            )
        self.capture_state_timeout_s = capture_state_timeout_s
        self.media_state_timeout_s = media_state_timeout_s
        self.retarget_tolerance_px = retarget_tolerance_px
        self.new_tabs = new_tabs
        self.background_tabs = background_tabs

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
        self._context_lease = None
        self._scope_group = None
        self._scopes = {}
        self._js_scopes = {}
        self._bridges = {}
        self._main_frame_ids = {}
        self._navigation_feeds = {}
        self._selector_maps = {}
        self._target_to_session = {}
        self._session_to_target = {}
        self._tab_sessions = TabSessions()
        self._watchdogs = {}
        self._shared_watchdogs = []
        self._global_scope = None
        self._default_session_id = None
        self._alive = False
        self._cycle_cache = {}
        self._observation_cycle = 0
        self._initial_storage: StorageSeed | None = None
        self._storage_tabs: dict[str, str] = {}
        self._injected_client = None
        self._cdp_factory = None
        self._recorder = None
        self._recorder_error = None
        self._replay_plan = None
        self._replay_server = None
        self._archivers = []
        self._archive = None
        self._diverged_seen = 0
        self._media_alignment = None
        self._frame_targets = {}
        self._browser_defaults = None
        self._input_platform = None
        self._tabs = None
        self._tab_callbacks = []
        self._follow_window = None
        self._focus_cycle = None
        self._focus_from = None
        self._focus_moves = 0
        self._focus_moved = None
        self._unfollowable = set()
        self._frozen = set()

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

    @property
    def provided_actions(self) -> frozenset[str]:
        """The actions this configured browser can actually route."""
        return frozenset(self._actions_by_name)

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
    def action_scope(self) -> EventScope:
        """The focused tab's event endpoint, used by its browser actions."""
        return self._scopes[self.default_session_id]

    @property
    def selector_map(self) -> Mapping[int, DOMElementNode]:
        """Read-only indexed elements from the focused tab's latest observation."""
        return MappingProxyType(self._selector_maps.get(self.default_session_id, {}))

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
    def cdp(self) -> CDPCommandProtocol:
        if self._cdp is None:
            raise RuntimeError("BrowserCapability not set up — call setup() first")
        return self._cdp

    @property
    def browser_context_id(self) -> str | None:
        """This run's partition, or None for a dedicated/default-context browser."""
        return self._context_lease.context_id if self._context_lease is not None else None

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
        order, and the fingerprint run record. A session that cannot collect
        is named in the batch's ``gaps``; the others' evidence is handed
        over. Never set up, or nothing bound: an empty batch.
        """
        archive = self._archive
        if archive is None or not self._archivers:
            return ArchiveBatch()
        self._ensure_recording()
        server = self._replay_server
        return await archive.collect(
            plan_fingerprint=self._replay_plan.fingerprint() if self._replay_plan else {},
            session_metadata=(
                self._context_lease.metadata
                if self._context_lease is not None
                else self._session.metadata
                if self._session is not None
                else None
            ),
            fresh_profile=self._fresh_profile(),
            background_tabs=self.background_tabs,
            clock=time.time,
            network_fingerprint=(
                server.fingerprint() if server is not None else self.network_source.fingerprint()
            ),
            browser_defaults=self._browser_defaults,
            input_platform=self._input_platform,
            initial_storage=self._initial_storage.fingerprint() if self._initial_storage else None,
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
        (:func:`~spectragent.core.capabilities.browser.restoration.measure`),
        and so are the two screenshots' media playback states;
        ``document_misses`` is this run's replay server's count of main-frame
        document misses since the previous call (``None`` when the network is
        live), and the media alignment done since the previous call is taken
        with it — both reported once, so a restorer measures each restored
        observation exactly once (the ``RestorationSupport`` contract). The
        URL comparison leaves out the query parameters the replay's frozen
        rule set declares in its page-URL rules (none when the network is
        live). Thresholds are the restorer's.
        """
        alignment: list[MediaAlignment] | None = None
        alignment_detail = ""
        # The alignment belongs to this restored observation only: taken here,
        # whether a capture acted on it or not (``align_next_observation``).
        pending, self._media_alignment = self._media_alignment, None
        if pending is not None:
            report = pending.take()
            alignment, alignment_detail = list(report.records), report.detail
        return measure(
            recorded,
            restored,
            capability_id=self.capability_id,
            document_misses=self._document_misses_since_last_check(),
            media=media_measure(
                recorded,
                restored,
                tolerance_px=self.retarget_tolerance_px,
                alignment=alignment,
                alignment_detail=alignment_detail,
            ),
            page_urls=self._replay_server.page_urls if self._replay_server is not None else (),
        )

    # ── Checkpoint restoration: media alignment (RestorationAlignment, §4.9 / §4.14) ──

    async def align_next_observation(
        self, recorded: Any, *, media_seek_timeout_s: float, media_end_lead_s: float
    ) -> None:
        """Align the next observation's media to *recorded*'s, inside its capture.

        Only the request is kept here — the page is not touched. The next
        observation cycle's capture aligns after its settle wait and before
        its screenshot (:meth:`_capture_once`, ``media.py::align_media``),
        and the next :meth:`verify_restoration` reports what it did. The
        request belongs to that one cycle: a later cycle never aligns.
        """
        screenshot = screenshot_fragment(recorded)
        self._media_alignment = PendingAlignment(
            recorded=screenshot.media if screenshot is not None else None,
            seek_timeout_s=media_seek_timeout_s,
            end_lead_s=media_end_lead_s,
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
            screenshot_cursor=self.screenshot_cursor,
            screenshot_cursor_size=self.screenshot_cursor_size,
            screenshot_quality=self.screenshot_quality,
        )

    # ── What setup would refuse, refused at prepare() (SetupCheck, §4.14) ──

    def prepare_setup_context(self, context: Mapping[str, Any]) -> PreparedSetupContext:
        """Freeze browser-owned input files and keep seed evidence out of task provenance."""
        execution = dict(context)
        source = execution.get("storage_state")
        if source is not None:
            if isinstance(source, os.PathLike):
                source = os.fspath(source)
            if isinstance(source, Mapping):
                source = BrowserStorageState.model_validate(source)
            execution["storage_state"] = StorageSeed.freeze(source).state
        archive = execution.get(REPLAY_ARCHIVE_KEY)
        if isinstance(archive, (str, os.PathLike)) and os.fspath(archive):
            execution[REPLAY_ARCHIVE_KEY] = str(Path(archive).expanduser().resolve())
        recordable = {key: value for key, value in execution.items() if key != "storage_state"}
        return PreparedSetupContext(execution, recordable)

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
          (``load_fingerprint``), plus committed request index metadata for rule
          coverage and conflicts; never WARC response bodies —
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
        source_reader = (
            open_archive(replay_archive)
            if self.network_source.intercepts and replay_archive
            else None
        )
        seed_source = context.get("storage_state")
        seed = StorageSeed.freeze(seed_source) if seed_source is not None else None
        if seed is not None and (cdp_url is not None or injected):
            problems.append(
                "storage_state requires an owned fresh browser context; external CDP state is unknown"
            )
        if seed is not None and not self._fresh_profile():
            problems.append(
                "storage_state requires a fresh profile; persistent-profile state cannot be replaced completely"
            )
        if seed is not None and self.network_source.intercepts and source_reader is not None:
            recorded_seed = read_initial_seed(source_reader)
            if seed.data != recorded_seed.data:
                problems.append(
                    "explicit storage_state conflicts with the replay archive initial storage seed"
                )
        elif source_reader is not None:
            read_initial_seed(source_reader)
        self.network_source.check_config(source_reader)
        if source_reader is not None:
            problems.extend(
                consistency_problems(
                    load_fingerprint(source_reader),
                    replayability=self.replayability,
                    viewport=(self.viewport_width, self.viewport_height),
                    background_tabs=self.background_tabs,
                    planned_launch_flags=planned_launch_flags(
                        self, cdp_url=cdp_url, injected_client=injected
                    ),
                    browser_defaults=planned_browser_defaults(
                        self, cdp_url=cdp_url, injected_client=injected
                    ),
                    expected_platform=self.expected_platform,
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
        storage_state: BrowserStorageState | str | None = None,
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
        ``storage_state`` freezes a typed cookie / Web Storage seed or JSON
        source before business navigation; replay uses committed initial-state
        evidence and refuses a conflicting explicit seed. Risk-control metadata
        is accepted and reported at WARNING because it is not automated.

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
        explicit_seed = StorageSeed.freeze(storage_state) if storage_state is not None else None
        self.check_setup(
            {
                "cdp_url": cdp_url,
                REPLAY_ARCHIVE_KEY: replay_archive,
                "start_url": start_url,
                "storage_state": explicit_seed.state if explicit_seed is not None else None,
            },
            negotiation=negotiation,
            archivers=self.bound_archivers,
        )

        unhandled = sorted(
            name
            for name, value in (
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
        # under base_time: from_archive, resolved before any shim is built. The
        # clock advances in real time from this start, in every document alike.
        now_ms = int(time.time() * 1000)
        t0_ms = replay_t0(recorded, self.replayability, now_ms) if recorded else now_ms
        color_scheme = (
            replay_color_scheme(recorded)
            if recorded is not None and self.replayability.needs_color_scheme
            else None
        )

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
                if isinstance(backend, SharedBrowserProvider) and backend.lifetime == "experiment":
                    owner = backend.managed_browser
                    if owner is None:
                        raise RuntimeError(
                            "backend lifetime: experiment requires an explicitly bound ExperimentScope"
                        )
                    self._context_lease = await owner.acquire(
                        launch_args=switches,
                        launch_env=env,
                        record_targets=bool(recorder_needs(self)),
                    )
                    cdp_url = self._context_lease.cdp_url
                elif (switches or env) and LAUNCH_ARGS in getattr(
                    type(backend), "__capabilities__", ()
                ):
                    self._session = await backend.open(launch_args=switches, launch_env=env)
                else:
                    self._session = await backend.open()
                if self._session is not None:
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
            control = self._new_cdp_client(
                cdp_url,
                additional_headers=self._session_cdp_headers(),
                **transport_options(cdp_url),
            )
            self._cdp = (
                BrowserContextClient(control, self._context_lease.context_id)
                if self._context_lease is not None
                else control
            )
            await self._cdp.start()

        # Seed before discovery, recorder attachment, business requests or scripts.
        # Replay always reads the pre-navigation evidence rather than step zero.
        if self.network_source.intercepts and replay_archive:
            self._initial_storage = read_initial_seed(open_archive(replay_archive))
        elif explicit_seed is not None:
            self._initial_storage = explicit_seed
        elif self._fresh_profile():
            self._initial_storage = StorageSeed(
                canonical_json(BrowserStorageState().model_dump(mode="json")), "fresh"
            )
        else:
            self._initial_storage = await capture_profile_initial(
                self._cdp, self.browser_context_id
            )
        if (
            explicit_seed is not None or self.network_source.intercepts
        ) and self._initial_storage.fingerprint()["nonempty"]:
            seeded_tabs = await hydrate_storage(
                self._cdp, self._initial_storage, self.browser_context_id
            )
            self._storage_tabs = {target: tab for tab, target in seeded_tabs.items()}

        # The browser's own internal OS fact, before any business tab or input
        # controller is prepared. This uses the control connection even without
        # an archive or a recorder, so it applies to plain and cloud CDP too.
        try:
            self._input_platform = await measure_input_platform(self._cdp)
        except Exception as exc:
            raise BrowserCooperationError(
                [
                    f"browser input platform could not be measured ({exc!r}); no UA or agent-host fallback is permitted"
                ]
            ) from exc
        if self.expected_platform is not None and self._input_platform != self.expected_platform:
            raise BrowserCooperationError(
                [
                    f"expected_platform={self.expected_platform!r} ↔ measured browser input platform={self._input_platform!r}"
                ]
            )
        if recorded is not None:
            platform_problems = input_platform_problems(recorded, self._input_platform)
            if platform_problems:
                raise BrowserCooperationError(platform_problems)

        # 1a. Every tab, in the order it opens, and which tab opened it (§4.14
        # "页面新开的标签页"): what following a new tab and leaving a closed one
        # read. Before the first tab is adopted — discovery reports the tabs
        # already open before its reply.
        await self._start_tab_discovery()

        # A newly acquired context has no default page. Make a blank page for
        # pre-recording measurements; business navigation still waits for the
        # recorder's ready barrier. Existing hydrated login tabs are retained.
        if self._context_lease is not None:
            targets = await self._cdp.send("Target.getTargets", {})
            if not any(info.get("type") == "page" for info in targets.get("targetInfos", [])):
                await self._cdp.send("Target.createTarget", {"url": "about:blank"})

        # 1b. The recorder connection (§4.14), built but not yet attached: its
        # client, the archive scope and channel, this run's plan and the bound
        # archivers' sessions.
        if recorder_needs(self):
            assert cdp_url is not None  # the cooperation check refused anything else
            await self._connect_recorder(
                cdp_url,
                t0_ms=t0_ms,
                run_started_ms=now_ms,
                recorded=recorded,
                notes=consistency_notes(recorded, planned_launch_flags=planned)
                if recorded is not None
                else [],
                expected_defaults=expected_defaults,
                color_scheme=color_scheme,
            )

        # 2. Global scope — browser-level watchdogs. Before the recorder
        # attaches: the ArchiveWatchdog (a shared watchdog, installed by
        # binding) must consume the archive scope from the recorder's first
        # event on, or the collection barrier counts those events as dropped.
        self._global_scope = await self._scope_group.create_scope(
            _GLOBAL_SCOPE_ID, max_history_size=0, metadata={"purpose": "browser"}
        )
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
        initial_target = next(iter(self._storage_tabs), None)
        page_target = next((t for t in targets if t.get("targetId") == initial_target), None)
        if page_target is None:
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
        # started the agent on about:blank. Not an action of the agent's, so
        # it opens no follow window: a tab the start page opens as it loads is
        # not followed.
        if start_url:
            result = await self._run_action(
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
        run_started_ms: int,
        recorded: RecordedArchive | None = None,
        notes: Sequence[str] = (),
        expected_defaults: BrowserDefaults | None = None,
        color_scheme: ColorScheme | None = None,
    ) -> None:
        """Build the recorder connection; :meth:`setup` opens it after the watchdogs.

        Its own client: no compression (large frames are 13–48x slower with
        permessage-deflate), no proxy (loopback CDP would tunnel through
        ``http_proxy``), no frame limit (a paused upload arrives twice). The
        connection is kept on ``self`` before it opens, so a failure part way
        is still cleaned up by :meth:`teardown`.

        With archivers bound, the browser's defaults are measured here, before
        the recorder prepares any target (:meth:`_measure_browser_defaults`).
        A tier whose plan overrides the user agent gets the browser's own
        client hints, read here too (:meth:`_measure_client_hints`); a plan
        that cannot use them (a Chrome identifier they contradict), or a
        replay whose plan carries other ones than the recording's, is refused
        here, before any target is prepared.

        Replaying *recorded* (M30), the network source's server is built here
        — pure construction, before the archive sessions, whose context
        carries its log — and the source run's tolerated mismatches
        (*notes*, the Chrome version) become its warnings.
        """
        if self._context_lease is not None:
            client = self._context_lease.recorder_client
        else:
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
            scope = await self._scope_group.create_scope(
                _ARCHIVE_SCOPE_ID,
                queue_maxsize=65536,
                max_history_size=0,
                metadata={"purpose": "archive"},
            )
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
        measurement = self._cdp if self._context_lease is not None else recorder
        assert measurement is not None
        if self._archivers:
            self._browser_defaults = await self._measure_browser_defaults(
                measurement, expected=expected_defaults, recorded=recorded
            )
        hints = (
            await self._measure_client_hints(measurement)
            if self.replayability.needs_client_hints
            else None
        )
        if self.replayability.needs_color_scheme and color_scheme is None:
            color_scheme = (
                replay_color_scheme(recorded)
                if recorded is not None
                else await self._measure_color_scheme(measurement)
            )
        plan = self.replayability.plan(
            t0_ms=t0_ms,
            run_started_ms=run_started_ms,
            browser_user_agent=str(version.get("userAgent", "")),
            client_hints=hints,
            color_scheme=color_scheme,
        )
        problems = [*plan.problems]
        if recorded is not None:
            problems.extend(client_hint_problems(recorded, plan))
        if problems:
            raise BrowserCooperationError(problems)
        self._replay_plan = plan
        if recorded is not None:
            server = self.network_source.serve(
                recorded,
                ReplayContext(recorder=recorder, cycle=lambda: self._observation_cycle),
            )
            for warning in [
                *notes,
                *version_warnings(
                    recorded,
                    browser_version=version,
                    replayability=self.replayability,
                    client_hints=plan.client_hints,
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
                    browser_context_id=self.browser_context_id,
                    initial_storage=self._initial_storage,
                    storage_tabs=dict(self._storage_tabs),
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

    async def _measure_color_scheme(
        self, recorder: RecorderConnection | CDPClient | BrowserContextClient
    ) -> ColorScheme:
        """The live recording's theme, measured once before any target is prepared."""
        try:
            return await measure_color_scheme(recorder, timeout=_COLOR_SCHEME_TIMEOUT_S)
        except Exception as exc:
            tier = getattr(
                self.replayability, "replayability_id", type(self.replayability).__name__
            )
            raise BrowserCooperationError(
                [
                    f"replayability {tier!r} ↔ this browser: its prefers-color-scheme could not "
                    f"be measured after launch ({exc!r}); no recording theme can be pinned"
                ]
            ) from exc

    async def _measure_client_hints(
        self, recorder: RecorderConnection | CDPClient | BrowserContextClient
    ) -> ClientHints:
        """The browser's own client hints, read right after launch (§4.14, ``client_hints.py``).

        In a temporary background tab, closed again before any tab is
        adopted. A read that fails refuses the run: the tier's user agent
        override would otherwise empty ``navigator.userAgentData`` and send
        no ``Sec-CH-UA``, which no real Chrome does.
        """
        try:
            return await measure_client_hints(recorder, timeout=_CLIENT_HINTS_TIMEOUT_S)
        except Exception as exc:
            tier = getattr(
                self.replayability, "replayability_id", type(self.replayability).__name__
            )
            raise BrowserCooperationError(
                [
                    f"replayability {tier!r} ↔ this browser: its own client hints could not be "
                    f"read after launch ({exc!r}), so the user agent override would carry none "
                    f"(no brands, no Sec-CH-UA)"
                ]
            ) from exc

    async def _measure_browser_defaults(
        self,
        recorder: RecorderConnection | CDPClient | BrowserContextClient,
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
        self, sid: str, capture: TargetCapture, device_pixel_ratio: float, request_id: str
    ) -> None:
        """Accept a self-capture only for the exact request that is still active."""
        cache = self._cycle_cache.get(sid)
        if cache is None or cache.active_request_id != request_id:
            return
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
        """Keep capture failure for archive health without poisoning control calls."""
        if self._recorder_error is None:
            self._recorder_error = error
        if self._recorder_runs_environment():
            self._alive = False
        logger.error("BrowserCapability: %s", error)

    def _recorder_runs_environment(self) -> bool:
        plan = self._replay_plan
        return self._replay_server is not None or (
            plan is not None and bool(plan.frame_commands or plan.worker_commands)
        )

    def _ensure_recording(self) -> None:
        """Archive consumers see the original recording failure."""
        if self._recorder_error is not None:
            raise self._recorder_error

    def _ensure_environment(self) -> None:
        """A lost replay server or target-configuration channel invalidates execution."""
        if self._recorder_error is not None and self._recorder_runs_environment():
            raise RuntimeError(
                "Browser environment lost its recorder connection while enforcing "
                f"replay or fixed target configuration: {self._recorder_error}"
            ) from self._recorder_error

    async def teardown(self) -> None:
        """Attempt every owned resource, then report cleanup failures together.

        Recorder cleanup precedes scopes and control transport. A user-supplied
        CDP endpoint gives us no ownership of its process, profile or existing
        tabs; only a ProviderSession opened by this capability is closed.
        Incomplete scopes and failed resources remain reachable for retry.
        """
        self._alive = False
        self._tab_sessions.clear()
        errors: list[BaseException] = []

        def clean(label: str, close: Callable[[], Any]) -> bool:
            try:
                close()
                return True
            except Exception as exc:
                exc.add_note(label)
                errors.append(exc)
                return False

        async def aclean(label: str, close: Callable[[], Awaitable[Any]]) -> bool:
            try:
                await close()
                return True
            except (Exception, asyncio.CancelledError) as exc:
                exc.add_note(label)
                errors.append(exc)
                return False

        if self._recorder is not None and await aclean("recorder close", self._recorder.close):
            self._recorder = None
        if self._replay_server is not None and clean(
            "replay server close", self._replay_server.close
        ):
            self._replay_server = None
        if self._archive is not None and await aclean("archive close", self._archive.aclose):
            self._archive = None
        # The tabs this run froze outlive it only in a browser it does not own
        # (an explicit cdp_url, an injected transport): they are set running
        # again. An owned browser or BrowserContext closes with them.
        if self._session is None and self._context_lease is None:
            for target_id in sorted(self._frozen):
                if self._tabs is None or not self._tabs.is_closed(target_id):
                    await aclean(
                        f"setting frozen tab {target_id} running",
                        lambda target_id=target_id: self._set_lifecycle(target_id, "active"),
                    )
        self._frozen.clear()
        clean("tab discovery close", self._stop_tab_discovery)
        for sid, bridge in list(self._bridges.items()):
            if clean(f"bridge close for {sid}", bridge.close):
                self._bridges.pop(sid, None)
        for sid, feed in list(self._navigation_feeds.items()):
            if clean(f"navigation feed close for {sid}", feed.close):
                self._navigation_feeds.pop(sid, None)

        watchdogs = [wd for group in self._watchdogs.values() for wd in group]
        for wd in [*watchdogs, *self._shared_watchdogs]:
            for binding in wd.bindings:
                clean(
                    f"{type(wd).__name__} detach for {binding.scope.scope_id}",
                    lambda wd=wd, binding=binding: wd.detach(binding.scope, binding.session_id),
                )
        if self._scope_group is not None:
            await aclean("scope group close", self._scope_group.close_all)
            if self._scope_group.scope_count:
                errors.append(
                    RuntimeError(f"Browser scopes remain unfinished: {self._scope_group.scope_ids}")
                )
            else:
                self._scope_group = None

        # Injected transports belong to their caller. Release the sessions
        # this capability attached, without stopping that transport or tabs.
        if self._cdp is not None and self._injected_client is not None:
            control = self._cdp
            for sid in tuple(self._session_to_target):
                await aclean(
                    f"control session detach for {sid}",
                    lambda sid=sid: control.send("Target.detachFromTarget", {"sessionId": sid}),
                )
            self._cdp = None
        elif self._cdp is not None and await aclean("control CDP close", self._cdp.stop):
            self._cdp = None

        if self._context_lease is not None and await aclean(
            "browser context close", self._context_lease.aclose
        ):
            self._context_lease = None

        backend = self.resolved_backend()
        if self._session is not None and backend is not None:
            session = self._session
            if await aclean("provider session close", lambda: backend.close(session)):
                self._session = None

        self._scopes = {sid: scope for sid, scope in self._scopes.items() if not scope.is_closed}
        self._js_scopes = {
            sid: scope for sid, scope in self._js_scopes.items() if not scope.is_closed
        }
        self._watchdogs = {
            key: [wd for wd in group if wd.bindings]
            for key, group in self._watchdogs.items()
            if any(wd.bindings for wd in group)
        }
        self._shared_watchdogs = [wd for wd in self._shared_watchdogs if wd.bindings]
        self._selector_maps.clear()
        self._target_to_session.clear()
        self._session_to_target.clear()
        self._cycle_cache.clear()
        self._main_frame_ids.clear()
        if self._global_scope is not None and self._global_scope.is_closed:
            self._global_scope = None
        self._default_session_id = None
        self._initial_storage = None
        self._storage_tabs.clear()
        self._replay_plan = None
        self._browser_defaults = None
        self._input_platform = None
        self._diverged_seen = 0
        self._media_alignment = None
        for frame_targets in self._frame_targets.values():
            frame_targets.close()
        self._frame_targets.clear()
        for error in errors:
            if isinstance(error, asyncio.CancelledError):
                for other in errors:
                    if other is not error:
                        error.add_note(f"Browser cleanup also failed: {other!r}")
                raise error
        if errors:
            raise ExceptionGroup(
                "Browser resource cleanup failed", [e for e in errors if isinstance(e, Exception)]
            )
        logger.info("BrowserCapability teardown complete")

    async def execute_action(self, action: Action) -> ActionResult:
        """Run one of the agent's actions on the focused tab (:meth:`_run_action`).

        First the archive side announces the call (:meth:`_announce_action`,
        the ``action_boundary`` channel, §4.14 ``browser.frames``): every
        call, so the archive numbers them exactly as the trace's action spans
        are numbered. Then the tab is checked: an action on a focused tab
        that has closed itself — before the announcement or while it was
        being waited for — fails at once, naming the tab; the next
        observation moves the focus off it. Only then does it open the
        follow window when none is open (§4.14 "页面新开的标签页"): a tab the
        focused page opens from now until the next observation captures is
        followed (``new_tabs: follow``), and one the page opened while the
        archive was capturing is not the agent's.
        """
        self._ensure_environment()
        await self._announce_action(action, closed=self._focused_tab_closed())
        closed = self._focused_tab_closed()
        if closed is not None:
            return ActionResult(
                success=False,
                error=(
                    f"the tab the agent was on ({closed}) closed itself; the next "
                    f"observation shows the tab the focus moves to"
                ),
            )
        self._open_follow_window()
        return await self._run_action(action)

    async def _announce_action(self, action: Action, *, closed: str | None) -> None:
        """Tell the archive side an action is about to run on the focused tab (*closed*: it is gone).

        Only the agent's actions are announced: setup's start page goes
        through :meth:`_run_action` directly. The action waits for the
        archivers that want boundaries, within their budget; nothing here
        depends on what they captured.
        """
        archive = self._archive
        if archive is None:
            return
        sid = self._default_session_id
        tab = closed if closed is not None else self._session_to_target.get(sid) if sid else None
        await archive.action_boundary(
            tab=tab,
            control_session_id=sid or "",
            action=action.name,
            tab_open=closed is None and sid is not None,
        )

    async def _run_action(self, action: Action) -> ActionResult:
        """Delegate action execution to the registered action instance.

        There is no central dispatch table or ``if/else`` chain here —
        each action class owns its own parameter handling and event
        emission. We invalidate the per-tab observe cache before
        dispatching so a subsequent ``resolve_fragment`` call will see
        the post-action page state.

        The tab acted on is the focused one (:attr:`default_session_id`).
        Setup opens the start page through here directly: not an action of
        the agent's, so no follow window.
        """
        self._ensure_environment()
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

        # ActionRegistrar registers implementations against BaseAction.
        # ActionBase carries configuration; execution consumes the public contract.
        executor = cast(BaseAction, impl)
        try:
            result = await executor.execute(action.params, capability=self)
        except RecorderLostError:
            raise
        except Exception as exc:
            self._ensure_environment()
            logger.error("Action %s failed: %s", action.name, exc, exc_info=True)
            return ActionResult(success=False, error=str(exc))
        return result

    async def begin_observation(self, cycle: int) -> None:
        """Open observation cycle *cycle* — the next read re-captures.

        Recording the number is the whole implementation: a cache entry
        stamped with an older cycle is not served (see
        :meth:`_populate_cycle`), so this costs nothing until a fragment
        is actually asked for, and a tab nobody observes this cycle is not
        scraped at all.

        No CDP call. A recorder failure affects capture health; only a lost
        replay or target-configuration channel invalidates the environment.
        """
        self._ensure_environment()
        self._observation_cycle = cycle
        pending = self._media_alignment
        if pending is not None and not pending.bind(cycle):
            # Requested for an earlier observation: it never reaches this one.
            self._media_alignment = None

    async def resolve_fragment(self, fragment_key: str) -> Any:
        """Resolve one observation fragment via the lazy-batch cache.

        The first call of a cycle fires a single
        ``BrowserStateRequestEvent`` carrying the negotiated fragment set;
        every other fragment of the same cycle is served from that one
        capture, however many times each is asked for. The next cycle is
        announced by :meth:`begin_observation`, not guessed at.

        Which keys are supported is decided by the installed watchdogs'
        ``provides`` — there is no branch per fragment here. The tab
        observed is the focused one (:attr:`default_session_id`), after the
        cycle's first fragment settled where the focus is
        (:meth:`_settle_focus`: a tab the agent's actions opened is followed,
        a focused tab that closed itself is left).
        """
        if self._default_session_id is None:
            raise RuntimeError("No active browser session")

        provided = self.provided_fragments
        if fragment_key not in provided:
            raise ValueError(
                f"BrowserCapability cannot resolve fragment "
                f"{fragment_key!r}. Active fragments for this run: "
                f"{sorted(provided)}."
            )

        # Ensure the focused tab holds a capture belonging to the current cycle.
        sid = await self._populate_cycle()

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

    def _cycle_entry(self, sid: str) -> _ObserveCycleCache:
        """Tab *sid*'s cache entry for the current cycle, made fresh when it has none."""
        cache = self._cycle_cache.get(sid)
        if cache is None or cache.cycle != self._observation_cycle:
            # No capture for this tab, or one belonging to an observation
            # that is over. Either way the page must be read again.
            cache = _ObserveCycleCache(cycle=self._observation_cycle)
            self._cycle_cache[sid] = cache
        return cache

    async def _populate_cycle(self) -> str:
        """Fire one batched state event per cycle on the focused tab, cache it, return its session.

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

        **Where the focus is, is settled first** (§4.14 "页面新开的标签页"):
        the cycle's first fragment asks :meth:`_settle_focus` before anything
        is waited for or captured, so the navigation and settle waits run on
        the tab the capture shows; :meth:`_capture_once` asks again after its
        waits. A failure there (no tab left open) is the cycle's failure,
        recorded the same way. The capture closes the follow window, whatever
        became of it.
        """
        if self._focus_cycle != self._observation_cycle:
            self._focus_cycle = self._observation_cycle
            self._focus_from = self._focused_target()
            self._focus_moves = 0
            self._focus_moved = None
            try:
                await self._settle_focus(read_tab_list=True)
            except Exception as exc:
                self._follow_window = None
                self._cycle_entry(self.default_session_id).error = exc
                raise

        sid = self.default_session_id
        cache = self._cycle_entry(sid)
        if cache.fired:
            return sid  # This cycle's capture is already in hand.

        if cache.error is not None:
            # This cycle's capture already failed, and it is one capture
            # for every fragment: re-attempting it per fragment would
            # multiply the cost of a bad step by the number of fragments
            # the agent negotiated. Same exception object, so the
            # environment reports one cause for the whole slice — which is
            # the truth about what happened.
            raise cache.error

        try:
            sid, outcome = await self._capture_once(sid)
        except Exception as exc:
            # Remembered on the cycle — on the tab focused now, the one every
            # later fragment of this slice asks for — so the rest of this
            # slice is refused rather than re-captured. Anything that reaches
            # here — a lost race, a dead scope, a CDP transport error — costs
            # the same once.
            self._cycle_entry(self.default_session_id).error = exc
            raise
        finally:
            self._follow_window = None

        cache = self._cycle_entry(sid)
        cache.fragments.update(outcome.fragments)
        cache.fired = True
        if self._archive is not None:
            # Only a successful cycle's capture reaches the archive (§4.12).
            tab = self._session_to_target.get(sid, "")
            self._archive.commit_raw(
                cache.raw,
                cycle=cache.cycle,
                tab=tab,
                viewport=(self.viewport_width, self.viewport_height),
                cleaner=str(
                    getattr(self.dom_cleaner, "cleaner_type", type(self.dom_cleaner).__name__)
                ),
            )
            cache.raw.clear()
            self._archive.cycle_committed(cycle=cache.cycle, tab=tab, control_session_id=sid)
        return sid

    async def _capture_once(self, sid: str) -> tuple[str, _StateEventOutcome]:
        """One capture attempt-with-retries of the focused tab; its session and outcome, or raise.

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
        ``settle_quiet_ms`` with something visible in its viewport — at most
        ``settle_max_wait_s``, after which it captures anyway. The wait is part of the observation: it runs before
        the one event that captures every negotiated fragment, whichever
        they are, and every fragment of the served capture records it
        (``BrowserCaptureFragment.settle``).

        Between the waits and the capture, the focus is settled again
        (:meth:`_settle_focus`, from the events alone): a tab the agent's
        actions opened while this observation waited — a click whose handler
        opens the tab once a request it sent comes back — is followed, a
        focused tab that closed meanwhile is left, and the waits run again on
        the tab focused now, without spending an attempt. Every fragment of
        the served capture records whether the focus moved
        (``BrowserCaptureFragment.focus_moved``).
        """
        wanted = self.provided_fragments
        outcome: _StateEventOutcome | None = None
        attempt = 1
        while True:
            scope = self._scopes.get(sid)
            if scope is None:
                raise RuntimeError(f"Unknown session_id: {sid}")
            cache = self._cycle_entry(sid)
            feed = self._navigation_feeds.get(sid)
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
            # The follow window is open until the capture (§4.14): a tab opened
            # while this observation waited moves the focus, and the waits run
            # again on the tab focused now.
            if await self._settle_focus(read_tab_list=False):
                sid = self.default_session_id
                continue
            cache.raw.clear()  # a re-fired attempt's capture replaces the last one's
            # A restored observation's media is aligned to the recording here:
            # after the waits, right before the screenshot (§4.14). Once per cycle.
            await self._align_media(sid)
            outcome = await self._fire_state_event(
                sid, scope, wanted, screenshot_first=self._aligned_playing_media()
            )
            # A recorder lost during the capture: nothing of it is served,
            # and the error says why rather than "no watchdog produced …".
            self._ensure_environment()
            # TODO: a capture missing any negotiated fragment, optional ones
            # included, is re-fired and finally fails whole; partial capture of
            # optional fragments may be allowed later (architecture §4.4,
            # capture_state). Until then optional resolvers record their own
            # problems inside their fragment instead of failing.
            if wanted <= outcome.fragments.keys():
                if not documents_disagree(outcome.stamps):
                    for fragment in outcome.fragments.values():
                        if isinstance(fragment, BrowserCaptureFragment):
                            fragment.settle = settle
                            fragment.focus_moved = self._focus_moved
                    return sid, outcome
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
            else:
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
            if attempt >= _OBSERVE_MAX_ATTEMPTS:
                break
            attempt += 1

        assert outcome is not None  # a capture was fired before the loop could end
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

    def _aligned_playing_media(self) -> bool:
        """Whether this cycle's alignment left a recorded-playing element playing (§4.14).

        Its capture takes the screenshot first: the tab captures fragments one
        after another, and a playing video moves on through a DOM capture taken
        before the pixels (measured: 0.6 s at 5,000 elements, 3.5 s at 30,000,
        past the restorer's 1 s tolerance). Every other capture keeps its order.
        """
        pending = self._media_alignment
        return (
            pending is not None
            and pending.cycle == self._observation_cycle
            and pending.report is not None
            and pending.report.left_playing
        )

    async def _align_media(self, sid: str) -> None:
        """Run the media alignment requested for this cycle, if any (``align_next_observation``)."""
        pending = self._media_alignment
        if pending is None or not pending.due(self._observation_cycle) or self._cdp is None:
            return
        try:
            pending.report = await align_media(
                self._cdp,
                session_id=sid,
                frame_id=self._main_frame_ids.get(sid),
                frames=self._frame_targets.get(sid),
                recorded=pending.recorded,
                tolerance_px=self.retarget_tolerance_px,
                read_timeout_s=self.media_state_timeout_s,
                seek_timeout_s=pending.seek_timeout_s,
                end_lead_s=pending.end_lead_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # aligning never fails the observation; it is reported
            logger.warning("media alignment (sid=%s) raised: %r", sid, exc)
            pending.report = alignment_raised(pending.recorded, exc)

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
        self,
        sid: str,
        scope: EventScope,
        wanted: frozenset[str],
        *,
        screenshot_first: bool = False,
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

        ``screenshot_first`` puts the screenshot ahead of the DOM capture
        (:meth:`_aligned_playing_media`); otherwise the order is unchanged.
        """
        event = BrowserStateRequestEvent(fragments=wanted, screenshot_first=screenshot_first)
        self._cycle_entry(sid).active_request_id = event.event_id

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
            try:
                await event.wait()
            except TimeoutError:
                unfinished_handlers = any(
                    result.participation is Participation.HANDLER and not result.is_terminal
                    for result in event.results.values()
                )
                event.cancel_execution(include_running=False)
                if unfinished_handlers:
                    raise
                # Capture-only observers may time out independently. Their
                # missing evidence is reported by archive completeness.
            except asyncio.CancelledError:
                event.cancel_execution(include_running=False)
                raise
        finally:
            if action_wd is not None:
                await action_wd.set_overlay_visible(True)

        # Merge every responding watchdog's fragments. The capability has
        # no idea what any of them mean — that is the point.
        outcome = _StateEventOutcome()
        for result_obj in event.results.values():
            if result_obj.participation is Participation.OBSERVER:
                continue
            outcome.handler_count += 1
            error = result_obj.business_error or result_obj.error
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
        """Best-effort tab enumeration, in the order the tabs opened; returns empty on error.

        ``Target.getTargets`` lists tabs in no fixed order (measured, Chrome
        155: a tab its opener cannot reach before its opener, a
        ``window.open`` tab after it), so the tab registry's order is used:
        the position of a tab means the same in a recording and in its
        restoration (``browser.switch_tab`` is re-targeted by it, §4.14).
        """
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
            if self._tabs is not None:
                order = self._tabs.in_opening_order(tab["tab_id"] for tab in tabs)
                tabs.sort(key=lambda tab: order.index(tab["tab_id"]))
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
        """Every open page target id, in the order the tabs opened (``Target.getTargets`` order
        without the tab registry)."""
        if self._cdp is None:
            raise RuntimeError("BrowserCapability not set up")
        result = await self._cdp.send("Target.getTargets", {})
        ids = [
            t["targetId"]
            for t in (result or {}).get("targetInfos", [])
            if t.get("type") == "page" and t.get("targetId")
        ]
        return self._tabs.in_opening_order(ids) if self._tabs is not None else ids

    async def switch_tab(self, target_id: str | None = None) -> TabSelection:
        """Move agent focus to another tab; return its target and control session.

        ``target_id`` is a CDP ``targetId`` — the same string the agent
        already sees as ``tab_id`` in the ``browser_meta`` fragment.
        ``None`` means "the most recently opened tab other than the
        current one", matching the action's advertised semantics: the last
        in the order the tabs opened (the tab registry's).

        The agent chose where to look, so an open follow window restarts at
        the tab it switched to (§4.14 "页面新开的标签页"): a tab the tab it
        left opened is no longer followed, one the newly focused tab opens
        from now is.

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

        sid = await self._move_focus(target_id)
        if self._follow_window is not None and self._tabs is not None:
            self._follow_window = FollowWindow(focused=target_id, mark=self._tabs.mark)
        logger.info("Agent focus switched to tab %s (session %s)", target_id, sid)
        return TabSelection(target_id=target_id, session_id=sid)

    async def _move_focus(
        self, target_id: str, *, url: str = "", adopt_timeout: float | None = None
    ) -> str:
        """Make *target_id* the focused tab; return its session.

        What :meth:`switch_tab` and following a new tab share (§4.14
        "页面新开的标签页"): a tab the page opened has no scope or watchdogs
        until it is adopted here (:meth:`_attach_tab`; *adopt_timeout* bounds
        that), ``Target.activateTarget`` brings it to the front so it runs
        foreground timers, renders and screenshots the way a user-visible tab
        does (measured, Chrome 155: it turns ``visible`` and the tab it
        replaced ``hidden``), and its cache from an earlier cycle is dropped —
        the agent is about to observe it fresh.

        Under ``background_tabs: freeze`` (§4.14 "离开焦点的 tab") a tab
        frozen when the focus left it is set running before it is brought to
        the front, and the tab the focus leaves is frozen once the new one is
        in front: from then on only the focused tab of the ones the focus was
        ever on runs.
        """
        if self._cdp is None:
            raise RuntimeError("BrowserCapability not set up")
        left = self._focused_target()
        sid = self._target_to_session.get(target_id)
        if sid is None:
            attach = self._attach_tab(target_id, url)
            sid = await (
                attach if adopt_timeout is None else asyncio.wait_for(attach, adopt_timeout)
            )
        await self._resume_tab(target_id)
        await self._cdp.send("Target.activateTarget", {"targetId": target_id})
        self._default_session_id = sid
        self._cycle_cache.pop(sid, None)
        if left is not None and left != target_id:
            await self._freeze_tab(left)
        return sid

    # ── Freezing the tabs the focus left (§4.14 "离开焦点的 tab") ──────

    async def _freeze_tab(self, target_id: str) -> None:
        """Freeze *target_id*, the tab the focus just left, under ``background_tabs: freeze``.

        ``Page.setWebLifecycleState{state: frozen}`` on its control session:
        Chrome stops the page's task queues (timers, scripts, storage events,
        network callbacks; its out-of-process iframes too) and fires the
        page's ``freeze`` event. A tab that closed has nothing to freeze. A
        failure is only logged: the tab keeps running, as under ``run``.
        """
        if self.background_tabs != "freeze" or target_id in self._frozen:
            return
        if self._tabs is not None and self._tabs.is_closed(target_id):
            return
        try:
            await self._set_lifecycle(target_id, "frozen")
        except Exception as exc:
            logger.warning(
                "BrowserCapability: the tab the focus left (%s) could not be frozen and keeps "
                "running: %r",
                target_id,
                exc,
            )
            return
        self._frozen.add(target_id)

    async def _resume_tab(self, target_id: str) -> None:
        """Set *target_id* running again, if the focus froze it when it left; before it is shown.

        A failure is only logged: bringing the tab to the front resumes a
        frozen page anyway (measured, Chrome 155), only without telling the
        browser it is no longer frozen.
        """
        if target_id not in self._frozen:
            return
        self._frozen.discard(target_id)
        try:
            await self._set_lifecycle(target_id, "active")
        except Exception as exc:
            logger.warning(
                "BrowserCapability: the frozen tab %s could not be set running before the focus "
                "returns to it: %r",
                target_id,
                exc,
            )

    async def _set_lifecycle(self, target_id: str, state: Literal["frozen", "active"]) -> None:
        """``Page.setWebLifecycleState`` on *target_id*'s control session."""
        sid = self._target_to_session.get(target_id)
        if self._cdp is None or sid is None:
            raise RuntimeError(f"tab {target_id} is not attached")
        await self._cdp.send(
            "Page.setWebLifecycleState",
            {"state": state},
            session_id=sid,
            timeout=_LIFECYCLE_TIMEOUT_S,
        )

    # ── Following new tabs, leaving closed ones (§4.14 "页面新开的标签页") ──

    async def _start_tab_discovery(self) -> None:
        """Turn on the control connection's target discovery, feeding a fresh :class:`TabRegistry`.

        ``Target.setDiscoverTargets{discover: true, filter: [{type: page}]}``
        reports every tab's ``targetCreated`` (with ``openerId``),
        ``targetInfoChanged`` and ``targetDestroyed`` — the tabs already open
        before its reply (measured, Chrome 155). ``Page.windowOpen``, which
        says whether a window was opened with the user's activation, arrives
        on the session of the tab that opened it: every tab this connection
        adopted has ``Page.enable``. A browser that refuses discovery runs
        without it, as before: nothing is followed, a focused tab that closes
        is not left, tabs are listed in ``Target.getTargets`` order.
        """
        if self._cdp is None:
            return
        registry = TabRegistry()

        def created(params: Mapping[str, Any], _session_id: str | None = None) -> None:
            registry.created(params.get("targetInfo") or {})

        def changed(params: Mapping[str, Any], _session_id: str | None = None) -> None:
            registry.changed(params.get("targetInfo") or {})

        def destroyed(params: Mapping[str, Any], _session_id: str | None = None) -> None:
            registry.destroyed(str(params.get("targetId") or ""))

        def window_open(params: Mapping[str, Any], session_id: str | None = None) -> None:
            opener = self._session_to_target.get(session_id or "")
            if opener is not None:
                registry.window_opened(opener, bool(params.get("userGesture")))

        for method, callback in (
            ("Target.targetCreated", created),
            ("Target.targetInfoChanged", changed),
            ("Target.targetDestroyed", destroyed),
            ("Page.windowOpen", window_open),
        ):
            self._cdp.on_event(method, callback)
            self._tab_callbacks.append((method, callback))
        self._tabs = registry
        # The filter keeps every other target's events off this connection; a
        # browser that predates it gets them all, and the registry keeps the tabs.
        refused: Exception | None = None
        for params in (
            {"discover": True, "filter": [{"type": "page"}]},
            {"discover": True},
        ):
            try:
                await self._cdp.send("Target.setDiscoverTargets", params)
            except Exception as exc:
                refused = exc
                continue
            return
        logger.warning(
            "BrowserCapability: the browser refused target discovery (%s): a tab a page "
            "opens is not followed, and a focused tab that closes itself is not left",
            refused,
        )
        self._stop_tab_discovery()

    def _stop_tab_discovery(self) -> None:
        """Unregister the discovery callbacks; forget the registry and the focus state."""
        callbacks, self._tab_callbacks = self._tab_callbacks, []
        if self._cdp is not None:
            for method, callback in callbacks:
                self._cdp.off_event(method, callback)
        self._tabs = None
        self._follow_window = None
        self._focus_cycle = None
        self._focus_from = None
        self._focus_moves = 0
        self._focus_moved = None
        self._unfollowable = set()

    def _focused_target(self) -> str | None:
        """The focused tab's target id, when one is attached."""
        return self._session_to_target.get(self._default_session_id or "")

    def _open_follow_window(self) -> None:
        """Open the follow window at the focused tab, unless one is open (``new_tabs: follow``)."""
        if self._follow_window is not None or self._tabs is None or self.new_tabs != "follow":
            return
        focused = self._focused_target()
        if focused is not None:
            self._follow_window = FollowWindow(focused=focused, mark=self._tabs.mark)

    def _focused_tab_closed(self) -> str | None:
        """The focused tab's target id when the page closed it, else ``None``."""
        focused = self._focused_target()
        if focused is not None and self._tabs is not None and self._tabs.is_closed(focused):
            return focused
        return None

    async def _settle_focus(self, *, read_tab_list: bool) -> bool:
        """Move the focus before a capture when the rule says so; whether it moved.

        In order (§4.14 "页面新开的标签页"):

        1. ``new_tabs: follow`` with the follow window open: the newest tab
           the window's focused tab opened during the window, still open, and
           not reported as opened without the user's activation
           (:func:`~spectragent.core.capabilities.browser.tabs.tabs_to_follow`),
           unless the focus is on it already; one that cannot be adopted gives
           way to the next newest;
        2. the focused tab closed itself, whichever ``new_tabs``: the tab that
           opened it if it is still open, else the most recently opened tab
           still open (:func:`~spectragent.core.capabilities.browser.tabs.tabs_after_close`).
           No tab open at all raises.

        *read_tab_list* — the cycle's first decision, when a window is open or
        the focused tab is known closed — reads ``Target.getTargets`` first:
        every ``targetCreated`` of a tab open now arrives before its reply, and
        the answer says which tabs are still open. The decision after the
        observation's waits reads only the events since. At most
        ``_FOCUS_MOVES_PER_OBSERVATION`` moves per observation. Afterwards the
        tabs the page closed are released.
        """
        registry = self._tabs
        focused = self._focused_target()
        if registry is None or self._cdp is None or focused is None:
            return False
        window = self._follow_window if self.new_tabs == "follow" else None
        if read_tab_list and (window is not None or registry.is_closed(focused)):
            await self._read_tab_list(registry)
        moved = False
        if self._focus_moves < _FOCUS_MOVES_PER_OBSERVATION:
            if window is not None:
                for tab in tabs_to_follow(registry, window, exclude=self._unfollowable):
                    if tab.target_id == focused:
                        break  # the newest such tab is where the focus is
                    if await self._move_focus_to(tab, "new_tab"):
                        moved = True
                        break
            if not moved and registry.is_closed(focused):
                after = tabs_after_close(registry, focused)
                if not after:
                    raise RuntimeError(
                        f"the tab the agent was on ({focused}) closed itself, and no other "
                        f"tab is open"
                    )
                for tab in after:
                    if tab.target_id not in self._unfollowable and await self._move_focus_to(
                        tab, "tab_closed"
                    ):
                        moved = True
                        break
        await self._release_closed_tabs()
        return moved

    async def _read_tab_list(self, registry: TabRegistry) -> None:
        """One ``Target.getTargets``, reconciled into *registry*; a failure is only logged."""
        assert self._cdp is not None
        try:
            reply = await self._cdp.send("Target.getTargets", {}, timeout=_TAB_LIST_TIMEOUT_S)
        except Exception as exc:
            logger.debug("BrowserCapability: Target.getTargets failed: %s", exc)
            return
        registry.reconcile(
            info for info in (reply or {}).get("targetInfos", []) if isinstance(info, Mapping)
        )

    async def _move_focus_to(self, tab: PageTab, reason: FocusMoveReason) -> bool:
        """Move the focus to *tab* for this observation and record why; ``False`` when it cannot be adopted.

        Adoption is bounded by ``_FOLLOW_ADOPT_TIMEOUT_S``: a tab whose first
        navigation is in flight answers its first page-side command only once
        that navigation commits, so adopting it waits for its first document.
        A tab that cannot be adopted (it never committed in time, or it is
        gone) is never tried again.
        """
        try:
            await self._move_focus(
                tab.target_id, url=tab.url, adopt_timeout=_FOLLOW_ADOPT_TIMEOUT_S
            )
        except Exception as exc:
            self._unfollowable.add(tab.target_id)
            logger.warning(
                "BrowserCapability: the focus cannot move to tab %s (%s, %s): %r",
                tab.target_id,
                tab.url or "no URL yet",
                reason,
                exc,
            )
            return False
        self._focus_moves += 1
        start = self._focus_from
        self._focus_moved = (
            FocusMove(reason=reason, from_tab=start, to_tab=tab.target_id)
            if start is not None and start != tab.target_id
            else None
        )
        logger.info(
            "BrowserCapability: the focus moved to tab %s (%s): %s",
            tab.target_id,
            tab.url or "no URL yet",
            "a tab the page opened" if reason == "new_tab" else "the focused tab closed itself",
        )
        return True

    async def _release_closed_tabs(self) -> None:
        """Release what the capability holds for tabs the page closed — never the focused one.

        Closing idle agent-cdp scopes is immediate; their bindings must not
        survive until the end of the run.
        """
        if self._tabs is None:
            return
        for target_id, sid in list(self._target_to_session.items()):
            if sid != self._default_session_id and self._tabs.is_closed(target_id):
                await self._release_tab(sid)

    async def close_tab(self, session_id: str) -> None:
        """Close one requested tab, attempting target closure even if its bindings fail."""
        if session_id == self._default_session_id:
            raise ValueError(
                "Cannot close the default session tab. "
                "Create a new tab and reassign before closing."
            )
        target_id = self._session_to_target.get(session_id)
        if target_id is None:
            return
        errors: list[BaseException] = []
        try:
            await self._release_tab(session_id)
        except (Exception, asyncio.CancelledError) as exc:
            errors.append(exc)
        if self._cdp is not None:
            try:
                await self._cdp.send("Target.closeTarget", {"targetId": target_id})
            except (Exception, asyncio.CancelledError) as exc:
                # Keep the identity needed for an explicit retry even if the
                # scopes already closed. Session authority remains revoked.
                self._session_to_target[session_id] = target_id
                self._target_to_session[target_id] = session_id
                errors.append(exc)
        for error in errors:
            if isinstance(error, asyncio.CancelledError):
                for other in errors:
                    if other is not error:
                        error.add_note(f"Tab cleanup also failed: {other!r}")
                raise error
        if errors:
            raise ExceptionGroup(
                f"Tab {target_id!r} close failed", [e for e in errors if isinstance(e, Exception)]
            )

    async def _release_tab(self, session_id: str) -> str | None:
        """Release only this tab's bindings and both scopes, even if cleanup fails."""
        target_id = self._session_to_target.get(session_id)
        if target_id is None:
            return None
        errors: list[BaseException] = []
        self._tab_sessions.unbind(session_id)
        bridge = self._bridges.get(session_id)
        if bridge is not None:
            try:
                bridge.close()
            except Exception as exc:
                errors.append(exc)
            else:
                self._bridges.pop(session_id, None)
        feed = self._navigation_feeds.get(session_id)
        if feed is not None:
            try:
                feed.close()
            except Exception as exc:
                errors.append(exc)
            else:
                self._navigation_feeds.pop(session_id, None)
        scope = self._scopes.get(session_id)
        if scope is not None:
            for wd in [*self._watchdogs.get(target_id, []), *self._shared_watchdogs]:
                try:
                    wd.detach(scope, session_id)
                except Exception as exc:
                    errors.append(exc)
        if self._scope_group is not None:
            for scope_id in (target_id, f"{target_id}:js"):
                if scope_id not in self._scope_group.scope_ids:
                    continue
                try:
                    result = await self._scope_group.close_scope(scope_id)
                    if not result.complete:
                        errors.append(
                            RuntimeError(f"Browser scope {scope_id!r} remains unfinished: {result}")
                        )
                except (Exception, asyncio.CancelledError) as exc:
                    errors.append(exc)

        for scopes in (self._scopes, self._js_scopes):
            bound_scope = scopes.get(session_id)
            if bound_scope is not None and bound_scope.is_closed:
                scopes.pop(session_id, None)
        self._selector_maps.pop(session_id, None)
        remaining = [wd for wd in self._watchdogs.get(target_id, []) if wd.bindings]
        if remaining:
            self._watchdogs[target_id] = remaining
        else:
            self._watchdogs.pop(target_id, None)
        self._cycle_cache.pop(session_id, None)
        self._main_frame_ids.pop(session_id, None)
        frame_targets = self._frame_targets.pop(session_id, None)
        if frame_targets is not None:
            frame_targets.close()
        for error in errors:
            if isinstance(error, asyncio.CancelledError):
                for other in errors:
                    if other is not error:
                        error.add_note(f"Tab cleanup also failed: {other!r}")
                raise error
        if errors:
            raise ExceptionGroup(
                f"Tab {target_id!r} cleanup failed", [e for e in errors if isinstance(e, Exception)]
            )
        self._target_to_session.pop(target_id, None)
        self._session_to_target.pop(session_id, None)
        self._frozen.discard(target_id)
        return target_id

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
            max_history_size=0,
            metadata={"target_id": target_id, "url": url, "cdp_session_id": cdp_session_id},
        )
        created_scope_ids.append(target_id)
        js_scope = await self._scope_group.create_scope(
            f"{target_id}:js",
            max_history_size=0,
            metadata={"target_id": target_id, "url": url, "cdp_session_id": cdp_session_id},
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
        self._tab_sessions.bind(cdp_session_id)

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
            try:
                feed.close()
            except Exception as exc:
                logger.warning(
                    "Attach rollback: navigation feed close failed for %s: %s", cdp_session_id, exc
                )

        self._tab_sessions.unbind(cdp_session_id)
        if self._scope_group is not None and target_id in self._scope_group.scope_ids:
            scope = self._scope_group.get_scope(target_id)
            for wd in [*self._watchdogs.pop(target_id, []), *self._shared_watchdogs]:
                try:
                    wd.detach(scope, cdp_session_id)
                except Exception as exc:
                    logger.warning(
                        "Attach rollback: watchdog detach failed for %s: %s", target_id, exc
                    )

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
        # The tab's frame registry is the capability's, not a watchdog's: the
        # watchdogs' detach above leaves it watching, so it is closed here.
        frame_targets = self._frame_targets.pop(cdp_session_id, None)
        if frame_targets is not None:
            frame_targets.close()

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
            if cdp_session_id is not None and self._tab_sessions.owns(
                cdp_session_id, cdp_session_id
            ):
                self._selector_maps[cdp_session_id] = selector_map

        # Per-tab main_frame_id getter — ``_on_navigate`` uses it to
        # filter sub-frame Page.lifecycleEvent so we only wait on the
        # main frame's load/networkidle.
        def _main_frame_id_getter() -> str | None:
            if cdp_session_id is not None:
                return self._main_frame_ids.get(cdp_session_id)
            return None

        # One frame-session registry per tab, shared by the DOM resolver and
        # the media reads (never for the browser-level session: auto-attach
        # there would adopt every tab). Whichever reaches into the tab's frames
        # first starts watching it; a second registry on the same tab would
        # never hear of the frames already attached (``_frame_targets``).
        frame_targets: FrameTargets | None = None
        if cdp_session_id is not None:
            frame_targets = self._frame_targets.get(cdp_session_id)
            if frame_targets is None:
                frame_targets = FrameTargets(cdp)
                self._frame_targets[cdp_session_id] = frame_targets
        # The only optional argument here is a callback, not a union of all
        # context fields (the tab-local pointer state is supplied by its factory).
        capture_options: dict[str, Any] = (
            {"raw_capture_sink": self._raw_capture_sink(cdp_session_id)}
            if cdp_session_id is not None
            and self._archive is not None
            and "raw_dom" in self._archive.needs
            else {}
        )
        return WatchdogContext(
            cdp=cdp,
            profile=self.profile_config,
            viewport=(self.viewport_width, self.viewport_height),
            input_platform=self._input_platform,
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
            screenshot_cursor=self.screenshot_cursor,
            screenshot_cursor_size=self.screenshot_cursor_size,
            action_effect_timeout_s=self.action_effect_timeout_s,
            type_focus_wait_s=self.type_focus_wait_s,
            capture_state_timeout_s=self.capture_state_timeout_s,
            media_state_timeout_s=self.media_state_timeout_s,
            frame_targets=frame_targets,
            selector_map_getter=_selector_map_getter,
            selector_map_setter=_selector_map_setter,
            main_frame_id_getter=_main_frame_id_getter,
            tabs_getter=self._get_tabs_safe,
            target_sessions_getter=lambda: dict(self._session_to_target),
            tab_sessions=self._tab_sessions,
            archive=self._recorder.channel if self._recorder is not None else None,
            # A real sink only when setup opened the raw-DOM channel (§4.12);
            # otherwise the default no-op, and the resolver never knows.
            **capture_options,
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
