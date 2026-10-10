"""Browser archivers: configured instances, run-time sessions, and their context (§4.14).

The framework's ``ArchiverBase`` carries identity and declarations only; how a
capability captures is its own business (``core/archive/_registrar.py``). For
the browser that business has three parts, kept apart on purpose:

- :class:`BrowserArchiverBase` — the **configured instance**: YAML builds it,
  the trace hook holds it, ``bind_archivers`` hands it to the capability. It
  holds knobs and nothing else — never a session, a scope or a buffer — so
  one instance can be bound, unbound and bound again, run after run. It
  declares which capture channels it needs (:data:`ArchiveNeed`) and opens a
  run-time session from a context (:meth:`BrowserArchiverBase.open`, pure
  construction: no CDP call).
- :class:`BrowserArchiveSession` — the **run-time session**: a recorder
  participant (``recorder.RecorderParticipantBase``) that contributes commands
  to every new target, receives metadata events and pauses, and hands over
  :class:`~spectragent.core.archive.ArchiveFile` s on :meth:`collect`.
- :class:`BrowserArchiveContext` — the **run-time handles** a session may use,
  assembled by the capability at setup (the ``WatchdogContext`` pattern): the
  recorder connection, the archive channel, the raw-capture store, the tab
  list, the cycle counter, a clock, and the privacy facts.

The capability iterates over bound instances and their sessions without ever
branching on an archiver id or looking one up (receptor hygiene Rule 2).

.. note::
   No ``from __future__ import annotations`` in this package: archivers
   declare conscribe ``Annotated`` knobs (``af-trace-and-hooks`` §6).
"""

import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol

from spectragent.core.archive import ArchiveFile, ArchiverBase
from spectragent.core.capabilities.browser.archive.redaction import MARKER_BROWSER_REDACTED
from spectragent.core.capabilities.browser.dom.models import TargetCapture
from spectragent.core.capabilities.browser.recorder import (
    ArchiveChannel,
    RecorderParticipantBase,
    RecorderTarget,
)

#: A capture channel an archiver needs opened at setup.
#:
#: - ``fetch_response`` — the recorder's Response-stage ``Fetch`` (bodies);
#: - ``raw_dom`` — the DOM resolver's raw capture, parked on the observe cycle;
#: - ``observation_cycle`` — the observation cycles, delivered into the archive
#:   scope (a self-capture when no DOM fragment was negotiated);
#: - ``replay_log`` — the replay server's decisions (M30): an archiver with it
#:   records the replay log when the run replays an archive, and a replay
#:   refuses to run without one bound (the cooperation check finds it by this
#:   need, never by archiver id).
ArchiveNeed = Literal["fetch_response", "raw_dom", "observation_cycle", "replay_log"]


class RecorderHandle(Protocol):
    """What an archive session may do with the recorder connection."""

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Any: ...

    def target(self, session_id: str) -> RecorderTarget | None: ...

    def targets(self) -> dict[str, RecorderTarget]: ...

    def session_for_target(self, target_id: str) -> str | None: ...

    async def browser_version(self) -> dict[str, Any]: ...


class ReplayLogFeed(Protocol):
    """The replay server's log, as the archive side sees it (M30).

    Plain JSON dicts, ready to be written one per line — so ``archive/``
    never imports the replay package's entry type.
    """

    def drain(self) -> list[dict[str, Any]]:
        """Every entry since the last drain, in order; forgotten by the log."""
        ...


#: The ``cleaner`` key of a capture dom_raw made itself (no DOM negotiated).
SELF_CAPTURE = "self_capture"


@dataclass(frozen=True)
class ParkedRaw:
    """A raw capture parked on an observe cycle's pending slot, not committed yet."""

    capture: TargetCapture
    device_pixel_ratio: float
    captured_at: float
    source: Literal["observation", "archive"]


@dataclass(frozen=True)
class RawCapture:
    """One committed observation cycle's raw DOM capture, as the archive keeps it.

    ``capture`` is the very object the cycle produced (never mutated here);
    the rest is what the capability adds when the cycle commits (§4.12):
    which cycle and tab, the viewport, the DPR, the cleaner it was captured
    for and the document stamp.
    """

    capture: TargetCapture
    device_pixel_ratio: float
    captured_at: float
    cycle: int
    #: The tab's target id (``""`` when the capability did not know it).
    tab: str
    viewport: tuple[int, int]
    #: The cleaner key the capture was made for, or ``"self_capture"``.
    cleaner: str
    #: ``observation`` — the DOM resolver's own capture, handed over by its
    #: sink; ``archive`` — dom_raw's self-capture in a vision-only run.
    source: Literal["observation", "archive"]


class RawCaptureStore:
    """Raw captures of committed cycles, waiting for the next collection.

    The capability commits into it from ``_populate_cycle`` — only for a cycle
    that succeeded, so the archive holds exactly the capture the agent was
    served — and ``browser.dom_raw``'s session drains it at collection.
    """

    def __init__(self) -> None:
        self._committed: list[RawCapture] = []

    def commit(self, captures: Iterable[RawCapture]) -> None:
        self._committed.extend(captures)

    def drain(self) -> list[RawCapture]:
        """Everything committed since the last drain, in commit order; forgotten here."""
        drained, self._committed = self._committed, []
        return drained

    def __len__(self) -> int:
        return len(self._committed)


