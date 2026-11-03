"""Android archivers: configured instances, run-time sessions, and their context (§11.2).

The framework's ``ArchiverBase`` carries identity and declarations only; how a
capability captures is its own business (``core/archive/_registrar.py``). The
Android capability keeps the browser's three parts apart (§4.14):

- :class:`AndroidArchiverBase`, the **configured instance**: YAML builds it,
  the trace hook holds it, ``bind_archivers`` hands it to the capability. It
  holds knobs only, never a session or a buffer. It declares what it needs of
  the capture (:data:`AndroidArchiveNeed`) and opens a run-time session from a
  context (:meth:`AndroidArchiverBase.open`, pure construction).
- :class:`AndroidArchiveSession`, the **run-time session**: on
  :meth:`~AndroidArchiveSession.collect` it hands over its
  :class:`~spectragent.core.archive.ArchiveFile` s, and names what it could not
  take as gaps.
- :class:`AndroidArchiveContext`, the **run-time handles** a session may use,
  assembled by the capability at setup: today the trees of successful
  captures, and the ones a capture failed to take (:class:`TreeStore`).

The capability reads the bound archivers' needs and never branches on an
archiver's id. No ``from __future__ import annotations`` here or in an
archiver's module: conscribe reads an archiver's ``Annotated`` knobs.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

from spectragent.core.archive import ArchiveBatch, ArchiverBase
from spectragent.core.capabilities.android.device.clients import TreeCapture
from spectragent.core.capabilities.android.ui_tree._registrar import ScreenSize

#: What an archiver needs of the capture.
#:
#: ``ui_tree``: the UI tree of every capture that succeeded, as its source
#: returned it. In a run that does not negotiate ``android.ui_tree`` the
#: capability captures the tree for the archive alone, beside the capture's
#: other sources, and the agent never sees it (user ruling 2026-11-03, §11.2);
#: when that capture fails, the archive misses the tree and the observation
#: goes on.
AndroidArchiveNeed = Literal["ui_tree"]


@dataclass(frozen=True)
class CommittedTree:
    """The UI tree of one capture that succeeded, as the capability hands it to the archive side.

    Committed only once every fragment of the capture was built: the archive
    holds the tree of every observation the agent was served, and of no other.
    """

    #: The source's answer, untouched: the whole forest, its node count, the
    #: off-screen subtrees it left out, what it cost on the device.
    capture: TreeCapture
    #: The tree source that captured it (its registry key, the fragment's ``producer``).
    producer: str
    #: The screen, in pixels, the capture was cleaned against (or would be, in
    #: a run that negotiates no tree).
    screen: ScreenSize
    #: The observation cycle of the capture (the environment's counter).
    cycle: int
    #: Wall-clock seconds: when the capture's ``android.ui_tree`` was made, or
    #: when the tree captured for the archive alone came back.
    captured_at: float


@dataclass(frozen=True)
class MissingTree:
    """A capture that succeeded without the tree it took for the archive alone, and why."""

    #: The observation cycle of the capture.
    cycle: int
    #: Why the tree did not come back.
    error: Exception


class TreeStore:
    """The trees of successful captures, and the ones they missed, waiting for the next collection.

    The capability commits into it while a bound archiver needs ``ui_tree``;
    that archiver's session drains it when the trace hook collects.
    """

    def __init__(self) -> None:
        self._entries: list[CommittedTree | MissingTree] = []

    def commit(self, entry: CommittedTree | MissingTree) -> None:
        self._entries.append(entry)

    def drain(self) -> list[CommittedTree | MissingTree]:
        """Everything committed since the last drain, in order; forgotten here."""
        drained, self._entries = self._entries, []
        return drained


@dataclass(frozen=True)
class AndroidArchiveContext:
    """The run-time handles of one recording, assembled by the capability at setup.

    Sessions read these; configured archivers never see them.
    """

    #: The trees of successful captures, and the ones missed, for the archivers that need ``ui_tree``.
    trees: TreeStore


class AndroidArchiveSession:
    """One bound archiver's run-time session, from setup to teardown."""

    def __init__(self, archiver: "AndroidArchiverBase", ctx: AndroidArchiveContext) -> None:
        self.archiver = archiver
        self.ctx = ctx

    @property
    def archiver_id(self) -> str:
        return self.archiver.archiver_id

    async def collect(self) -> ArchiveBatch:
        """Hand over what was captured since the last collection, and forget it.

        What it could not take is named in the batch's gaps under this
        archiver's key; raise only when it can hand over nothing.
        """
        return ArchiveBatch()


class AndroidArchiverBase(ArchiverBase):
    """Base for the Android capability's archivers (``android.<id>``).

    A concrete archiver declares :attr:`needs`, takes its knobs as
    ``Annotated`` constructor parameters, and implements :meth:`open`.
    """

    __abstract__ = True

    #: What this archiver needs of the capture.
    needs: ClassVar[frozenset[AndroidArchiveNeed]] = frozenset()

    def open(self, ctx: AndroidArchiveContext) -> AndroidArchiveSession:
        """This run's session. Pure construction: no device call."""
        raise NotImplementedError(f"archiver {self.archiver_id!r} must open a session")


def android_archivers(archivers: Sequence[ArchiverBase]) -> list[AndroidArchiverBase]:
    """*archivers* as Android archivers, in order; anything else refused by name.

    Raises:
        TypeError: An archiver that is not an ``AndroidArchiverBase``.
    """
    foreign = sorted(a.archiver_id for a in archivers if not isinstance(a, AndroidArchiverBase))
    if foreign:
        raise TypeError(
            f"AndroidCapability can bind only Android archivers; got {foreign} "
            "(not AndroidArchiverBase)."
        )
    return [a for a in archivers if isinstance(a, AndroidArchiverBase)]
