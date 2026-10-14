"""Matching live requests against a recorded archive (§4.14 "三级匹配").

One :class:`Matcher` per replay run (one episode): it owns the run's cursors.
:meth:`Matcher.match` takes a live request — its body already redacted with
M29's ``redact_post_body`` — and in one synchronous call selects a recorded
exchange *and* marks it used, so no ``await`` ever sits between choosing a
record and consuming it.

**Levels**, tried in order:

- **L0 exact** — method, full URL (no fragment) and body bytes equal; a
  multipart body compared without its boundary (Playwright ``harBackend``).
- **L1 normalised** — the canonical keys equal (``canonical.py``).
- **L2 fuzzy** — same host and path and method, scored ``2M/T`` (``M`` the
  parameter names whose values agree on both sides, ``T`` the parameter names
  of both sides together; both sides without any: ``1.0``) over the canonical
  query parameters — and, for a request with a body, its canonical body
  parameters too — and a hit only at or above ``fuzzy_threshold``. Off for
  documents and non-GET requests unless the host's site rule says
  ``fuzzy: on``, which also turns it on when ``levels`` lacks ``fuzzy``.
  The threshold and both restrictions are **ours**: WPR's fuzzy match has no
  threshold (the best same-path URL always wins, ``M`` counted from 1) and
  never looks at a body.

**Each record is used once.** Within a key, records come in recording order
(mitmproxy's pop, WPR's ``serve-response-in-chronological-sequence``); the
used set is shared across levels. When no level has an unused record left,
the **last** record (recording order) of the best level that had candidates
is repeated and the result says ``exhausted`` — a deliberate choice of ours
(mitmproxy misses once a key is used up, WPR falls back to the best header
match), so a polling page does not fail on its N+1st poll; the replay log
shows it.

**Run affinity**: once an episode has hit a record, records of that run come
first, then other runs in recording order ("right HTML, wrong JSON" across a
merged archive; v1 archives hold one run). **Request headers only break
ties**: between candidates otherwise equal — two runs' records at the same
place in their recordings, two fuzzy URLs with the same score — the one
sharing more Fetch-visible request headers wins (the stripped credential
headers and the site rule's ``ignore_headers`` never count), then the earlier
recorded, then the smaller URL (WPR).

A ``Range`` request is answered from the archive's byte-range map and uses no
cursor. A ``304`` is a candidate only for a conditional request. On a miss the
result carries the three nearest records of the same host.

.. note::
   No ``from __future__ import annotations`` — the browser replay package
   rule: ``MatcherConfig`` is a knob conscribe reads at class creation.
"""

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import ConfigDict, Field, field_validator
from pydantic.dataclasses import dataclass as pydantic_dataclass

from spectragent.core.capabilities.browser.archive.redaction import (
    DEFAULT_STRIPPED_REQUEST_HEADERS,
)
from spectragent.core.capabilities.browser.replay.archive_index import (
    ArchiveIndex,
    IndexedRecord,
    RangeCover,
)
from spectragent.core.capabilities.browser.replay.canonical import (
    Params,
    RequestKeys,
    host_of,
    request_keys,
)
from spectragent.core.capabilities.browser.replay.miss_policy import MissReason
from spectragent.core.capabilities.browser.replay.site_rules import SiteRule, SiteRules

MatchLevel = Literal["exact", "normalized", "fuzzy"]

