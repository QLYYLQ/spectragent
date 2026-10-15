"""``browser.replay_log`` — a replay's serving decisions, one ``replay.jsonl`` per step (§4.14).

A run that replays an archive (``network_source: archive``) has no live
traffic to record: every byte it serves is already in the source run's
archive. What it does have is the replay server's log — every hit, exhausted
repeat, synthesised preflight and miss — and this archiver records it: its
session drains the log at each collection, in one synchronous call, into
that step's ``replay.jsonl``. An empty window writes nothing. The step whose
collection takes an entry is the step it is attributed to.

The archiver has one meaning, whatever else the run is configured with, and
so does ``browser.network`` (always live traffic). Whether the two fit the
network source is the cooperation check's business (``cooperation.py``), by
need and never by archiver id: an archive source needs an archiver with the
``replay_log`` need and refuses one that captures live responses
(``fetch_response``); a live source refuses one with ``replay_log``, because
it replays nothing.

The session contributes nothing to a target's preparation and consumes no
event (the recorder participant defaults). The entry type and the readers
live with the replay (``replay/replay_log.py``); this side sees plain JSON
dicts (:class:`~.base.ReplayLogFeed`), so the archive package never imports a
replay type.
"""

from typing import Annotated, ClassVar

from pydantic import Field

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.capabilities.browser.archive.base import (
    ArchiveNeed,
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
    ReplayLogFeed,
)
from spectragent.core.capabilities.browser.archive.encoding import jsonl

#: The archiver's key: the readers find a run's replay log by it.
REPLAY_LOG_ARCHIVER = "browser.replay_log"
#: The replay server's decisions of one step.
REPLAY_LOG_NAME = "replay.jsonl"


class ReplayLogArchiver(BrowserArchiverBase):
    """The replay server's decisions, step by step."""

    __registry_key__ = REPLAY_LOG_ARCHIVER
    needs: ClassVar[frozenset[ArchiveNeed]] = frozenset({"replay_log"})

    def __init__(
        self,
        allow_session_state: Annotated[
            bool,
            Field(
                description=(
                    "Accept replaying in a browser that did not start from a fresh profile "
                    "(logged-in state). Off: such a configuration is refused at binding."
                ),
            ),
        ] = False,
    ) -> None:
        self.allow_session_state = allow_session_state

    def open(self, ctx: BrowserArchiveContext) -> "ReplayLogSession":
        """The session that drains this run's replay log.

        Raises:
            RuntimeError: The run replays no archive, so there is no log to
                record (the cooperation check refuses that configuration
                before anything opens).
        """
        if ctx.replay_log is None:
            raise RuntimeError(
                f"archiver {self.archiver_id} has no replay log to record: this run does not "
                f"replay an archive (network_source: archive)"
            )
        return ReplayLogSession(self, ctx, ctx.replay_log)


class ReplayLogSession(BrowserArchiveSession):
    """One replaying run's log: each collection writes what the server decided since the last."""

    def __init__(
        self, archiver: ReplayLogArchiver, ctx: BrowserArchiveContext, log: ReplayLogFeed
    ) -> None:
        super().__init__(archiver, ctx)
        self._log = log

    async def collect(self) -> list[ArchiveFile]:
        entries = self._log.drain()
        if not entries:
            return []
        return [
            ArchiveFile(
                archiver_id=self.archiver.archiver_id,
                name=REPLAY_LOG_NAME,
                media_type="application/x-ndjson",
                data=jsonl(entries),
                meta=ArchiveProvenance(captured_at=self.ctx.clock()),
            )
        ]
