"""A recorded run's network archive, loaded for replay (§4.14 "加载").

:func:`load_archive` reads one source run through the
:class:`~spectragent.core.archive.source.ArchiveReader` Protocol — it never
learns where the WAL keeps anything — at setup, before the browser starts:

- the environment fingerprint, ``archive_run_file("browser")``, with the
  committed sha256 of that record and the recording's T0;
- every ``index.jsonl`` of ``browser.network`` — the steps', then the trailing
  commits' — read back line by line (``archive_lines``) as :data:`IndexLine`;
- each commit's ``exchanges.warc.gz`` once (``read_extra``), so every record an
  index line points at is checked — its type and ``WARC-Record-ID`` — before
  anything is served, and a request's (redacted) body is read for its keys.

The index holds raw request facts only (M29). **Keys are recomputed here**
with the one canonicaliser (``canonical.py``) and the current site rules, so a
rule change leaves no stale key. Each servable exchange becomes one
:class:`IndexedRecord`, carrying its source ``run_id`` and its place in the
recording order (request start time, then index order):

- ``ok`` / ``redirect`` — the response (or ``revisit``) record of the same
  commit's WARC; a revisit's payload is the first response it refers to
  (``revisit_of``, possibly an earlier step's WARC), its HTTP head its own.
  Bodies are stored decoded (no ``Content-Encoding``).
- ``streamed_later`` — status and headers from the index line, the body the
  content-addressed blob a later ``kind: body`` line names; without an
  ``ok`` body line the record matches but cannot be served
  (``body_unavailable``).
- ``too_large``, ``incomplete``, and a record whose last line is ``pending``
  — matched, never served (``too_large`` / ``body_unavailable``).
- ``websocket`` — never a candidate (a handshake never pauses in Fetch).
- ``206`` — only in the byte-range map (:class:`RangeMap`), with the ``200``
  bodies of the same request; a ``304`` is a candidate for a conditional
  request only (the matcher's rule).

An index line that does not parse is the trace layer's schema error (naming
the file and line); a line whose WARC record is missing or is not what the
line says is :class:`ReplayArchiveError`, naming the source run, the file and
the record. Either fails setup before Chrome starts.

:func:`load_fingerprint` reads only the fingerprint (and counts the
``index.jsonl`` files): the consistency check ``prepare()`` runs needs nothing
else, so it opens no WARC and parses no index line.

:func:`iter_index_lines` is the same line-by-line read, handed out as
``(step, line)`` — the read the replay loads with and the offline readers of
the archive (``archive_stats.py``, the flywheel metrics, §4.16) share, so a
line means one thing to both.
"""

import hashlib
import io
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel
from warcio.archiveiterator import ArchiveIterator
from warcio.exceptions import ArchiveLoadFailed

from spectragent.core.archive.source import ArchiveEntry, ArchiveReader
from spectragent.core.capabilities.browser.archive.network import INDEX_NAME, WARC_NAME
from spectragent.core.capabilities.browser.archive.warc import header_lines
from spectragent.core.capabilities.browser.replay.canonical import (
    RequestKeys,
    host_of,
    request_keys,
)
from spectragent.core.capabilities.browser.replay.miss_policy import MissReason
from spectragent.core.capabilities.browser.replay.site_rules import SiteRules

#: The archiver whose evidence a replay serves.
NETWORK_ARCHIVER = "browser.network"
#: The capability whose run record is the environment fingerprint.
FINGERPRINT_CAPABILITY = "browser"

BodyStatus = Literal[
    "ok", "redirect", "too_large", "streamed_later", "websocket", "incomplete", "pending"
]