#: Request headers that make a request conditional (a ``304`` answers only these).
CONDITIONAL_HEADERS = (
    "if-none-match",
    "if-modified-since",
    "if-match",
    "if-unmodified-since",
    "if-range",
)
#: How many near records a miss reports.
NEAREST = 3
#: ``bytes=N-``, ``bytes=N-M``, ``bytes=-K`` (one range; a multi-range list is not served).
_RANGE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)
_STRIPPED = frozenset(DEFAULT_STRIPPED_REQUEST_HEADERS)


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class MatcherConfig:
    """How hard the replay tries to match (``network_source.matcher``).

    A pydantic dataclass rather than a ``BaseModel``: conscribe (1.3) imports
    a non-``BaseModel`` knob type into the generated ``_composed.py`` but
    assumes every ``BaseModel`` is a submodel it emitted itself, so a
    ``BaseModel`` here would leave the generated module unimportable.
    """

    levels: list[MatchLevel] = Field(
        default_factory=lambda: ["exact", "normalized"],
        min_length=1,
        description=(
            "Matching levels to try, in order: exact (method, URL, body bytes), "
            "normalized (canonical keys), fuzzy (same host and path, query-ratio "
            "score). A site rule's 'fuzzy: on' turns fuzzy on for that site anyway."
        ),
    )
    fuzzy_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Lowest 2M/T score that counts as a fuzzy hit (inclusive).",
    )
    site_rules: str | None = Field(
        default=None,
        description=(
            "Directory of per-site rule files (<domain>.yaml), e.g. configs/replay_rules. "
            "None: no site rules."
        ),
    )

    @field_validator("levels")
    @classmethod
    def _each_level_once(cls, levels: list[MatchLevel]) -> list[MatchLevel]:
        if len(set(levels)) != len(levels):
            raise ValueError(f"each matching level once: got {levels}")
        return levels

    def fingerprint(self) -> dict[str, object]:
        """The knobs as the replay fingerprint records them."""
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class LiveRequest:
    """A request Chrome paused, as the matcher needs it.

    Attributes:
        method: The request method.
        url: The request URL (a fragment is ignored).
        headers: The Fetch-visible request headers.
        body: The request body **after** ``redact_post_body`` (``None``: none).
        is_document: The request loads a document (``resourceType: Document``).
        resource_type: CDP's ``resourceType`` (``Script``, ``XHR`` …), when
            known: the responder tries the JSONP callback rewrite on scripts
            only (``jsonp.py``).
    """

    method: str
    url: str
    headers: Mapping[str, str] = dataclasses.field(default_factory=dict)
    body: bytes | None = None
    is_document: bool = False
    resource_type: str | None = None

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((value for key, value in self.headers.items() if key.lower() == wanted), None)

    @property
    def content_type(self) -> str | None:
        return self.header("content-type")


@dataclass(frozen=True)
class Candidate:
    """A recorded exchange near a missed request (the replay log's top 3)."""

    record_id: str
    method: str
    url: str
    score: float
    consumed: bool


@dataclass(frozen=True)
class MatchResult:
    """What the archive holds for one live request.

    Attributes:
        keys: The request's keys (what the replay log records).
        record: The record chosen (or repeated, or the first one a range
            cover reads from); ``None`` on a miss.
        level: The level that found it.
        fuzzy_score: The ``2M/T`` score of a fuzzy hit.
        exhausted: Every candidate was used; :attr:`record` is the last one,
            repeated.
        range: The bytes that answer a ``Range`` request.
        reason: ``None`` when the result can be served; otherwise why not —
            ``no_match``, ``range_uncovered``, or the chosen record's own
            ``too_large`` / ``body_unavailable``.
        candidates: On ``no_match``, the nearest records of the same host.
    """

    keys: RequestKeys
    record: IndexedRecord | None = None
    level: MatchLevel | None = None
    fuzzy_score: float | None = None
    exhausted: bool = False
    range: RangeCover | None = None
    reason: MissReason | None = None
    candidates: tuple[Candidate, ...] = ()

    @property
    def servable(self) -> bool:
        return self.reason is None


def fuzzy_score(left: Params, right: Params) -> float:
    """``2M/T`` over parameter names: ``M`` names whose values agree, ``T`` names of both sides.

    A name's value is the sorted list of its values, so a repeated parameter
    agrees only when all of its values do. Both sides without parameters
    score ``1.0``.
    """
    a = _grouped(left)
    b = _grouped(right)
    total = len(a) + len(b)
    if total == 0:
        return 1.0
    agree = sum(1 for name, values in a.items() if b.get(name) == values)
    return 2 * agree / total


def parse_range(value: str) -> tuple[int | None, int | None] | None:
    """``bytes=N-`` → ``(N, None)``; ``bytes=N-M`` → ``(N, M)``; ``bytes=-K`` → ``(None, K)``.

    ``None`` for anything else (a multi-range list, another unit).
    """
    match = _RANGE.match(value)
    if match is None:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        return None, int(last)
    return int(first), int(last) if last else None


