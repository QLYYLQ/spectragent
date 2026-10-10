"""What a capability hands over when its evidence is pulled.

Plain frozen value objects, with no receptors and no knowledge of the trace:
a capability builds :class:`ArchiveFile`\\ s, ``collect_archive`` returns them
as one :class:`ArchiveBatch`, and the consumer (the trace hook) decides
where they land. Nothing here knows a step number, the ``env/`` layout or
the WAL (``docs/architecture_design.md`` §4.9, §5.2).

Validation happens at construction, because the two names an archive file
carries — ``archiver_id`` and ``name`` — become path segments, and every
string it carries lands in the trace's JSON index. A batch that reached the
WAL with ``../`` in a name, a name that makes one path both a file and a
directory, a name longer than a filesystem allows (:data:`MAX_SEGMENT_BYTES`)
or a string UTF-8 cannot encode (a lone surrogate) would fail its commit —
and fail again, the same way, every time the consumer retried the batch.
Refusing it here keeps that failure at the capability that produced it.

.. note::
   No ``from __future__ import annotations`` — the package rule
   (:mod:`spectragent.core.archive._registrar`).
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, get_args

#: ``step`` — evidence attributed to the step whose commit takes it.
#: ``run`` — a capability's run record (environment fingerprint …),
#: versioned by the consumer because a committed path is immutable.
ArchiveScope = Literal["step", "run"]

_SCOPES = frozenset(get_args(ArchiveScope))

#: The one media type a run record may have: it is read back as a document.
RUN_RECORD_MEDIA_TYPE = "application/json"

#: The longest path segment, in bytes of UTF-8, that the common filesystems
#: accept (``NAME_MAX`` on ext4 and most Linux filesystems). A trace written
#: on a filesystem that counts characters must still fit on one that counts
#: bytes once it is copied there.
MAX_SEGMENT_BYTES = 255


class ArchiveContractError(ValueError):
    """A collected batch breaks the archiving contract.

    Raised by whoever receives the batch — the environment when a
    capability hands over a file under another capability's prefix, the
    consumer when a file names an archiver this run did not bind — before
    a single byte of it is written. A contract violation is not something
    to record around: the archive was explicitly requested, so a batch
    that cannot be trusted fails the run (``docs/architecture_design.md``
    §4.6, "fail closed").
    """


def text_problem(value: str) -> str | None:
    """Why *value* cannot be written into a trace, or ``None`` when it can.

    Every string of an archive file ends up in the trace's JSON index, and
    JSON on disk is UTF-8: a lone surrogate (``"\\udc80"``, what a lenient
    decoder leaves behind) has no UTF-8 encoding, so the commit carrying it
    would fail — deterministically, on every retry.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "is not encodable as UTF-8 (it holds a lone surrogate)"
    return None


def segment_problem(value: str) -> str | None:
    """Why *value* is not one safe path segment, or ``None`` when it is.

    The rule ``encoding.check_relative_path`` applies to a whole path,
    narrowed to one segment — non-empty, not ``.`` or ``..``, no ``/``,
    backslash or NUL — plus what only a file name can break: it must be
    encodable as UTF-8 (:func:`text_problem`) and at most
    :data:`MAX_SEGMENT_BYTES` bytes of it, or the filesystem refuses it
    (``ENAMETOOLONG``) when the commit writes it.

    Public because the consumer applies the same rule again to what it
    received: a frozen dataclass can still be forged around its own
    validation.
    """
    if value in ("", ".", "..") or any(ch in value for ch in ("/", "\\", "\x00")):
        return "is not a single path segment (non-empty, not '.' or '..', no '/', backslash or NUL)"
    problem = text_problem(value)
    if problem is not None:
        return problem
    size = len(value.encode("utf-8"))
    if size > MAX_SEGMENT_BYTES:
        return f"is {size} bytes of UTF-8, longer than the {MAX_SEGMENT_BYTES} a file name may have"
    return None


def _check_segment(value: object, what: str) -> None:
    """Refuse anything that is not one safe path segment (:func:`segment_problem`)."""
    if not isinstance(value, str):
        raise TypeError(f"ArchiveFile.{what} must be a str, got {type(value).__name__}")
    problem = segment_problem(value)
    if problem is not None:
        raise ValueError(f"ArchiveFile.{what} {value!r} {problem}")


@dataclass(frozen=True)
class ArchiveProvenance:
    """Where one piece of evidence came from — carried by every file.

    Attributes:
        captured_at: Wall-clock Unix time of the capture, comparable to
            ``StepRecord.timestamp`` and ``FragmentBase.captured_at``.
        cycle: The observation cycle the evidence belongs to, when it
            belongs to one. A cycle is the environment's counter, not a
            step number — the capability never learns step numbers.
        ids: Capability-scoped identifiers (session / frame / loader id
            and the like). Copied and frozen at construction; every key
            and value must be encodable as UTF-8 (:func:`text_problem`).
    """

    captured_at: float
    cycle: int | None = None
    ids: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.captured_at, bool) or not isinstance(self.captured_at, (int, float)):
            raise TypeError(
                f"ArchiveProvenance.captured_at must be a number (Unix time), "
                f"got {type(self.captured_at).__name__}"
            )
        if not math.isfinite(self.captured_at):
            raise ValueError(
                f"ArchiveProvenance.captured_at must be finite, got {self.captured_at}"
            )
        if self.cycle is not None and (
            isinstance(self.cycle, bool) or not isinstance(self.cycle, int)
        ):
            raise TypeError(
                f"ArchiveProvenance.cycle must be an int or None, got {type(self.cycle).__name__}"
            )
        if not isinstance(self.ids, Mapping):
            raise TypeError(
                f"ArchiveProvenance.ids must be a mapping, got {type(self.ids).__name__}"
            )
        frozen: dict[str, str] = {}
        for key, value in self.ids.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError(f"ArchiveProvenance.ids maps str to str; got {key!r}: {value!r}")
            for text in (key, value):
                problem = text_problem(text)
                if problem is not None:
                    raise ValueError(f"ArchiveProvenance.ids entry {key!r}: {text!r} {problem}")
            frozen[key] = value
        # A frozen record whose mapping a caller can still mutate is not
        # frozen. Copy, then expose read-only.
        object.__setattr__(self, "ids", MappingProxyType(frozen))