class ReplayArchiveError(RuntimeError):
    """The source run's network archive cannot be served as recorded.

    Names the source run, the archive file and the record it is about.
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        file: str | None = None,
        record_id: str | None = None,
    ) -> None:
        where = ", ".join(
            part
            for part in (
                f"source run {run_id!r}",
                f"file {file}" if file else "",
                f"record {record_id}" if record_id else "",
            )
            if part
        )
        super().__init__(f"{message} ({where})")
        self.run_id = run_id
        self.file = file
        self.record_id = record_id


# ── index.jsonl, as M29 writes it ───────────────────────────────────────
# Only the fields replay reads are modelled; the rest of a line (timing,
# initiator, ledger, …) is informational and ignored, never invented.


class _Line(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class WarcSpan(_Line):
    offset: int = Field(ge=0)
    length: int = Field(gt=0)


class WarcSpans(_Line):
    request: WarcSpan | None = None
    response: WarcSpan | None = None


class RequestFacts(_Line):
    #: The stored request headers (credential headers already stripped).
    headers: dict[str, Any] = Field(default_factory=dict)
    #: ``{sha256, length, content_type, …}`` when a body was stored.
    post: dict[str, Any] | None = None


class ExchangeLine(_Line):
    kind: Literal["exchange"]
    record_id: str
    request_record_id: str | None = None
    response_record_id: str | None = None
    warc: WarcSpans = Field(default_factory=WarcSpans)
    revisit_of: str | None = None
    method: str | None = None
    url: str
    status: int | None = None
    status_text: str | None = None
    resource_type: str | None = None
    request: RequestFacts = Field(default_factory=RequestFacts)
    response_headers: Any = None
    payload_sha256: str | None = None
    payload_length: int | None = None
    content_range: dict[str, Any] | None = None
    body_status: Literal["ok", "redirect", "too_large", "streamed_later", "websocket", "incomplete"]
    started_at: float | None = None


class PendingLine(_Line):
    kind: Literal["pending"]
    record_id: str
    method: str | None = None
    url: str
    waiting_for: str | None = None


class BodyLine(_Line):
    kind: Literal["body"]
    record_id: str
    body_status: str
    payload_sha256: str | None = None
    payload_length: int | None = None


#: One line of ``index.jsonl``, as :func:`iter_index_lines` hands it out.
IndexEntry = ExchangeLine | PendingLine | BodyLine


class IndexLine(
    RootModel[Annotated[ExchangeLine | PendingLine | BodyLine, Field(discriminator="kind")]]
):
    """One line of ``index.jsonl``: an exchange, a pending exchange, or a late body."""


# ── what a replay serves ─────────────────────────────────────────────────


@dataclass(frozen=True)
class WarcPayload:
    """A response body inside a WARC: the record at ``offset`` of the file at ``path``."""

    path: str
    offset: int
    length: int
    record_id: str


@dataclass(frozen=True)
class BlobPayload:
    """A response body stored as a content-addressed blob."""

    sha256: str


PayloadLocator = WarcPayload | BlobPayload


@dataclass(frozen=True)
class IndexedRecord:
    """One recorded exchange, keyed and located — what the matcher hands out.

    Attributes:
        record_id: The exchange's ``record_id`` (its WARC request record id).
        run_id: The run it was recorded in (merged archives hold several).
        order: Its place in its run's recording order (request start time).
        method: The request method.
        url: The request URL, without fragment.
        request_headers: The stored request headers (credential headers
            stripped at recording), for the header tie-break.
        body: The stored (redacted) request body.
        content_type: The request's ``Content-Type``.
        keys: The keys recomputed at load (``canonical.request_keys``).
        status: The recorded response status.
        status_text: Its reason phrase.
        response_headers: The recorded response headers, in order, one
            entry per header line (each ``Set-Cookie`` its own).
        payload: Where the response body is (``None``: nothing to serve).
        payload_length: The body's length, when known.
        payload_sha256: The body's sha256, when known.
        body_status: How M29 recorded the body (``pending``: never settled).
        content_range: ``{"unit", "start", "end", "total"}`` of a 206.
        resource_type: CDP's resource type at recording.
        started_at: Wall-clock start of the request, when known.
        miss_reason: Why the record matches but cannot be served, if it
            cannot (``too_large`` / ``body_unavailable``).
    """

    record_id: str
    run_id: str
    order: int
    method: str
    url: str
    request_headers: Mapping[str, str]
    body: bytes | None
    content_type: str | None
    keys: RequestKeys
    status: int | None = None
    status_text: str = ""
    response_headers: tuple[tuple[str, str], ...] = ()
    payload: PayloadLocator | None = None
    payload_length: int | None = None
    payload_sha256: str | None = None
    body_status: BodyStatus = "ok"
    content_range: Mapping[str, Any] | None = None
    resource_type: str | None = None
    started_at: float | None = None
    miss_reason: MissReason | None = None

    @property
    def servable(self) -> bool:
        return self.miss_reason is None


@dataclass(frozen=True)
class RangeSegment:
    """Bytes ``start``..``end`` (inclusive) of a resource of ``total`` bytes, in one record's body."""

    start: int
    end: int
    total: int | None
    record: IndexedRecord


