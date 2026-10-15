"""browser.storage: immutable initial seed and partitioned step-boundary state.

Cookies belong to the run context, local storage to its actual Chrome storage
key, and session storage to logical tab plus storage key. OOPIF reads use their
own sessions. Values are preserved, with explicit failure evidence and coverage.
"""

from typing import Annotated, ClassVar

from pydantic import Field

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.archive.privacy import MARKER_SESSION_STATE
from spectragent.core.capabilities.browser.archive.base import (
    ArchiveNeed,
    BrowserArchiveContext,
    BrowserArchiverBase,
    BrowserArchiveSession,
)
from spectragent.core.capabilities.browser.archive.encoding import canonical_json
from spectragent.core.capabilities.browser.archive.redaction import (
    MARKER_BROWSER_REDACTED,
    browser_ledger,
)
from spectragent.core.capabilities.browser.storage_capture import BrowserStorageCapture
from spectragent.core.capabilities.browser.storage_seed import INITIAL_STATE_NAME

STORAGE_NAME = "storage.json"


class StorageArchiver(BrowserArchiverBase):
    """Cookies and local / session storage at every step boundary."""

    __registry_key__ = "browser.storage"
    # No capture channel: everything is read at collection.
    needs: ClassVar[frozenset[ArchiveNeed]] = frozenset()

    def __init__(
        self,
        allow_session_state: Annotated[
            bool,
            Field(
                description=(
                    "Accept recording a browser that did not start from a fresh profile "
                    "(logged-in pages). Cookie and Web Storage values are stored as they "
                    "are either way."
                ),
            ),
        ] = False,
        command_timeout_s: Annotated[
            float,
            Field(gt=0, description="Seconds each storage read on the recorder may take."),
        ] = 1.0,
    ) -> None:
        self.allow_session_state = allow_session_state
        self.command_timeout_s = command_timeout_s

    @property
    def privacy_markers(self) -> frozenset[str]:
        # Cookie and Web Storage values are stored as they are: the evidence
        # carries session state.
        return frozenset({MARKER_BROWSER_REDACTED, MARKER_SESSION_STATE})

    def open(self, ctx: BrowserArchiveContext) -> "StorageSession":
        return StorageSession(self, ctx)


class StorageSession(BrowserArchiveSession):
    """Read partitioned storage and hand over its immutable pre-navigation seed."""

    archiver: StorageArchiver

    def __init__(self, archiver: StorageArchiver, ctx: BrowserArchiveContext) -> None:
        super().__init__(archiver, ctx)
        self._last: bytes | None = None
        self._initial_sent = False
        self._capture = BrowserStorageCapture(
            lambda *args, **kwargs: ctx.recorder.send(*args, **kwargs),
            ctx.recorder.targets,
            ctx.browser_context_id,
            ctx.storage_tabs,
        )

    async def collect(self) -> list[ArchiveFile]:
        captured_at = self.ctx.clock()
        state = await self._capture.read(timeout=self.archiver.command_timeout_s)
        key = canonical_json(state.model_dump(mode="json"))
        changed = key != self._last
        self._last = key
        files: list[ArchiveFile] = []
        if changed:
            document = {
                "captured_at": captured_at,
                **state.model_dump(mode="json"),
                "redaction": browser_ledger([]).model_dump(mode="json"),
            }
            files.append(
                ArchiveFile(
                    archiver_id=self.archiver_id,
                    name=STORAGE_NAME,
                    media_type="application/json",
                    data=canonical_json(document),
                    meta=ArchiveProvenance(captured_at=captured_at),
                )
            )
        if self.ctx.initial_storage is not None and (changed or not self._initial_sent):
            files.append(
                ArchiveFile(
                    archiver_id=self.archiver_id,
                    name=INITIAL_STATE_NAME,
                    media_type="application/json",
                    data=self.ctx.initial_storage.data,
                    content_addressed=True,
                    meta=ArchiveProvenance(captured_at=captured_at),
                )
            )
            self._initial_sent = True
        return files
