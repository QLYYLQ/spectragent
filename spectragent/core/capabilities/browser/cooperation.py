"""Can this browser configuration work at all? — the cooperation check (§4.14).

What *may* be configured is constrained by the generated config; whether
the chosen pieces can **cooperate** is decided here (the 2026-09-24 ruling),
in one function that reports every problem at once and names both parties
in each line — the strategy or archiver on one side, the provider or knob on
the other. Every row is decided by configuration and task data alone, so
all of them are refused at ``prepare()``: ``BrowserCapability.check_setup``
(called through ``Environment.check_setup`` with the task's setup context
and the archivers about to be bound) runs :func:`cooperation_problems`, with
a replay's consistency check beside it. ``bind_archivers`` runs the binding
rows again when it binds, and ``setup`` runs ``check_setup`` once more
before it launches anything — defensive re-checks of the same function.

The rows this module owns:

- **Ownership** — everything that opens the recorder connection (a bound
  browser archiver, ``replayability`` other than ``none``) needs a browser the
  capability launched itself: a provider declaring ``NETWORK_CAPTURE``, no
  explicit ``cdp_url``, no injected control client. A recorder holding
  ``waitForDebuggerOnStart`` on a browser it does not own can wedge that
  browser for everyone (measured, ``docs/architecture_design.md`` §4.14).
- **Replayability** — a tier with launch switches needs a provider declaring
  ``LAUNCH_ARGS`` (the only way the switches reach Chrome), none of the
  provider's knobs may contradict them, the profile must be fresh, and a
  clock read from an archive needs an archive network source (M30).
- **Network source** (M30) — an intercepting source (``archive``) opens the
  recorder connection too (the ownership rows), needs the task's
  ``replay_archive`` (and a ``live`` source refuses one), needs a bound
  archiver that records the replay log (the ``replay_log`` need:
  ``browser.replay_log`` — a miss must always be recorded), refuses one that
  records live traffic (the ``fetch_response`` need: ``browser.network`` — a
  replay has none), and with ``isolation: required`` a provider declaring
  ``NETWORK_ISOLATION`` whose own knobs do not route around the isolation —
  unless the configuration says ``isolation: none_acknowledged``. A ``live``
  source refuses an archiver with the ``replay_log`` need: it replays
  nothing. Archivers are found by need, never by archiver id.

- **Archivers** — every bound browser archiver opens the recorder connection
  (the ownership rows above), a ``require_replayable`` archiver refuses the
  ``none`` tier, a browser that did not start from a fresh profile needs
  ``allow_session_state`` on every bound archiver (§5.6: the definition of
  ``logged_in``), and a profile that excludes the ``archive`` watchdog leaves
  nothing to drain the archive scope. :func:`binding_problems` is this group
  — plus, when any archiver is bound, the network source's
  configuration-decidable rows (which archivers fit the source, isolation):
  ``bind_archivers`` runs it before it changes the binding.

Whether a replay's *source run* matches this configuration is the replay
layer's check (``replay/consistency.py``): ``check_setup`` reads the source
run's environment fingerprint (``load_fingerprint``, no index) and folds the
check into the same error.
"""

from collections.abc import Sequence
from typing import Any, Protocol

from spectragent.core.capabilities.browser.archive.base import BrowserArchiverBase
from spectragent.core.capabilities.browser.archive.replay_log import REPLAY_LOG_ARCHIVER
from spectragent.core.capabilities.browser.backends.base import (
    LaunchArgsProvider,
    NetworkCaptureProvider,
    NetworkIsolatingProvider,
)
from spectragent.core.capabilities.browser.backends.browser_defaults import BrowserDefaults
from spectragent.core.capabilities.browser.backends.capabilities import (
    LAUNCH_ARGS,
    NETWORK_CAPTURE,
    NETWORK_ISOLATION,
)
from spectragent.core.capabilities.browser.backends.chrome_switches import NETWORK_ISOLATION_ARGS
from spectragent.core.capabilities.browser.profiles import BrowserProfileConfig
from spectragent.core.capabilities.browser.replay import (
    NetworkSourceProtocol,
    ReplayabilityProtocol,
)
from spectragent.core.capabilities.browser.watchdogs.archive_watchdog import ArchiveWatchdog