@dataclass(frozen=True)
class RangeCover:
    """The recorded bytes that answer one ``Range`` request, from its first byte on.

    ``parts`` are ``(record, offset in that record's body, length)``, in byte
    order and contiguous; together they are bytes ``start``..``end``.
    """

    start: int
    end: int
    total: int | None
    parts: tuple[tuple[IndexedRecord, int, int], ...]

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def clipped(self, max_bytes: int) -> "RangeCover":
        """The first *max_bytes* of this cover (Chrome asks for the rest next)."""
        if self.length <= max_bytes:
            return self
        end = self.start + max_bytes - 1
        parts: list[tuple[IndexedRecord, int, int]] = []
        remaining = max_bytes
        for record, offset, length in self.parts:
            if remaining <= 0:
                break
            take = min(length, remaining)
            parts.append((record, offset, take))
            remaining -= take
        return RangeCover(start=self.start, end=end, total=self.total, parts=tuple(parts))


class RangeMap:
    """Byte ranges of a resource, from every ``206`` and ``200`` of the same GET.

    Keyed by the request's normalised key. A ``Range`` request is answered
    from here and never consumes a matcher cursor.
    """

    def __init__(self, segments: Mapping[str, Sequence[RangeSegment]] | None = None) -> None:
        self._segments = {
            key: sorted(found, key=lambda s: (s.start, -s.end, s.record.order))
            for key, found in (segments or {}).items()
            if found
        }

    def knows(self, key: str) -> bool:
        return key in self._segments

    def cover(self, key: str, start: int | None, end: int | None) -> RangeCover | None:
        """The contiguous recorded bytes from *start* (to *end*, or as far as recorded).

        *start* ``None`` with *end* ``K`` is a suffix range (the last ``K``
        bytes, needing a known total). ``None`` when the first byte asked for
        was never recorded.
        """
        segments = self._segments.get(key)
        if not segments:
            return None
        total = next((s.total for s in segments if s.total is not None), None)
        if start is None:
            if end is None or total is None or end <= 0:
                return None
            start, end = max(total - end, 0), total - 1
        if total is not None:
            if start >= total:
                return None
            end = total - 1 if end is None else min(end, total - 1)
        if end is not None and end < start:
            return None
        parts: list[tuple[IndexedRecord, int, int]] = []
        position = start
        for segment in segments:  # sorted by start
            if segment.start > position:
                break
            if segment.end < position:
                continue
            stop = segment.end if end is None else min(segment.end, end)
            parts.append((segment.record, position - segment.start, stop - position + 1))
            position = stop + 1
            if end is not None and position > end:
                break
        if not parts:
            return None
        return RangeCover(start=start, end=position - 1, total=total, parts=tuple(parts))