class Matcher:
    """One episode's matcher: the index, the knobs, and the cursors."""

    def __init__(
        self,
        index: ArchiveIndex,
        config: MatcherConfig | None = None,
        rules: SiteRules | None = None,
    ) -> None:
        self.index = index
        self.config = config if config is not None else MatcherConfig()
        self.rules = rules if rules is not None else SiteRules()
        #: ``(run_id, record_id)`` of every record served (or repeated) so far.
        self._used: set[tuple[str, str]] = set()
        #: The run of the episode's first hit.
        self.affinity: str | None = None

    # ── the one entry point ──────────────────────────────────────────────

    def match(self, request: LiveRequest) -> MatchResult:
        """Select and consume the record for *request* — synchronously, in one call."""
        rule = self.rules.for_host(host_of(request.url))
        keys = request_keys(request.method, request.url, request.body, request.content_type, rule)
        range_header = request.header("range")
        if range_header is not None:
            return self._match_range(keys, range_header)
        conditional = any(request.header(name) is not None for name in CONDITIONAL_HEADERS)
        live_headers = {(name.lower(), value) for name, value in request.headers.items()}

        best: tuple[MatchLevel, list[_Group]] | None = None
        for level in self._levels(request, rule):
            groups = self._groups(level, keys, conditional, live_headers, rule)
            if not groups:
                continue
            if best is None:
                best = (level, groups)
            for group in groups:
                record = group.next_unused(self._used, self.affinity)
                if record is not None:
                    return self._hit(keys, level, group, record, exhausted=False)
        if best is not None:
            level, groups = best
            record = groups[0].last(self.affinity)
            return self._hit(keys, level, groups[0], record, exhausted=True)
        return MatchResult(keys=keys, reason="no_match", candidates=self._nearest(keys, request))

    # ── levels ───────────────────────────────────────────────────────────

    def _levels(self, request: LiveRequest, rule: SiteRule | None) -> list[MatchLevel]:
        levels: list[MatchLevel] = [level for level in self.config.levels if level != "fuzzy"]
        fuzzy = rule is not None and rule.fuzzy
        if not fuzzy and "fuzzy" in self.config.levels:
            fuzzy = request.method.upper() == "GET" and not request.is_document
        if fuzzy:
            levels.append("fuzzy")
        order: dict[MatchLevel, int] = {"exact": 0, "normalized": 1, "fuzzy": 2}
        return sorted(levels, key=lambda level: order[level])

    def _groups(
        self,
        level: MatchLevel,
        keys: RequestKeys,
        conditional: bool,
        live_headers: set[tuple[str, str]],
        rule: SiteRule | None,
    ) -> list["_Group"]:
        """The candidate groups of one level, best first."""

        def headers_of(record: IndexedRecord) -> int:
            return _shared_headers(record, live_headers, rule)

        def eligible(records: Sequence[IndexedRecord]) -> list[IndexedRecord]:
            return [record for record in records if conditional or record.status != 304]

        if level == "exact":
            found = eligible(self.index.exact(keys.exact))
            return [_Group(found, headers_of)] if found else []
        if level == "normalized":
            found = eligible(self.index.normalized(keys.normalized))
            return [_Group(found, headers_of)] if found else []
        verb = keys.exact[0]
        by_key: dict[str, list[IndexedRecord]] = {}
        scores: dict[str, float] = {}
        for record in eligible(self.index.same_path(keys.host, keys.path)):
            if record.method != verb:
                continue
            score = scores.get(record.keys.normalized)
            if score is None:
                score = scores[record.keys.normalized] = fuzzy_score(
                    keys.params, record.keys.params
                )
            if score >= self.config.fuzzy_threshold:
                by_key.setdefault(record.keys.normalized, []).append(record)
        groups = [
            _Group(found, headers_of, score=scores[key]) for key, found in by_key.items() if found
        ]
        # Score first; between equal scores, the shared request headers, then
        # the earlier recording, then the smaller URL (WPR's tie-break).
        groups.sort(key=lambda g: (-g.score, -g.head_headers(), g.head.order, g.head.url))
        return groups

    def _hit(
        self,
        keys: RequestKeys,
        level: MatchLevel,
        group: "_Group",
        record: IndexedRecord,
        *,
        exhausted: bool,
    ) -> MatchResult:
        self._used.add((record.run_id, record.record_id))
        if self.affinity is None:
            self.affinity = record.run_id
        return MatchResult(
            keys=keys,
            record=record,
            level=level,
            fuzzy_score=group.score if level == "fuzzy" else None,
            exhausted=exhausted,
            reason=record.miss_reason,
        )

    # ── ranges ───────────────────────────────────────────────────────────

    def _match_range(self, keys: RequestKeys, header: str) -> MatchResult:
        """A ``Range`` request: answered from the byte-range map, never from a cursor."""
        ranges = self.index.ranges
        if not ranges.knows(keys.normalized):
            return MatchResult(keys=keys, reason="no_match")
        wanted = parse_range(header)
        cover = ranges.cover(keys.normalized, *wanted) if wanted is not None else None
        if cover is None:
            return MatchResult(keys=keys, reason="range_uncovered")
        first = cover.parts[0][0]
        level: MatchLevel = "exact" if first.keys.exact[1] == keys.exact[1] else "normalized"
        return MatchResult(keys=keys, record=first, level=level, range=cover)

    # ── misses ───────────────────────────────────────────────────────────

    def _nearest(self, keys: RequestKeys, request: LiveRequest) -> tuple[Candidate, ...]:
        """The records of the same host nearest to *request*: same method, same path, score."""
        verb = request.method.upper()
        ranked = sorted(
            (
                (
                    record.method != verb,
                    record.keys.path != keys.path,
                    -fuzzy_score(keys.params, record.keys.params),
                    record.order,
                    record.url,
                    record,
                )
                for record in self.index.same_host(keys.host)
            ),
            key=lambda item: item[:5],
        )
        return tuple(
            Candidate(
                record_id=record.record_id,
                method=record.method,
                url=record.url,
                score=-negative_score,
                consumed=(record.run_id, record.record_id) in self._used,
            )
            for _, _, negative_score, _, _, record in ranked[:NEAREST]
        )


