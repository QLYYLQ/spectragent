"""Can this browser configuration work at all? — the cooperation check (§4.14).

What *may* be configured is constrained by the generated config; whether
the chosen pieces can **cooperate** is decided here (the 2026-09-24 ruling),
in one function that reports every problem at once and names both parties
in each line — the strategy or archiver on one side, the provider or knob on
the other. ``BrowserCapability.setup`` runs it before it launches anything;
``bind_archivers`` runs the rows it can already decide at ``prepare()``.

The rows this module owns:

- **Ownership** — everything that opens the recorder connection (a bound
  browser archiver, ``replayability`` other than ``off``) needs a browser the
  capability launched itself: a provider declaring ``NETWORK_CAPTURE``, no
  explicit ``cdp_url``, no injected control client. A recorder holding
  ``waitForDebuggerOnStart`` on a browser it does not own can wedge that
  browser for everyone (S1).
- **Replayability** — a tier with launch switches needs a provider declaring
  ``LAUNCH_ARGS`` (the only way the switches reach Chrome), none of the
  provider's knobs may contradict them, the profile must be fresh, and a
  clock read from an archive needs an archive network source (M30).

- **Archivers** — every bound browser archiver opens the recorder connection
  (the ownership rows above), a ``require_replayable`` archiver refuses the
  ``off`` tier, a browser that did not start from a fresh profile needs
  ``allow_session_state`` on every bound archiver (§5.6: the definition of
  ``logged_in``), and a profile that excludes the ``archive`` watchdog leaves
  nothing to drain the archive scope. :func:`binding_problems` is this group
  alone: ``bind_archivers`` runs it at ``prepare()``, before it changes the
  binding.
"""

from collections.abc import Sequence
from typing import Any, Protocol

from spectragent.core.capabilities.browser.archive.base import BrowserArchiverBase
from spectragent.core.capabilities.browser.backends.base import (
    LaunchArgsProvider,
    NetworkCaptureProvider,
)
from spectragent.core.capabilities.browser.backends.capabilities import (
    LAUNCH_ARGS,
    NETWORK_CAPTURE,
)
from spectragent.core.capabilities.browser.profiles import BrowserProfileConfig
from spectragent.core.capabilities.browser.replay import ReplayabilityProtocol
from spectragent.core.capabilities.browser.watchdogs.archive_watchdog import ArchiveWatchdog


class CooperatingCapability(Protocol):
    """What the check reads off the capability — and nothing else."""

    @property
    def replayability(self) -> ReplayabilityProtocol: ...

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


def recorder_needs(cap: CooperatingCapability) -> list[str]:
    """Every configured party that needs the recorder connection, by name."""
    needs = [_archiver_name(archiver) for archiver in cap.bound_archivers]
    if cap.replayability.replayable:
        needs.append(_tier_name(cap.replayability))
    return needs


def persistent_profile(backend: Any) -> str | None:
    """Which knob or variable makes the provider's profile non-fresh, or ``None``.

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


def _replayability_problems(replayability: Any, backend: Any) -> list[str]:
    tier = _tier_name(replayability)
    problems: list[str] = []
    if replayability.needs_archive_clock:
        problems.append(
            f"{tier} base_time=from_archive ↔ network source live: T0 can come from an "
            f"archive only when the run replays one (archive network source, M30)"
        )
    launch_args = replayability.launch_args()
    if backend is None or not launch_args:
        return problems
    provider = _provider_name(backend)
    if LAUNCH_ARGS not in getattr(type(backend), "__capabilities__", set()):
        problems.append(
            f"{tier} ↔ {provider}: the provider does not declare LAUNCH_ARGS, so the "
            f"tier's launch switches cannot reach the browser"
        )
        return problems
    if not isinstance(backend, LaunchArgsProvider):
        problems.append(
            f"{tier} ↔ {provider}: the provider declares LAUNCH_ARGS but does not "
            f"implement LaunchArgsProvider (open(launch_args=...) / launch_arg_conflicts)"
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


def binding_problems(
    cap: CooperatingCapability,
    archivers: Sequence[BrowserArchiverBase],
    *,
    cdp_url: str | None,
    injected_client: bool,
) -> list[str]:
    """The rows binding *archivers* decides — what ``bind_archivers`` refuses at ``prepare()``.

    ``cdp_url`` is what is known by then: the capability's ``cdp_url`` knob
    (setup checks again with the task's).
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
    return problems


def cooperation_problems(
    cap: CooperatingCapability, *, cdp_url: str | None, injected_client: bool
) -> list[str]:
    """Every reason this capability's configuration cannot run, at once.

    ``cdp_url`` is the explicit endpoint in effect (setup's argument or the
    ``cdp_url`` knob); ``injected_client`` whether a test injected the
    control client. Empty when everything cooperates.
    """
    backend = None if cdp_url or injected_client else cap.resolved_backend()
    problems = binding_problems(
        cap, cap.bound_archivers, cdp_url=cdp_url, injected_client=injected_client
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
        problems.extend(_replayability_problems(cap.replayability, backend))
    return problems