class ArchiveIndex:
    """The candidates of one replay, by key — built once, read by the matcher.

    ``206`` records live only in :attr:`ranges`; everything else is a
    candidate, in recording order, under its exact key, its normalised key
    and its host + path.
    """

    def __init__(self, records: Iterable[IndexedRecord], ranges: RangeMap | None = None) -> None:
        ordered = sorted(records, key=lambda r: (r.order, r.run_id, r.record_id))
        self.records: tuple[IndexedRecord, ...] = tuple(ordered)
        self.ranges = ranges if ranges is not None else RangeMap()
        self._exact: dict[tuple[str, str, str | None], list[IndexedRecord]] = {}
        self._normalized: dict[str, list[IndexedRecord]] = {}
        self._paths: dict[tuple[str, str], list[IndexedRecord]] = {}
        self._hosts: dict[str, list[IndexedRecord]] = {}
        for record in ordered:
            keys = record.keys
            self._exact.setdefault(keys.exact, []).append(record)
            self._normalized.setdefault(keys.normalized, []).append(record)
            self._paths.setdefault((keys.host, keys.path), []).append(record)
            self._hosts.setdefault(keys.host, []).append(record)

    def exact(self, key: tuple[str, str, str | None]) -> Sequence[IndexedRecord]:
        return self._exact.get(key, ())

    def normalized(self, key: str) -> Sequence[IndexedRecord]:
        return self._normalized.get(key, ())

    def same_path(self, host: str, path: str) -> Sequence[IndexedRecord]:
        return self._paths.get((host, path), ())

    def same_host(self, host: str) -> Sequence[IndexedRecord]:
        return self._hosts.get(host, ())

    def __len__(self) -> int:
        return len(self.records)


class PayloadStore:
    """Reads response bodies on demand: WARC records at their offsets, blobs by digest."""

    def __init__(self, reader: ArchiveReader, warcs: "_WarcFiles") -> None:
        self._reader = reader
        self._warcs = warcs

    def read(self, record: IndexedRecord) -> bytes:
        locator = record.payload
        if locator is None:
            raise ReplayArchiveError(
                "record has no response body to serve",
                run_id=record.run_id,
                record_id=record.record_id,
            )
        if isinstance(locator, BlobPayload):
            data = self._reader.blob(locator.sha256)
        else:
            data = self._warcs.record(
                locator.path,
                WarcSpan(offset=locator.offset, length=locator.length),
                locator.record_id,
                ("response",),
                owner=record.record_id,
                with_payload=True,
            ).payload
        expected = record.payload_sha256
        if expected is not None and hashlib.sha256(data).hexdigest() != expected:
            raise ReplayArchiveError(
                "the stored body does not hash to the sha256 the index recorded",
                run_id=record.run_id,
                file=locator.path if isinstance(locator, WarcPayload) else None,
                record_id=record.record_id,
            )
        return data


@dataclass
class RecordedArchive:
    """One source run, loaded: its fingerprint and its network index.

    Attributes:
        run_id: The source run.
        fingerprint: Its ``env/run.browser.json`` (``None`` when it has none —
            the consistency check reports that, naming the run).
        fingerprint_sha256: The committed sha256 of that record.
        t0_ms: The recording's T0 (``replayability.t0_ms``), when pinned.
        index: The candidates.
        rules: The site rules the keys were computed with.
        network_files: How many ``index.jsonl`` files the run committed
            (``0``: it recorded no network traffic, or no ``browser.network``).
        store: Where bodies are read from (``None`` for an index built in
            memory, which serves no body).
    """

    run_id: str
    fingerprint: dict[str, Any] | None
    fingerprint_sha256: str | None
    t0_ms: int | None
    index: ArchiveIndex
    rules: SiteRules = field(default_factory=SiteRules)
    network_files: int = 0
    store: PayloadStore | None = field(default=None, repr=False)

    def body(self, record: IndexedRecord) -> bytes:
        """*record*'s recorded response body (read on demand, checked against its sha256).

        Raises:
            ReplayArchiveError: The record has no body to serve, or the bytes
                are not what the index recorded.
        """
        if self.store is None:
            raise ReplayArchiveError(
                "this archive was built in memory and holds no bodies",
                run_id=record.run_id,
                record_id=record.record_id,
            )
        return self.store.read(record)

    def range_bytes(self, cover: RangeCover) -> bytes:
        """The bytes of *cover*, cut from its records' bodies."""
        return b"".join(
            self.body(record)[offset : offset + length] for record, offset, length in cover.parts
        )


# ── loading ─────────────────────────────────────────────────────────────


@dataclass
class _Commit:
    """One commit's ``browser.network`` files."""

    step: int | None
    files: dict[str, ArchiveEntry] = field(default_factory=dict)


