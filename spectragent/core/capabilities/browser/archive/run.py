"""One recording's archive side: channel, sessions, raw store, fingerprint (§4.14).

``BrowserCapability`` owns one :class:`BrowserArchiveRun` from setup to
teardown, and only when archivers are bound at setup. The capability wires
(it creates the browser-level ``__archive__`` scope and hands this object the
channel and the context); everything the archive side *does* lives here and in
the sessions:

- :meth:`BrowserArchiveRun.bind` opens a session for each bound archiver
  (pure construction) and keeps the binding order, which is the collection
  order. Binding again after setup changes who receives events and who is
  collected; it opens no capture channel that setup did not (the runtime
  binds before setup).
- :meth:`BrowserArchiveRun.cycle_committed` tells every session that an
  observation cycle committed (after :meth:`BrowserArchiveRun.commit_raw`).
- :meth:`BrowserArchiveRun.action_boundary` announces each ``env.act`` call
  before it touches the page, numbered since the observation cycle, and hands
  it to the sessions that want boundaries (the ``action_boundary`` channel)
  directly, in the action's own task, each within its budget.
- :meth:`BrowserArchiveRun.collect` is ``collect_archive`` after the
  capability's liveness check: the channel flush (every metadata event
  emitted so far was handled, or the batch names a gap for the whole
  capability), then each session in binding order, then the fingerprint run
  record. Every step is bounded. A session that cannot collect leaves a gap
  under its archiver and takes nobody else's evidence with it; only a
  fingerprint that cannot be measured fails the collection, because every
  batch with step evidence carries its fingerprint.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from spectragent.core.archive import ArchiveBatch, ArchiveFile, ArchiveGap
from spectragent.core.capabilities.browser.archive.base import (
    SELF_CAPTURE,
    ActionBoundary,
    ArchiveNeed,
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
    CommittedCycle,
    ParkedRaw,
    RawCapture,
    RawCaptureStore,
    archive_needs,
)
from spectragent.core.capabilities.browser.archive.fingerprint import BrowserFingerprint
from spectragent.core.capabilities.browser.backends.browser_defaults import BrowserDefaults
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.capabilities.browser.recorder import ArchiveChannel, ArchiveChannelError

logger = logging.getLogger(__name__)

#: Seconds the collection flush may wait for the archive scope to drain.
FLUSH_TIMEOUT_S = 5.0
#: Seconds an action waits for a session beyond the session's own budget
#: (:attr:`BrowserArchiveSession.action_boundary_budget_s`) before the run
#: cancels it and lets the action run.
ACTION_BOUNDARY_GRACE_S = 0.5
#: The budget of a session that wants action boundaries and declares none.
DEFAULT_ACTION_BOUNDARY_BUDGET_S = 1.0


class BrowserArchiveRun:
    """The archive side of one recording (setup → teardown)."""

    def __init__(
        self,
        *,
        channel: ArchiveChannel,
        archivers: Sequence[BrowserArchiverBase],
    ) -> None:
        self.channel = channel
        #: What the capture channels were opened for, fixed at setup.
        self.needs: frozenset[ArchiveNeed] = archive_needs(archivers)
        self.raw_captures = RawCaptureStore()
        self.fingerprint = BrowserFingerprint()
        self._ctx: BrowserArchiveContext | None = None
        self._sessions: list[tuple[BrowserArchiverBase, BrowserArchiveSession]] = []
        #: Sessions unbound mid-run; closed with the run.
        self._retired: list[BrowserArchiveSession] = []
        self._errors_seen = 0
        self._raw_failures: list[str] = []
        self._closed = False
        #: The cycle the boundary count belongs to, and the calls announced in it.
        self._boundary_cycle: int | None = None
        self._boundaries = 0

    # ── binding ───────────────────────────────────────────────────────

    def start(self, ctx: BrowserArchiveContext, archivers: Sequence[BrowserArchiverBase]) -> None:
        """Adopt the run's context and open the sessions of the setup-time binding."""
        self._ctx = ctx
        self.bind(archivers)

    def bind(self, archivers: Sequence[BrowserArchiverBase]) -> None:
        """Make *archivers* the bound set: keep their sessions, open new ones, retire the rest."""
        if self._ctx is None:
            raise RuntimeError("BrowserArchiveRun.bind() before start()")
        existing = {id(archiver): session for archiver, session in self._sessions}
        sessions: list[tuple[BrowserArchiverBase, BrowserArchiveSession]] = []
        for archiver in archivers:
            session = existing.pop(id(archiver), None) or archiver.open(self._ctx)
            sessions.append((archiver, session))
        self._retired.extend(existing.values())
        self._sessions = sessions

    @property
    def archivers(self) -> tuple[BrowserArchiverBase, ...]:
        return tuple(archiver for archiver, _ in self._sessions)

    @property
    def sessions(self) -> tuple[BrowserArchiveSession, ...]:
        return tuple(session for _, session in self._sessions)

    def wants(self, need: ArchiveNeed) -> bool:
        """Whether a *currently bound* archiver needs *need* (and setup opened it)."""
        return need in self.needs and any(need in archiver.needs for archiver, _ in self._sessions)

    def commit_raw(
        self,
        parked: Sequence[ParkedRaw],
        *,
        cycle: int,
        tab: str,
        viewport: tuple[int, int],
        cleaner: str,
    ) -> None:
        """Commit a successful cycle's parked raw captures, with what the cycle knows (§4.12)."""
        if not self.wants("raw_dom"):
            return
        if not parked:
            self._raw_failures.append(f"cycle {cycle} of tab {tab!r} has no raw DOM capture")
            return
        self.raw_captures.commit(
            RawCapture(
                capture=item.capture,
                device_pixel_ratio=item.device_pixel_ratio,
                captured_at=item.captured_at,
                cycle=cycle,
                tab=tab,
                viewport=viewport,
                cleaner=cleaner if item.source == "observation" else SELF_CAPTURE,
                source=item.source,
            )
            for item in parked
        )

    def cycle_committed(self, *, cycle: int, tab: str, control_session_id: str) -> None:
        """Tell every bound session that observation cycle *cycle* committed on *tab*."""
        committed = CommittedCycle(
            cycle=cycle,
            tab=tab,
            control_session_id=control_session_id,
            committed_at=time.time(),
        )
        for _, session in self._sessions:
            try:
                session.on_cycle_committed(committed)
            except Exception:
                # Counted like a failed handler: the next collection fails
                # rather than look complete without what this session missed.
                self.channel.errors += 1
                logger.exception("archive: %s failed on committed cycle %d", session, cycle)

    async def action_boundary(
        self,
        *,
        tab: str | None,
        control_session_id: str,
        action: str,
        tab_open: bool = True,
    ) -> None:
        """Announce one ``env.act`` call on *tab* before it touches the page.

        Every call is numbered, since the observation cycle began, whether or
        not a bound archiver wants boundaries — so ``action_index`` is the
        ordinal of the call, the index into that step's ``action_spans``.
        Each session that wants boundaries gets the :class:`ActionBoundary`
        in this, the action's own task, one after the other; the run cancels
        one that holds the action past its
        :attr:`~.base.BrowserArchiveSession.action_boundary_budget_s` plus
        :data:`ACTION_BOUNDARY_GRACE_S` and lets the action run. What that
        costs is the session's evidence to record (``browser.frames`` lists
        a ``timeout`` entry); the collection, the step and the action never
        pay for it.
        """
        ctx = self._ctx
        if ctx is None:
            return
        cycle = ctx.cycle()
        if cycle != self._boundary_cycle:
            self._boundary_cycle, self._boundaries = cycle, 0
        index = self._boundaries
        self._boundaries += 1
        if not self.wants("action_boundary"):
            return
        wanting = [session for session in self.sessions if session.wants_action_boundaries]
        if not wanting:
            return
        boundary = ActionBoundary(
            target_id=tab,
            control_session_id=control_session_id,
            action=action,
            requested_at=time.time(),
            cycle=cycle,
            action_index=index,
            tab_open=tab_open,
        )
        for session in wanting:
            budget = (
                session.action_boundary_budget_s or DEFAULT_ACTION_BOUNDARY_BUDGET_S
            ) + ACTION_BOUNDARY_GRACE_S
            try:
                async with asyncio.timeout(budget):
                    await session.on_action_boundary(boundary)
            except TimeoutError:
                logger.warning(
                    "archive: %s held %s past %gs; the action runs without it",
                    session.archiver_id,
                    action,
                    budget,
                )
            except Exception:
                logger.exception(
                    "archive: %s failed on the boundary of %s", session.archiver_id, action
                )

    # ── collection ────────────────────────────────────────────────────

    async def collect(
        self,
        *,
        plan_fingerprint: Mapping[str, Any],
        session_metadata: Mapping[str, Any] | None,
        fresh_profile: bool,
        clock: Callable[[], float],
        network_fingerprint: Mapping[str, Any] | None = None,
        browser_defaults: BrowserDefaults | None = None,
        input_platform: InputPlatform | None = None,
        initial_storage: Mapping[str, Any] | None = None,
    ) -> ArchiveBatch:
        """Flush → each session in binding order → fingerprint → one batch.

        *network_fingerprint* is the network source's block (``None``: live);
        *browser_defaults* what the capability measured right after launch.

        What fails is named, not spread: a channel that lost metadata (a
        flush that did not settle, an event handler that raised) is a gap of
        the whole capability, since its loss is not one archiver's; a cycle
        whose raw DOM never arrived is a gap of each bound archiver that
        needs ``raw_dom``; a session whose ``collect`` raises is a gap of its
        archiver. Every other session still hands over what it holds. The
        fingerprint is not optional: a batch with step evidence carries it
        (§4.14), so one that cannot be measured raises and the environment
        records the whole capability's collection as failed.
        """
        ctx = self._ctx
        if ctx is None or not self._sessions:
            return ArchiveBatch()
        capability = self.fingerprint.capability_id
        gaps: list[ArchiveGap] = []
        try:
            await self.channel.flush(timeout=FLUSH_TIMEOUT_S)
        except Exception as exc:
            gaps.append(ArchiveGap.of(capability, exc))
        if self.channel.errors > self._errors_seen:
            failed = self.channel.errors - self._errors_seen
            self._errors_seen = self.channel.errors
            gaps.append(
                ArchiveGap.of(
                    capability,
                    ArchiveChannelError(
                        f"{failed} archive event handler(s) failed since the last collection "
                        f"(see the log); what this collection hands over may be incomplete"
                    ),
                )
            )
        if self._raw_failures:
            failures, self._raw_failures = self._raw_failures, []
            missing = ArchiveChannelError(
                "raw DOM missing for committed cycles: " + "; ".join(failures)
            )
            wanting = [a.archiver_id for a, _ in self._sessions if "raw_dom" in a.needs]
            gaps.extend(ArchiveGap.of(key, missing) for key in wanting or [capability])
        files: list[ArchiveFile] = []
        for _, session in self._sessions:
            try:
                files.extend(await session.collect())
            except Exception as exc:
                gaps.append(ArchiveGap.of(session.archiver_id, exc))
        record = await self.fingerprint.collect(
            recorder=ctx.recorder,
            tabs=ctx.tabs(),
            plan_fingerprint=plan_fingerprint,
            viewport=ctx.viewport,
            session_metadata=session_metadata,
            archivers=self.archivers,
            fresh_profile=fresh_profile,
            clock=clock,
            network_fingerprint=network_fingerprint,
            browser_defaults=browser_defaults,
            input_platform=input_platform,
            initial_storage=initial_storage,
            include_unchanged=bool(files),
        )
        if record is not None:
            files.append(record)
        return ArchiveBatch(tuple(files), tuple(gaps))

    async def aclose(self) -> None:
        """Attempt every session; keep failed resources reachable for a retry."""
        if self._closed:
            return
        errors: list[BaseException] = []
        unfinished: list[BrowserArchiveSession] = []
        for session in [*self.sessions, *self._retired]:
            try:
                await session.aclose()
            except (Exception, asyncio.CancelledError) as exc:
                exc.add_note(f"closing browser archive session {type(session).__name__}")
                errors.append(exc)
                unfinished.append(session)
        self._sessions.clear()
        self._retired = unfinished
        self._closed = not unfinished
        cancellation = next(
            (error for error in errors if isinstance(error, asyncio.CancelledError)), None
        )
        if cancellation is not None:
            for error in errors:
                if error is not cancellation:
                    cancellation.add_note(f"Archive cleanup also failed: {error!r}")
            raise cancellation
        if errors:
            raise BaseExceptionGroup("Browser archive cleanup failed", errors)