@dataclass(frozen=True)
class ArchiveFile:
    """One file of environment evidence.

    Attributes:
        archiver_id: Fully-qualified archiver key — becomes a path segment,
            so the file says which archiver produced it. A run record may
            instead carry the bare capability id: it belongs to the
            capability, not to one archiver.
        name: The archiver's own file name — one path segment
            (``exchanges.warc.gz``, ``storage.json``).
        media_type: ``type/subtype`` of :attr:`data`.
        data: The bytes, exactly as the consumer should write them.
        meta: Provenance — ``captured_at``, ``cycle``, ``ids``.
        content_addressed: Store under the data's sha256 and write once per
            run, however many steps reference it (a response body that
            repeats).
        scope: ``step`` or ``run`` (see :data:`ArchiveScope`).

    Raises (at construction):
        TypeError: A field of the wrong type.
        ValueError: ``archiver_id`` or ``name`` is not one safe path
            segment (:func:`segment_problem` — at most
            :data:`MAX_SEGMENT_BYTES` bytes of UTF-8 included); a step file
            under a key that is not ``<capability>.<id>``; a run record
            that is content-addressed or not JSON; a ``media_type`` without
            ``/`` or not encodable as UTF-8; an unknown scope.
    """

    archiver_id: str
    name: str
    media_type: str
    data: bytes
    meta: ArchiveProvenance
    content_addressed: bool = False
    scope: ArchiveScope = "step"

    def __post_init__(self) -> None:
        _check_segment(self.archiver_id, "archiver_id")
        _check_segment(self.name, "name")
        if not isinstance(self.media_type, str):
            raise TypeError(
                f"ArchiveFile.media_type must be a str, got {type(self.media_type).__name__}"
            )
        major, slash, minor = self.media_type.partition("/")
        if not (major and slash and minor):
            raise ValueError(
                f"ArchiveFile.media_type {self.media_type!r} is not a 'type/subtype' media type"
            )
        problem = text_problem(self.media_type)
        if problem is not None:
            raise ValueError(f"ArchiveFile.media_type {self.media_type!r} {problem}")
        if not isinstance(self.data, bytes):
            raise TypeError(f"ArchiveFile.data must be bytes, got {type(self.data).__name__}")
        if not isinstance(self.meta, ArchiveProvenance):
            raise TypeError(
                f"ArchiveFile.meta must be an ArchiveProvenance, got {type(self.meta).__name__}"
            )
        if not isinstance(self.content_addressed, bool):
            raise TypeError(
                f"ArchiveFile.content_addressed must be a bool, "
                f"got {type(self.content_addressed).__name__}"
            )
        if self.scope not in _SCOPES:
            raise ValueError(
                f"ArchiveFile.scope must be one of {sorted(_SCOPES)}, got {self.scope!r}"
            )
        prefix, dot, local = self.archiver_id.partition(".")
        if self.scope == "step":
            if not (dot and prefix and local):
                raise ValueError(
                    f"step evidence must name its archiver as '<capability>.<id>'; "
                    f"got archiver_id {self.archiver_id!r}"
                )
            return
        # A run record: owned by the capability, read back as a document.
        if dot and not (prefix and local):
            raise ValueError(
                f"a run record's archiver_id is a capability id or '<capability>.<id>'; "
                f"got {self.archiver_id!r}"
            )
        if self.content_addressed:
            raise ValueError(
                f"a run record is never content-addressed ({self.archiver_id!r}/{self.name!r}); "
                f"its versions are what change"
            )
        if self.media_type != RUN_RECORD_MEDIA_TYPE:
            raise ValueError(
                f"a run record must be {RUN_RECORD_MEDIA_TYPE!r}, got {self.media_type!r} "
                f"({self.archiver_id!r}/{self.name!r})"
            )

    @property
    def capability_id(self) -> str:
        """The capability that produced this file — the key's first segment."""
        return self.archiver_id.partition(".")[0]


@dataclass(frozen=True)
class ArchiveBatch:
    """Everything one ``collect_archive`` call handed over, in order.

    The caller owns it: the capability has already forgotten every file in
    it. A list passed as ``files`` is stored as a tuple.
    """

    files: tuple[ArchiveFile, ...] = ()

    def __post_init__(self) -> None:
        files = tuple(self.files)
        for item in files:
            if not isinstance(item, ArchiveFile):
                raise TypeError(
                    f"ArchiveBatch.files holds ArchiveFile objects, got {type(item).__name__}"
                )
        object.__setattr__(self, "files", files)

    @property
    def is_empty(self) -> bool:
        """True when the batch carries no file at all."""
        return not self.files

    @classmethod
    def merge(cls, batches: Iterable["ArchiveBatch"]) -> "ArchiveBatch":
        """One batch holding every file of *batches*, in the order given."""
        return cls(tuple(item for batch in batches for item in batch.files))