@dataclass
class _Raw:
    """An exchange line and where it was read."""

    line: ExchangeLine
    commit: _Commit
    position: int


def load_archive(reader: ArchiveReader, rules: SiteRules | None = None) -> RecordedArchive:
    """Load *reader*'s network archive for replay, keyed with *rules*.

    Raises:
        ReplayArchiveError: An index line points at a WARC record that is
            missing or is not the record it names, a revisit refers to a
            response the archive does not hold, a late body is not a
            committed blob, or the fingerprint is not a JSON object.
    """
    rules = rules or SiteRules()
    run_id = reader.run_id
    fingerprint, fingerprint_sha256 = _fingerprint(reader, run_id)
    commits = _commits(reader.iter_archive(NETWORK_ARCHIVER, include_trailing=True))
    warcs = _WarcFiles(reader, run_id)
    blobs = {
        entry.sha256
        for commit in commits
        for entry in commit.files.values()
        if entry.content_addressed
    }

    exchanges: dict[str, _Raw] = {}
    last_line: dict[str, IndexEntry] = {}
    pending_lines: dict[str, tuple[PendingLine, int]] = {}
    body_lines: dict[str, BodyLine] = {}
    network_files = sum(1 for commit in commits if INDEX_NAME in commit.files)
    for position, (commit, line) in enumerate(_commit_lines(reader, commits), start=1):
        last_line[line.record_id] = line
        if isinstance(line, ExchangeLine):
            if line.record_id in exchanges:
                raise ReplayArchiveError(
                    "an exchange is recorded twice",
                    run_id=run_id,
                    file=commit.files[INDEX_NAME].path,
                    record_id=line.record_id,
                )
            exchanges[line.record_id] = _Raw(line, commit, position)
        elif isinstance(line, PendingLine):
            pending_lines.setdefault(line.record_id, (line, position))
        else:
            body_lines[line.record_id] = line

    # Every response record by id, for revisits (which may point at an earlier step).
    responses: dict[str, WarcPayload] = {}
    for raw in exchanges.values():
        span = raw.line.warc.response
        if raw.line.response_record_id and span is not None:
            warc = _warc_entry(raw, run_id)
            responses[raw.line.response_record_id] = WarcPayload(
                warc.path, span.offset, span.length, raw.line.response_record_id
            )

    drafts: list[tuple[tuple[int, float, int], dict[str, Any]]] = []
    for record_id, raw in exchanges.items():
        line = raw.line
        if line.body_status == "websocket" or line.url.startswith(("ws://", "wss://")):
            continue
        drafts.append(
            (
                _recording_order(line.started_at, raw.position),
                _exchange_fields(
                    raw,
                    run_id=run_id,
                    warcs=warcs,
                    responses=responses,
                    body_line=body_lines.get(record_id),
                    blobs=blobs,
                    ends_pending=isinstance(last_line.get(record_id), PendingLine),
                ),
            )
        )
    for record_id, (pending, seen_at) in pending_lines.items():
        if record_id in exchanges or pending.url.startswith(("ws://", "wss://")):
            continue
        drafts.append(
            (
                _recording_order(None, seen_at),
                {
                    "record_id": record_id,
                    "method": (pending.method or "GET").upper(),
                    "url": pending.url,
                    "request_headers": {},
                    "body": None,
                    "content_type": None,
                    "body_status": "pending",
                    "miss_reason": "body_unavailable",
                },
            )
        )

    records: list[IndexedRecord] = []
    segments: dict[str, list[RangeSegment]] = {}
    for order, (_, fields) in enumerate(sorted(drafts, key=lambda draft: draft[0])):
        keys = request_keys(
            fields["method"],
            fields["url"],
            fields["body"],
            fields["content_type"],
            rules.for_host(host_of(fields["url"])),
        )
        record = IndexedRecord(run_id=run_id, order=order, keys=keys, **fields)
        segment = _range_segment(record)
        if segment is not None:
            segments.setdefault(record.keys.normalized, []).append(segment)
        if record.status == 206:
            continue
        records.append(record)

    return RecordedArchive(
        run_id=run_id,
        fingerprint=fingerprint,
        fingerprint_sha256=fingerprint_sha256,
        t0_ms=_t0_ms(fingerprint),
        index=ArchiveIndex(records, RangeMap(segments)),
        rules=rules,
        network_files=network_files,
        store=PayloadStore(reader, warcs),
    )


