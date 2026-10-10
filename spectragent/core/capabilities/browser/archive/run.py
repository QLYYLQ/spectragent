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
- :meth:`BrowserArchiveRun.collect` is ``collect_archive`` after the
  capability's liveness check: the channel barrier (every metadata event
  emitted so far was handled, or the collection fails), then each session in
  binding order, then the fingerprint run record. Every step is bounded; a
  step that cannot collect raises.

.. note::
   No ``from __future__ import annotations`` — the archive package rule.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from spectragent.core.archive import ArchiveBatch, ArchiveFile
from spectragent.core.capabilities.browser.archive.base import (
    SELF_CAPTURE,
    ArchiveNeed,
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
    ParkedRaw,
    RawCapture,
    RawCaptureStore,
    archive_needs,
)
from spectragent.core.capabilities.browser.archive.fingerprint import BrowserFingerprint
from spectragent.core.capabilities.browser.recorder import ArchiveChannel, ArchiveChannelError

logger = logging.getLogger(__name__)

#: Seconds the collection barrier may wait for the archive scope to drain.
BARRIER_TIMEOUT_S = 5.0


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
        self._closed = False

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

    # ── collection ────────────────────────────────────────────────────

    async def collect(
        self,
        *,
        plan_fingerprint: Mapping[str, Any],
        session_metadata: Mapping[str, Any] | None,
        fresh_profile: bool,
        clock: Callable[[], float],
        network_fingerprint: Mapping[str, Any] | None = None,
    ) -> ArchiveBatch:
        """Barrier → each session in binding order → fingerprint → one batch.

        *network_fingerprint* is the network source's block (``None``: live).
        """
        ctx = self._ctx
        if ctx is None or not self._sessions:
            return ArchiveBatch()
        await self.channel.barrier(timeout=BARRIER_TIMEOUT_S)
        if self.channel.errors > self._errors_seen:
            failed = self.channel.errors - self._errors_seen
            self._errors_seen = self.channel.errors
            raise ArchiveChannelError(
                f"{failed} archive event handler(s) failed since the last collection "
                f"(see the log); this collection would be incomplete"
            )
        files: list[ArchiveFile] = []
        for _, session in self._sessions:
            files.extend(await session.collect())
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
        )
        if record is not None:
            files.append(record)
        return ArchiveBatch(tuple(files))

    async def aclose(self) -> None:
        """Close every session, bound or retired. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for session in [*self.sessions, *self._retired]:
            try:
                await session.aclose()
            except Exception as exc:  # pragma: no cover — sessions close tolerantly
                logger.warning("archive: closing %s failed: %s", type(session).__name__, exc)
        self._sessions.clear()
        self._retired.clear()