#: ``park_raw(control_session_id, capture, device_pixel_ratio)``: put a capture
#: into that tab's current observe-cycle slot (committed only if the cycle
#: succeeds).
ParkRawCapture = Callable[[str, TargetCapture, float], None]


@dataclass(frozen=True)
class BrowserArchiveContext:
    """The run-time handles of one recording, assembled by the capability at setup.

    Sessions read these; configured archivers never see them. Frozen: a
    session that needs run state keeps it on itself.
    """

    #: The recorder connection (send per session, the target map, the version).
    recorder: RecorderHandle
    #: The browser-level archive channel (scope, counters, barrier).
    channel: ArchiveChannel
    #: Committed raw DOM captures (drained by ``browser.dom_raw``).
    raw_captures: RawCaptureStore
    #: Route a self-captured raw DOM into the current cycle's slot.
    park_raw: ParkRawCapture
    #: The observation fragments this run negotiated (DOM in the cycle or not).
    fragments: frozenset[str]
    #: The configured viewport (CSS pixels).
    viewport: tuple[int, int]
    #: The current observation cycle (the environment's counter).
    cycle: Callable[[], int]
    #: Wall-clock seconds; injectable for tests.
    clock: Callable[[], float] = time.time
    #: Whether the browser started from a fresh profile (§5.6: session state).
    fresh_profile: bool = True
    #: The replay server's log when the run replays an archive (M30), else
    #: ``None``; an archiver needing ``replay_log`` records it then.
    replay_log: ReplayLogFeed | None = None

    def tabs(self) -> list[RecorderTarget]:
        """The page targets the recorder prepared, one per target, in attach order."""
        seen: set[str] = set()
        tabs: list[RecorderTarget] = []
        for target in self.recorder.targets().values():
            if target.type == "page" and target.target_id not in seen:
                seen.add(target.target_id)
                tabs.append(target)
        return tabs


class BrowserArchiveSession(RecorderParticipantBase):
    """One bound archiver's run-time session — a recorder participant.

    Every participant member (``target_commands``, ``fetch_patterns``,
    ``on_event``, ``on_paused``, ``on_cycle``, ``wants_cycles``) defaults to a
    no-op; :meth:`collect` hands over everything captured since the previous
    collection and forgets it, swapping its buffers in one synchronous
    statement; :meth:`aclose` releases anything still running.
    """

    def __init__(self, archiver: "BrowserArchiverBase", ctx: BrowserArchiveContext) -> None:
        self.archiver = archiver
        self.ctx = ctx

    @property
    def archiver_id(self) -> str:
        return self.archiver.archiver_id

    async def collect(self) -> list[ArchiveFile]:
        """Hand over what was captured since the last collection; raise if it cannot."""
        return []

    async def aclose(self) -> None:
        """Stop whatever this session still runs. Idempotent."""
        return None


class BrowserArchiverBase(ArchiverBase):
    """Base for the browser's archivers (``browser.<id>``).

    Concrete archivers hand-write ``__init__`` with their knobs as
    ``Annotated`` parameters — ``allow_session_state`` on every one of them —
    and implement :meth:`open`. What a run captures is decided by the bound
    instances' :attr:`needs`, read once at setup.
    """

    __abstract__ = True

    #: The capture channels this archiver needs opened at setup.
    needs: ClassVar[frozenset[ArchiveNeed]] = frozenset()

    #: Accept evidence from a browser that did not start from a fresh profile
    #: (``logged_in``, §5.6). Every concrete archiver sets it from its knob.
    allow_session_state: bool

    @property
    def requires_replayable(self) -> bool:
        """Whether binding this archiver needs a replayability tier other than ``off``."""
        return False

    @property
    def privacy_markers(self) -> frozenset[str]:
        """Every browser archiver applies the browser's redaction rules."""
        return frozenset({MARKER_BROWSER_REDACTED})

    def redaction_facts(self) -> Mapping[str, Any]:
        """What this archiver withholds, for the environment fingerprint (default: nothing extra)."""
        return {}

    def open(self, ctx: BrowserArchiveContext) -> BrowserArchiveSession:
        """This run's session. Pure construction: no CDP call, no task."""
        raise NotImplementedError(f"archiver {self.archiver_id!r} must open a session")


def archive_needs(archivers: Sequence[BrowserArchiverBase]) -> frozenset[ArchiveNeed]:
    """The union of what *archivers* need."""
    needs: set[ArchiveNeed] = set()
    for archiver in archivers:
        needs |= archiver.needs
    return frozenset(needs)


#: How the capability collects the fingerprint run record after the sessions.
FingerprintCollector = Callable[[], Awaitable[ArchiveFile | None]]