def load_fingerprint(reader: ArchiveReader) -> RecordedArchive:
    """*reader*'s environment fingerprint, its T0 and its network file count — no index, no bodies.

    What the consistency check reads of a source run, cheaply: the run
    record (``archive_run_file("browser")``) and how many ``index.jsonl``
    files the run committed, never an index line or a WARC record. The
    result has an empty index and no store, so it matches and serves
    nothing: ``BrowserCapability.check_setup`` checks a replay against it at
    ``prepare()`` (§4.14), and setup loads the whole archive with
    :func:`load_archive` to serve it.

    Raises:
        ReplayArchiveError: The fingerprint is not a JSON object.
    """
    run_id = reader.run_id
    fingerprint, fingerprint_sha256 = _fingerprint(reader, run_id)
    commits = _commits(reader.iter_archive(NETWORK_ARCHIVER, include_trailing=True))
    return RecordedArchive(
        run_id=run_id,
        fingerprint=fingerprint,
        fingerprint_sha256=fingerprint_sha256,
        t0_ms=_t0_ms(fingerprint),
        index=ArchiveIndex([]),
        network_files=sum(1 for commit in commits if INDEX_NAME in commit.files),
    )


def _t0_ms(fingerprint: dict[str, Any] | None) -> int | None:
    """The recording's T0 (``replayability.t0_ms``), when the fingerprint carries an integer one."""
    tier = fingerprint.get("replayability") if fingerprint is not None else None
    t0_ms = tier.get("t0_ms") if isinstance(tier, dict) else None
    return t0_ms if isinstance(t0_ms, int) and not isinstance(t0_ms, bool) else None


