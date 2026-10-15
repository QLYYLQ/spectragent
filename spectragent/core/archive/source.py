"""The replay read side: ``ArchiveReader`` and its opener (§4.14 "回放的数据通路").

A replay reads a recorded run's environment evidence — and nothing else of
it — through :class:`ArchiveReader`. The capability that replays never learns
the WAL: it is handed a run *directory* (the task context's
``replay_archive``), opens it with :func:`open_archive`, and from then on
speaks only this Protocol.

``RunTrace`` satisfies the Protocol structurally (it does not import it), with
the committed-only accessors M27/M28 gave it: every read resolves through the
manifest and the extra indexes, so an ``env/`` file the manifest never listed
— a crash-window leftover, something dropped in by hand — is invisible here
and is never served. Opening and reading write nothing; the source
directory's tree is the same before and after a replay.

**The one import exception of ``core/archive``** (§10): this module imports
``core.trace.reader`` — exactly that module — to implement the opener. The
package ``__init__`` never imports this module, so ``import
spectragent.core.archive`` never pulls the trace layer in, and the
``core/trace`` → ``core.archive.privacy`` direction stays acyclic
(``tests/test_core/test_archive/test_archive_boundaries.py`` pins both).
"""

from collections.abc import Iterator, Mapping, Sequence
from os import PathLike
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from spectragent.core.trace.reader import RunTrace, TraceSchemaError

M = TypeVar("M", bound=BaseModel)


class ArchiveEntry(Protocol):
    """One committed environment-evidence file, as a reader hands it back.

    Every member is a read-only property, so a model whose fields are a
    ``dict`` or a ``Literal`` (``RunTrace``'s ``ArchiveRecord``) matches it
    covariantly. The bytes are ``ArchiveReader.read_extra(entry.path)``.
    """

    @property
    def path(self) -> str: ...

    @property
    def archiver_id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def media_type(self) -> str: ...

    @property
    def scope(self) -> str: ...

    @property
    def sha256(self) -> str: ...

    @property
    def content_addressed(self) -> bool: ...

    @property
    def captured_at(self) -> float: ...

    @property
    def ids(self) -> Mapping[str, str]: ...

    @property
    def run_version(self) -> int | None: ...


@runtime_checkable
class ArchiveReader(Protocol):
    """The committed environment evidence of one recorded run.

    The semantics are ``RunTrace``'s (§5.5): "not committed" reads as empty /
    ``None`` / ``KeyError``, "committed but unreadable" raises the trace
    layer's schema error naming the file.
    """

    @property
    def run_id(self) -> str: ...

    def iter_archive(
        self, archiver_id: str, *, include_trailing: bool = False
    ) -> Iterator[tuple[int | None, ArchiveEntry]]:
        """``(step, entry)`` for every committed step file of *archiver_id*, in step order.

        With *include_trailing*, the trailing commits' files follow as
        ``(None, entry)``. Run records are not yielded.
        """
        ...

    def archive_files(self, step: int, archiver_id: str) -> Sequence[ArchiveEntry]:
        """The files of *archiver_id* that *step*'s commit took, in index order."""
        ...

    def archive_run_file(self, capability_id: str) -> ArchiveEntry | None:
        """The highest committed version of *capability_id*'s run record."""
        ...

    def read_extra(self, path: str) -> bytes:
        """A committed file's bytes, checked against its index entry."""
        ...

    def blob(self, sha256: str) -> bytes:
        """A committed content-addressed archive file's bytes."""
        ...

    def archive_lines(self, path: str, model: type[M]) -> list[M]:
        """A committed line-delimited JSON file, each non-blank line read back as *model*."""
        ...


class ArchiveOpenError(RuntimeError):
    """``replay_archive`` names no recorded run directory.

    Raised by :func:`open_archive` when the path is not a directory, or is a
    directory without a manifest — never a committed run. A manifest that
    exists but cannot be read is the trace layer's own error, naming the
    file.
    """

    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message)
        #: The run directory that was asked for.
        self.path = path


def open_archive(run_dir: str | PathLike[str]) -> ArchiveReader:
    """Open a recorded run directory for replay — read-only, committed evidence only.

    Raises:
        ArchiveOpenError: *run_dir* is not a directory, or holds no
            manifest (it never committed anything).
    """
    path = Path(run_dir)
    if not path.is_dir():
        raise ArchiveOpenError(
            f"replay archive {path} is not a directory — replay_archive must name a "
            f"recorded run directory (the one holding its manifest.json)",
            path=path,
        )
    try:
        return RunTrace(path)
    except TraceSchemaError as exc:
        if exc.path is not None and not exc.path.exists():
            raise ArchiveOpenError(
                f"replay archive {path} has no {exc.path.name}: it is not a committed run "
                f"directory",
                path=path,
            ) from exc
        raise