#: The archive need of the archiver that records a replay's decisions.
REPLAY_LOG_NEED = "replay_log"
#: The archive need of an archiver that captures live responses (traffic).
LIVE_TRAFFIC_NEED = "fetch_response"


class CooperatingCapability(Protocol):
    """What the check reads off the capability — and nothing else."""

    @property
    def replayability(self) -> ReplayabilityProtocol: ...

    @property
    def network_source(self) -> NetworkSourceProtocol: ...

    @property
    def bound_archivers(self) -> tuple[BrowserArchiverBase, ...]: ...

    @property
    def profile(self) -> str: ...

    @property
    def profile_config(self) -> BrowserProfileConfig: ...

    def resolved_backend(self) -> Any: ...


class BrowserCooperationError(RuntimeError):
    """The configuration's parts cannot cooperate; ``problems`` lists every pair."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        lines = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(
            "BrowserCapability cannot run this configuration "
            f"({len(self.problems)} problem(s); docs/architecture_design.md §4.14):\n{lines}"
        )


def _tier_name(replayability: Any) -> str:
    return (
        f"replayability: {getattr(replayability, 'replayability_id', type(replayability).__name__)}"
    )


def _provider_name(backend: Any) -> str:
    return f"backend provider_id={getattr(backend, 'provider_id', type(backend).__name__)!r}"


def _archiver_name(archiver: BrowserArchiverBase) -> str:
    return f"archiver {archiver.archiver_id}"


def _source_name(source: Any) -> str:
    return f"network_source: {getattr(source, 'source_id', type(source).__name__)}"


def browser_archivers(archivers: Sequence[Any]) -> list[BrowserArchiverBase]:
    """*archivers* as browser archivers, in order — anything else refused by name.

    Raises:
        TypeError: An archiver that is not a ``BrowserArchiverBase``.
    """
    foreign = sorted(
        str(getattr(a, "archiver_id", a))
        for a in archivers
        if not isinstance(a, BrowserArchiverBase)
    )
    if foreign:
        raise TypeError(
            f"BrowserCapability can only bind browser archivers; got {foreign} "
            f"(not BrowserArchiverBase)."
        )
    return [a for a in archivers if isinstance(a, BrowserArchiverBase)]


def recorder_needs(cap: CooperatingCapability) -> list[str]:
    """Every configured party that needs the recorder connection, by name."""
    needs = [_archiver_name(archiver) for archiver in cap.bound_archivers]
    if cap.replayability.replayable:
        needs.append(_tier_name(cap.replayability))
    if cap.network_source.intercepts:
        needs.append(_source_name(cap.network_source))
    return needs


def persistent_profile(backend: Any) -> str | None:
    """Which knob makes the provider's profile non-fresh, or ``None``.

    Asked of a provider that owns its browser (``NETWORK_CAPTURE``); one that
    declares it without implementing ``NetworkCaptureProvider`` never gets
    here — the ownership row refuses it rather than taking it as fresh.
    """
    if isinstance(backend, NetworkCaptureProvider):
        return backend.persistent_profile()
    return None


def _ownership_problems(
    need: str, backend: Any, *, cdp_url: str | None, injected_client: bool
) -> list[str]:
    problems: list[str] = []
    if cdp_url:
        problems.append(
            f"{need} ↔ cdp_url: an explicit CDP endpoint is a browser this capability did "
            f"not launch, and the recorder connection may only run on one it owns"
        )
    if injected_client:
        problems.append(
            f"{need} ↔ injected control client: an injected client is a browser this "
            f"capability did not launch; tests use _inject_cdp_factory for the transport"
        )
    if cdp_url or injected_client:
        return problems
    if backend is None:
        problems.append(
            f"{need} ↔ no backend: the recorder connection needs a browser_provider "
            f"declaring NETWORK_CAPTURE (local_chrome)"
        )
    elif NETWORK_CAPTURE not in getattr(type(backend), "__capabilities__", set()):
        problems.append(
            f"{need} ↔ {_provider_name(backend)}: the provider does not declare "
            f"NETWORK_CAPTURE (it does not launch a browser this capability owns), and the "
            f"recorder connection needs one"
        )
    elif not isinstance(backend, NetworkCaptureProvider):
        problems.append(
            f"{need} ↔ {_provider_name(backend)}: the provider declares NETWORK_CAPTURE but "
            f"does not implement NetworkCaptureProvider (persistent_profile), so whether its "
            f"browser starts from a fresh profile (§5.6 logged_in) cannot be known"
        )
    return problems


def _replayability_problems(replayability: Any, backend: Any, source: Any) -> list[str]:
    tier = _tier_name(replayability)
    problems: list[str] = []
    if replayability.needs_archive_clock and not source.intercepts:
        problems.append(
            f"{tier} base_time=from_archive ↔ {_source_name(source)}: T0 can come from an "
            f"archive only when the run replays one (network_source: archive)"
        )
    launch_args = replayability.launch_args()
    if backend is None or not (launch_args or replayability.launch_env()):
        return problems
    provider = _provider_name(backend)
    if LAUNCH_ARGS not in getattr(type(backend), "__capabilities__", set()):
        problems.append(
            f"{tier} ↔ {provider}: the provider does not declare LAUNCH_ARGS, so the "
            f"tier's launch switches and environment cannot reach the browser"
        )
        return problems
    if not isinstance(backend, LaunchArgsProvider):
        problems.append(
            f"{tier} ↔ {provider}: the provider declares LAUNCH_ARGS but does not "
            f"implement LaunchArgsProvider (open(launch_args=..., launch_env=...) / "
            f"launch_arg_conflicts / planned_launch_flags / planned_browser_defaults)"
        )
        return problems
    problems.extend(
        f"{tier} ↔ {provider} knob {conflict}"
        for conflict in backend.launch_arg_conflicts(launch_args)
    )
    profile = persistent_profile(backend)
    if profile is not None:
        problems.append(
            f"{tier} ↔ {provider} {profile}: the tier needs a fresh profile for every run"
        )
    return problems


def _archiver_rows(
    cap: CooperatingCapability, archivers: Sequence[BrowserArchiverBase], backend: Any
) -> list[str]:
    """The rows only archivers have (ownership aside)."""
    problems: list[str] = []
    tier = _tier_name(cap.replayability)
    for archiver in archivers:
        if archiver.requires_replayable and not cap.replayability.replayable:
            problems.append(
                f"{_archiver_name(archiver)} require_replayable=true ↔ {tier}: an archive "
                f"recorded with unpinned randomness and clocks may not replay; choose a "
                f"replayable tier (pinned) or set require_replayable: false"
            )
    profile = persistent_profile(backend)
    if profile is not None:
        provider = _provider_name(backend)
        problems.extend(
            f"{_archiver_name(archiver)} allow_session_state=false ↔ {provider} {profile}: "
            f"the browser does not start from a fresh profile, so the evidence may carry "
            f"logged-in session state"
            for archiver in archivers
            if not archiver.allow_session_state
        )
    excluded = cap.profile_config.watchdogs_exclude
    if archivers and ArchiveWatchdog.__registry_key__ in excluded:
        problems.extend(
            f"{_archiver_name(archiver)} ↔ profile {cap.profile!r} watchdogs_exclude "
            f"{ArchiveWatchdog.__registry_key__!r}: nothing would drain the archive scope"
            for archiver in archivers
        )
    return problems


def _source_rows(
    cap: CooperatingCapability, archivers: Sequence[BrowserArchiverBase], backend: Any
) -> list[str]:
    """The network source's configuration rows: which archivers fit it, and isolation.

    A live source refuses an archiver that records a replay's log; an
    intercepting one needs exactly such an archiver, refuses one that
    records live traffic, and asks for isolation. *backend* is ``None`` when
    the browser is not the capability's own (explicit endpoint, injected
    client): the ownership rows refuse that.
    """
    source = cap.network_source
    name = _source_name(source)
    if not source.intercepts:
        return [
            f"{name} ↔ {_archiver_name(archiver)}: the archiver records a replay's serving "
            f"decisions, and a live source replays nothing; bind it with network_source: "
            f"archive, or leave it out of trace_writer.archive"
            for archiver in archivers
            if REPLAY_LOG_NEED in archiver.needs
        ]
    problems = [
        f"{name} ↔ {_archiver_name(archiver)}: the archiver records live traffic, and a "
        f"replay has none (every byte it serves is already in the source run's archive); "
        f"bind {REPLAY_LOG_ARCHIVER} instead, which records the replay's decisions"
        for archiver in archivers
        if LIVE_TRAFFIC_NEED in archiver.needs
    ]
    if not any(REPLAY_LOG_NEED in archiver.needs for archiver in archivers):
        bound = sorted(archiver.archiver_id for archiver in archivers)
        problems.append(
            f"{name} ↔ bound archivers {bound}: none records the replay log (the "
            f"{REPLAY_LOG_NEED!r} need: {REPLAY_LOG_ARCHIVER}); a replay must record every "
            f"miss — bind {REPLAY_LOG_ARCHIVER} on the trace writer"
        )
    if source.isolation != "required" or backend is None:
        return problems
    provider = _provider_name(backend)
    declared = getattr(type(backend), "__capabilities__", set())
    if NETWORK_ISOLATION not in declared or LAUNCH_ARGS not in declared:
        problems.append(
            f"{name} isolation=required ↔ {provider}: the provider does not declare "
            f"NETWORK_ISOLATION and LAUNCH_ARGS, so the browser cannot be cut off from the "
            f"network; use a provider that can (local_chrome) or write "
            f"isolation: none_acknowledged"
        )
    elif not isinstance(backend, NetworkIsolatingProvider):
        problems.append(
            f"{name} isolation=required ↔ {provider}: the provider declares "
            f"NETWORK_ISOLATION but does not implement NetworkIsolatingProvider "
            f"(isolation_conflicts)"
        )
    else:
        problems.extend(
            f"{name} isolation=required ↔ {provider} knob {conflict}"
            for conflict in backend.isolation_conflicts()
        )
    return problems


def binding_problems(
    cap: CooperatingCapability,
    archivers: Sequence[BrowserArchiverBase],
    *,
    cdp_url: str | None,
    injected_client: bool,
) -> list[str]:
    """The rows binding *archivers* decides — what ``bind_archivers`` refuses before it binds.

    ``cdp_url`` is the endpoint in effect: ``bind_archivers`` passes the
    capability's ``cdp_url`` knob; :func:`cooperation_problems` (``prepare()``
    through ``check_setup``, and setup) passes the task context's when it
    names one. With any archiver bound, the network source's configuration
    rows (which archivers fit the source, isolation) are decided here too.
    """
    backend = cap.resolved_backend()
    problems: list[str] = []
    for archiver in archivers:
        problems.extend(
            _ownership_problems(
                _archiver_name(archiver),
                backend,
                cdp_url=cdp_url,
                injected_client=injected_client,
            )
        )
    owned = None if cdp_url or injected_client else backend
    problems.extend(_archiver_rows(cap, archivers, owned))
    if archivers:
        problems.extend(_source_rows(cap, archivers, owned))
    return problems


def cooperation_problems(
    cap: CooperatingCapability,
    *,
    cdp_url: str | None,
    injected_client: bool,
    replay_archive: str | None = None,
    archivers: Sequence[BrowserArchiverBase] | None = None,
) -> list[str]:
    """Every reason this capability's configuration cannot run, at once.

    ``cdp_url`` is the explicit endpoint in effect (the task context's or
    the ``cdp_url`` knob); ``injected_client`` whether a test injected the
    control client; ``replay_archive`` the task's source run directory, if
    any; ``archivers`` the archivers bound — or about to be bound, when
    ``prepare()`` checks before it binds — and by default the capability's
    current binding. Empty when everything cooperates.
    """
    bound = tuple(cap.bound_archivers if archivers is None else archivers)
    backend = None if cdp_url or injected_client else cap.resolved_backend()
    problems = binding_problems(cap, bound, cdp_url=cdp_url, injected_client=injected_client)
    source = cap.network_source
    if source.intercepts:
        problems.extend(
            _ownership_problems(
                _source_name(source),
                cap.resolved_backend(),
                cdp_url=cdp_url,
                injected_client=injected_client,
            )
        )
        if not bound:
            problems.extend(_source_rows(cap, (), backend))
        if not replay_archive:
            problems.append(
                f"{_source_name(source)} ↔ task context: no replay_archive — the source run "
                f"directory to replay (run_cli --replay-archive DIR)"
            )
    elif replay_archive:
        problems.append(
            f"{_source_name(source)} ↔ replay_archive={replay_archive!r}: a live source "
            f"replays nothing; configure network_source: archive to replay that run"
        )
    if cap.replayability.replayable:
        problems.extend(
            _ownership_problems(
                _tier_name(cap.replayability),
                cap.resolved_backend(),
                cdp_url=cdp_url,
                injected_client=injected_client,
            )
        )
        problems.extend(_replayability_problems(cap.replayability, backend, source))
    return problems


def launch_args(cap: CooperatingCapability) -> tuple[str, ...]:
    """The launch switches this configuration hands the provider: the tier's, then isolation's.

    The isolation switches only for an intercepting source that requires
    them, and only to a provider declaring ``NETWORK_ISOLATION`` (the
    cooperation check refused any other); never with ``none_acknowledged``.
    """
    args = tuple(cap.replayability.launch_args())
    source = cap.network_source
    backend = cap.resolved_backend()
    if (
        source.intercepts
        and source.isolation == "required"
        and NETWORK_ISOLATION in getattr(type(backend), "__capabilities__", set())
    ):
        args = (*args, *NETWORK_ISOLATION_ARGS)
    return args


def launch_env(cap: CooperatingCapability) -> dict[str, str]:
    """The environment this configuration hands the provider with :func:`launch_args`: the tier's."""
    return dict(cap.replayability.launch_env())