class _Group:
    """Records under one key, in pop order: affinity run first, recording order, then ties."""

    def __init__(
        self,
        records: Sequence[IndexedRecord],
        headers_of: Callable[[IndexedRecord], int],
        *,
        score: float = 1.0,
    ) -> None:
        self.records = list(records)
        self.score = score
        self._headers_of = headers_of

    @property
    def head(self) -> IndexedRecord:
        return min(self.records, key=lambda r: (r.order, r.url, r.record_id))

    def head_headers(self) -> int:
        return self._headers_of(self.head)

    def _ranked(self, affinity: str | None) -> list[IndexedRecord]:
        return sorted(
            self.records,
            key=lambda r: (
                affinity is not None and r.run_id != affinity,
                r.order,
                -self._headers_of(r),
                r.started_at if r.started_at is not None else float("inf"),
                r.url,
                r.record_id,
            ),
        )

    def next_unused(self, used: set[tuple[str, str]], affinity: str | None) -> IndexedRecord | None:
        return next(
            (r for r in self._ranked(affinity) if (r.run_id, r.record_id) not in used), None
        )

    def last(self, affinity: str | None) -> IndexedRecord:
        """The last record in recording order — of the affinity run, when it has one here."""
        own = [r for r in self.records if affinity is not None and r.run_id == affinity]
        pool = own or self.records
        return max(pool, key=lambda r: (r.order, r.url, r.record_id))


def _shared_headers(
    record: IndexedRecord, live: set[tuple[str, str]], rule: SiteRule | None
) -> int:
    """How many of *record*'s request headers the live request carries with the same value."""
    count = 0
    for name, value in record.request_headers.items():
        lowered = name.lower()
        if lowered in _STRIPPED or (rule is not None and rule.ignores_header(lowered)):
            continue
        if (lowered, value) in live:
            count += 1
    return count


def _grouped(params: Params) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for name, value in params:
        grouped.setdefault(name, []).append(value)
    return {name: tuple(sorted(values)) for name, values in grouped.items()}