def _fingerprint(reader: ArchiveReader, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    entry = reader.archive_run_file(FINGERPRINT_CAPABILITY)
    if entry is None:
        return None, None
    try:
        document = json.loads(reader.read_extra(entry.path))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReplayArchiveError(
            f"the environment fingerprint is not JSON: {exc}", run_id=run_id, file=entry.path
        ) from exc
    if not isinstance(document, dict):
        raise ReplayArchiveError(
            "the environment fingerprint is not a JSON object", run_id=run_id, file=entry.path
        )
    return document, entry.sha256


def iter_index_lines(reader: ArchiveReader) -> Iterator[tuple[int | None, IndexEntry]]:
    """``(step, line)`` for every ``index.jsonl`` line of the run's ``browser.network`` archive.

    Commit by commit — the steps' in step order, then the trailing commits'
    under ``None`` — and within a file in line order: the order
    :func:`load_archive` reads them in. Only what the run committed
    (``ArchiveReader``); a line that does not parse is the reader's schema
    error, naming the file and the line.
    """
    commits = _commits(reader.iter_archive(NETWORK_ARCHIVER, include_trailing=True))
    for commit, line in _commit_lines(reader, commits):
        yield commit.step, line


def _commit_lines(
    reader: ArchiveReader, commits: Iterable[_Commit]
) -> Iterator[tuple[_Commit, IndexEntry]]:
    """Every ``index.jsonl`` line of *commits*, with the commit that took it, in order."""
    for commit in commits:
        index = commit.files.get(INDEX_NAME)
        if index is None:
            continue
        for item in reader.archive_lines(index.path, IndexLine):
            yield commit, item.root


def _commits(entries: Iterable[tuple[int | None, ArchiveEntry]]) -> list[_Commit]:
    """Group the iterated files by the commit that took them.

    One commit's files of one archiver come together and carry unique names,
    so a new step number — or a name seen again (consecutive trailing commits
    all report ``None``) — starts the next commit.
    """
    commits: list[_Commit] = []
    current: _Commit | None = None
    for step, entry in entries:
        if current is None or step != current.step or entry.name in current.files:
            current = _Commit(step)
            commits.append(current)
        current.files[entry.name] = entry
    return commits


def _recording_order(started_at: float | None, position: int) -> tuple[int, float, int]:
    """Request start time first; a record without one keeps its index position, after them."""
    if started_at is None:
        return (1, 0.0, position)
    return (0, started_at, position)


def _warc_entry(raw: _Raw, run_id: str) -> ArchiveEntry:
    entry = raw.commit.files.get(WARC_NAME)
    if entry is None:
        raise ReplayArchiveError(
            f"the index points into a WARC its commit does not hold ({WARC_NAME})",
            run_id=run_id,
            file=raw.commit.files[INDEX_NAME].path,
            record_id=raw.line.record_id,
        )
    return entry


def _exchange_fields(
    raw: _Raw,
    *,
    run_id: str,
    warcs: "_WarcFiles",
    responses: Mapping[str, WarcPayload],
    body_line: BodyLine | None,
    blobs: set[str],
    ends_pending: bool,
) -> dict[str, Any]:
    """Everything an :class:`IndexedRecord` needs from one exchange line, checked."""
    line = raw.line
    method = (line.method or "GET").upper()
    request_headers = {str(k): str(v) for k, v in line.request.headers.items()}
    content_type = _content_type(line, request_headers)
    body: bytes | None = None
    if line.warc.request is not None:
        warc = _warc_entry(raw, run_id)
        request = warcs.record(
            warc.path,
            line.warc.request,
            line.record_id,
            ("request",),
            owner=line.record_id,
            with_payload=True,
        )
        body = request.payload or None
        stored = line.request.post or {}
        digest = stored.get("sha256")
        if isinstance(digest, str) and hashlib.sha256(body or b"").hexdigest() != digest:
            raise ReplayArchiveError(
                "the request body in the WARC does not hash to the sha256 the index recorded",
                run_id=run_id,
                file=warc.path,
                record_id=line.record_id,
            )
    fields: dict[str, Any] = {
        "record_id": line.record_id,
        "method": method,
        "url": line.url.split("#", 1)[0],
        "request_headers": request_headers,
        "body": body,
        "content_type": content_type,
        "status": line.status,
        "status_text": line.status_text or "",
        "payload_length": line.payload_length,
        "payload_sha256": line.payload_sha256,
        "body_status": line.body_status,
        "content_range": line.content_range,
        "resource_type": line.resource_type,
        "started_at": line.started_at,
    }
    status = line.body_status
    if status in ("ok", "redirect"):
        span = line.warc.response
        if span is None or not line.response_record_id:
            raise ReplayArchiveError(
                f"an exchange recorded as {status!r} has no response record",
                run_id=run_id,
                file=_warc_entry(raw, run_id).path,
                record_id=line.record_id,
            )
        warc = _warc_entry(raw, run_id)
        response = warcs.record(
            warc.path, span, line.response_record_id, ("response", "revisit"), owner=line.record_id
        )
        fields["status"] = response.status if response.status is not None else line.status
        fields["status_text"] = response.status_text
        fields["response_headers"] = response.headers
        if response.rec_type == "revisit":
            first = responses.get(line.revisit_of or "")
            if first is None:
                raise ReplayArchiveError(
                    f"a revisit refers to {line.revisit_of!r}, which the archive does not hold",
                    run_id=run_id,
                    file=warc.path,
                    record_id=line.record_id,
                )
            warcs.record(
                first.path,
                WarcSpan(offset=first.offset, length=first.length),
                first.record_id,
                ("response",),
                owner=line.record_id,
            )
            fields["payload"] = first
        else:
            fields["payload"] = WarcPayload(warc.path, span.offset, span.length, response.record_id)
        if ends_pending:
            fields["miss_reason"] = "body_unavailable"
        return fields

    fields["response_headers"] = tuple(header_lines(line.response_headers))
    if status == "streamed_later" and body_line is not None and body_line.body_status == "ok":
        digest = body_line.payload_sha256
        if not digest or digest not in blobs:
            raise ReplayArchiveError(
                f"a streamed body names blob {digest!r}, which the archive does not hold",
                run_id=run_id,
                record_id=line.record_id,
            )
        fields["payload"] = BlobPayload(digest)
        fields["payload_length"] = body_line.payload_length
        fields["payload_sha256"] = digest
        if ends_pending:
            fields["miss_reason"] = "body_unavailable"
        return fields
    fields["miss_reason"] = "too_large" if status == "too_large" else "body_unavailable"
    return fields


def _content_type(line: ExchangeLine, headers: Mapping[str, str]) -> str | None:
    post = line.request.post or {}
    declared = post.get("content_type")
    if isinstance(declared, str) and declared:
        return declared
    return next((value for name, value in headers.items() if name.lower() == "content-type"), None)


def _range_segment(record: IndexedRecord) -> RangeSegment | None:
    """The bytes a servable GET record holds of its resource, if it is a 200 or a 206."""
    if record.method != "GET" or not record.servable or record.payload_length is None:
        return None
    if record.status == 200 and record.body_status == "ok" and record.payload_length > 0:
        length = record.payload_length
        return RangeSegment(start=0, end=length - 1, total=length, record=record)
    span = record.content_range or {}
    if record.status != 206 or span.get("unit") != "bytes":
        return None
    start, end = span.get("start"), span.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or record.payload_length <= 0:
        return None
    end = min(end, start + record.payload_length - 1)
    total = span.get("total")
    return RangeSegment(
        start=start, end=end, total=total if isinstance(total, int) else None, record=record
    )


# ── WARC records ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _WarcRecord:
    rec_type: str
    record_id: str
    status: int | None
    status_text: str
    headers: tuple[tuple[str, str], ...]
    payload: bytes


class _WarcFiles:
    """Each commit's WARC, read once; records parsed at their offsets and checked."""

    def __init__(self, reader: ArchiveReader, run_id: str) -> None:
        self._reader = reader
        self._run_id = run_id
        self._data: dict[str, bytes] = {}

    def bytes_of(self, path: str) -> bytes:
        data = self._data.get(path)
        if data is None:
            data = self._data[path] = self._reader.read_extra(path)
        return data

    def record(
        self,
        path: str,
        span: WarcSpan,
        record_id: str,
        types: Sequence[str],
        *,
        owner: str,
        with_payload: bool = False,
    ) -> _WarcRecord:
        """The record at *span* of *path*, which must be of one of *types* and have *record_id*.

        *owner* is the exchange the record belongs to — what an error names.
        Its payload is read only *with_payload*: loading checks every record
        but reads only request bodies; response bodies are read when served.
        """
        data = self.bytes_of(path)
        chunk = data[span.offset : span.offset + span.length]
        try:
            parsed = next(iter(ArchiveIterator(io.BytesIO(chunk))), None)
            if parsed is None:
                raise ArchiveLoadFailed("no record at that offset")
            rec_type = str(parsed.rec_type)
            found_id = str(parsed.rec_headers.get_header("WARC-Record-ID") or "").strip("<>")
            http = parsed.http_headers
            payload = parsed.raw_stream.read() if with_payload else b""
        except Exception as exc:  # untrusted bytes: any parse failure is "no record here"
            raise ReplayArchiveError(
                f"no WARC record at offset {span.offset} (+{span.length}): {exc}",
                run_id=self._run_id,
                file=path,
                record_id=owner,
            ) from exc
        if rec_type not in types or found_id != record_id:
            raise ReplayArchiveError(
                f"the WARC record at offset {span.offset} is a {rec_type!r} record "
                f"{found_id!r}, not the {'/'.join(types)} record {record_id!r} the index names",
                run_id=self._run_id,
                file=path,
                record_id=owner,
            )
        status: int | None = None
        status_text = ""
        headers: tuple[tuple[str, str], ...] = ()
        if http is not None:
            headers = tuple((str(name), str(value)) for name, value in http.headers)
            if rec_type != "request":
                code, _, text = str(http.statusline).partition(" ")
                status = int(code) if code.isdigit() else None
                status_text = text
        return _WarcRecord(rec_type, found_id, status, status_text, headers, payload)
