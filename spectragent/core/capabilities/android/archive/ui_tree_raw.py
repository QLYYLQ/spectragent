"""``android.ui_tree_raw``: the whole UI tree of every capture that succeeded (§11.2).

``android.ui_tree`` holds the cleaned tree. The cleaner drops nodes, and a
dropped node cannot be brought back, so this archiver keeps the whole tree the
source captured (the views that are not important for accessibility, the view
ids, the off-screen nodes up to the source's node limit), and another cleaner
can clean it again offline (§11.2, user ruling 2026-11-01). The capability
hands a tree over once every fragment of its capture was built
(``AndroidCapability``), so a capture that failed leaves nothing here:

- In a run that negotiates ``android.ui_tree``, it is the very answer the
  capture cleaned. The archiver asks the device for nothing, and a capture
  takes as long with it as without it.
- In a run that does not (an agent that reads only the screen), the capability
  asks the tree source in the same capture for the archive alone, as
  ``browser.dom_raw`` captures the DOM for UI-TARS, and the agent never sees
  the tree (user ruling 2026-11-03: forking and resuming compare screens with
  it). When that ask fails, the step's archive names the missing tree as a gap
  and the observation goes on.

One gzip JSON file per tree (:class:`RawUITree`): the forest in android_env's
proto3 JSON form, as our service sends it (``device/forest.py``), beside the
tree source, the screen size the capture was cleaned against and the number of
off-screen subtrees the source left out. A cleaner needs the forest and the
screen size. The cycle and the capture time are the file's provenance, not its
content, and the file is content-addressed: a screen that did not change
between two captures is stored once. Encoding runs when the trace hook
collects, after the step is sealed. Settings' licence page on the API 33
emulator (2026-11-03), 5147 nodes at the source's default limit of 5000: 1.8 MB
of JSON archived in 52 KB, collected in 35 ms; the device took 1.1 s to walk it.

Read a run's trees back with :func:`raw_ui_trees` (``docs/architecture_design.md``
§11.2 has how to clean them again).
"""

import gzip
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from spectragent.core.archive import ArchiveBatch, ArchiveFile, ArchiveGap, ArchiveProvenance
from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.android.archive.base import (
    AndroidArchiveContext,
    AndroidArchiveNeed,
    AndroidArchiverBase,
    AndroidArchiveSession,
    CommittedTree,
    MissingTree,
)
from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityForest
from spectragent.core.capabilities.android.ui_tree._registrar import ScreenSize

KEY = "android.ui_tree_raw"


class RawUITree(BaseModel):
    """One archived UI tree: the forest as its source captured it, and what cleaning it needs.

    :meth:`encode` writes the forest in android_env's proto3 JSON form (proto
    field names, enums by name, fields at their default left out), which is
    what our service sends, so
    ``json_format.ParseDict(document["forest"], AndroidAccessibilityForest(),
    ignore_unknown_fields=True)`` reads it into android_env's proto too.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The tree source that captured it (its registry key, the fragment's ``producer``).
    producer: str
    #: The screen's width and height in pixels, as the capture was cleaned against.
    screen: ScreenSize
    #: How many off-screen subtrees the source left out past its node limit;
    #: 0 means the forest is the whole tree.
    omitted_offscreen: int
    #: Every window on every display, with every node the source captured.
    forest: AndroidAccessibilityForest

    def encode(self) -> bytes:
        """This tree as gzip JSON, with ``mtime=0``: an equal tree gives equal bytes."""
        return gzip.compress(self.model_dump_json(exclude_defaults=True).encode(), mtime=0)

    @classmethod
    def decode(cls, data: bytes) -> "RawUITree":
        """The inverse of :meth:`encode`."""
        return cls.model_validate_json(gzip.decompress(data))


class RawUITreeArchiver(AndroidArchiverBase):
    """The whole UI tree of every capture that succeeded, to clean again offline."""

    __registry_key__ = KEY
    needs: ClassVar[frozenset[AndroidArchiveNeed]] = frozenset({"ui_tree"})

    def open(self, ctx: AndroidArchiveContext) -> "RawUITreeSession":
        return RawUITreeSession(self, ctx)


class RawUITreeSession(AndroidArchiveSession):
    """Encodes the committed trees when the trace hook collects, and names the missed ones as gaps."""

    async def collect(self) -> ArchiveBatch:
        files: list[ArchiveFile] = []
        gaps: list[ArchiveGap] = []
        for entry in self.ctx.trees.drain():
            if isinstance(entry, MissingTree):
                gaps.append(
                    ArchiveGap(
                        archiver_id=self.archiver_id,
                        error=type(entry.error).__name__,
                        detail=(
                            f"the UI tree of observation cycle {entry.cycle} was not captured: "
                            f"{str(entry.error) or type(entry.error).__name__}"
                        ),
                    )
                )
            else:
                files.append(self._file(entry))
        return ArchiveBatch(files=tuple(files), gaps=tuple(gaps))

    def _file(self, tree: CommittedTree) -> ArchiveFile:
        document = RawUITree(
            producer=tree.producer,
            screen=tree.screen,
            omitted_offscreen=tree.capture.omitted_offscreen,
            forest=tree.capture.forest,
        )
        return ArchiveFile(
            archiver_id=self.archiver_id,
            name=f"ui_tree-{tree.cycle}.json.gz",
            media_type="application/gzip",
            data=document.encode(),
            meta=ArchiveProvenance(captured_at=tree.captured_at, cycle=tree.cycle),
            content_addressed=True,
        )


def raw_ui_trees(reader: ArchiveReader, step: int) -> list[RawUITree]:
    """The UI trees step *step*'s commit took, in capture order; ``[]`` when it took none."""
    return [
        RawUITree.decode(reader.read_extra(entry.path)) for entry in reader.archive_files(step, KEY)
    ]