def _launching_provider(
    cap: CooperatingCapability, *, cdp_url: str | None, injected_client: bool
) -> LaunchArgsProvider | None:
    """The provider that will launch this configuration's browser with known switches, if any."""
    if cdp_url or injected_client:
        return None
    backend = cap.resolved_backend()
    if LAUNCH_ARGS not in getattr(type(backend), "__capabilities__", set()) or not isinstance(
        backend, LaunchArgsProvider
    ):
        return None
    return backend


def planned_launch_flags(
    cap: CooperatingCapability, *, cdp_url: str | None, injected_client: bool
) -> list[str] | None:
    """The ``launch_flags`` the provider would record for this configuration — before launching.

    ``None`` when nothing would be launched with known flags: an explicit
    endpoint, an injected client, or a provider that does not take launch
    switches (``LAUNCH_ARGS`` / ``LaunchArgsProvider``). A replay compares
    these with its source run's in ``check_setup`` (``replay/consistency.py``)
    and records them in its fingerprint at setup.
    """
    backend = _launching_provider(cap, cdp_url=cdp_url, injected_client=injected_client)
    if backend is None:
        return None
    return backend.planned_launch_flags(launch_args(cap), launch_env(cap))


def planned_browser_defaults(
    cap: CooperatingCapability, *, cdp_url: str | None, injected_client: bool
) -> BrowserDefaults | None:
    """The time zone and locale this configuration's browser will fall back to, read before launch.

    ``None`` under the same conditions as :func:`planned_launch_flags`. A
    replay compares them with its source run's in ``check_setup``
    (``replay/consistency.py``).
    """
    backend = _launching_provider(cap, cdp_url=cdp_url, injected_client=injected_client)
    if backend is None:
        return None
    return backend.planned_browser_defaults(launch_env(cap))
