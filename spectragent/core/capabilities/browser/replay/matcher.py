"""Replay matching with explicit allowed levels and per-request exhaustion.

The strongest allowed level with candidates owns the decision. Consumption
is synchronous and shared across levels; exhaustion never silently changes
to a weaker resource. Range matching obeys the same level and rule bounds,
while using the byte-range map without consuming ordinary sequence cursors.

A record without a body keeps its place in the sequence: the cursor
consumes it like any other. The applicable rule's ``unavailable`` policy
then decides its answer — the miss policy (``miss``), or another record of
the same sequence (``serve_identical``) when every available record there
has the same status and body, the consumed record's own recorded status (if
any) is that status, and the source request did not fail on the network
(never configurable). ``repeat_last`` repeats the sequence's last
available response (arch §4.14 "匹配").
"""

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import ConfigDict, Field, field_validator
from pydantic.dataclasses import dataclass as pydantic_dataclass

from spectragent.core.capabilities.browser.archive.redaction import (
    DEFAULT_STRIPPED_REQUEST_HEADERS,
)
from spectragent.core.capabilities.browser.replay.archive_index import (
    ArchiveIndex,
    IndexedRecord,
    RangeCover,
    SourceOutcome,
)
from spectragent.core.capabilities.browser.replay.canonical import (
    Params,
    RequestKeys,
    request_keys,
)
from spectragent.core.capabilities.browser.replay.miss_policy import MissReason
from spectragent.core.capabilities.browser.replay.site_rules import (
    ExhaustionPolicy,
    MatchLevel,
    SiteRule,
    SiteRules,
    UnavailablePolicy,
)

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
#: Source outcomes no record ever answers for: the source page saw the network
#: fail, so the replay must fail too. Fixed here, never a rule's or a run's knob.
NEVER_SUBSTITUTED: frozenset[SourceOutcome] = frozenset({"network_failed"})
#: ``bytes=N-``, ``bytes=N-M``, ``bytes=-K`` (one range; a multi-range list is not served).
_RANGE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)
_STRIPPED = frozenset(DEFAULT_STRIPPED_REQUEST_HEADERS)


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class MatcherConfig:
    """How hard the replay tries to match (``network_source.matcher``).

    Frozen settings shared by the live matcher and generated configuration.
    """

    levels: tuple[MatchLevel, ...] = Field(
        default=("exact", "normalized"),
        min_length=1,
        description=(
            "Allowed matching levels; fixed strength order is exact, normalized, fuzzy. "
            "Request rules may restrict this set and never enable an excluded level."
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
            "Directory of explicitly scoped rule documents, e.g. configs/replay_rules. "
            "Serving a recorded request requires an explicit exhaustion rule."
        ),
    )

    @field_validator("levels")
    @classmethod
    def _each_level_once(cls, levels: tuple[MatchLevel, ...]) -> tuple[MatchLevel, ...]:
        if len(set(levels)) != len(levels):
            raise ValueError(f"each matching level once: got {levels}")
        return levels

    def fingerprint(self) -> dict[str, object]:
        """The knobs as the replay fingerprint records them."""
        return {**dataclasses.asdict(self), "levels": list(self.levels)}


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
            cover reads from) — the one consumed, even when another record
            answers for it; ``None`` on a miss.
        substitute: The available record that answers for a consumed
            record without a body (``serve_identical``); ``None`` otherwise.
        level: The level that found it.
        fuzzy_score: The ``2M/T`` score of a fuzzy hit.
        exhausted: The selected response sequence was consumed; the policy
            either repeats its last available record (with none, the last
            record and its own reason) or reports ``sequence_exhausted``.
        range: The bytes that answer a ``Range`` request.
        reason: ``None`` when the result can be served; otherwise why not —
            ``no_match``, ``sequence_exhausted``, ``match_level_disallowed``,
            ``range_uncovered``, or the chosen record's own
            ``too_large`` / ``body_unavailable``.
        candidates: On ``no_match``, the nearest records of the same host.
    """

    keys: RequestKeys
    record: IndexedRecord | None = None
    substitute: IndexedRecord | None = None
    level: MatchLevel | None = None
    fuzzy_score: float | None = None
    exhausted: bool = False
    range: RangeCover | None = None
    reason: MissReason | None = None
    candidates: tuple[Candidate, ...] = ()
    rule_id: str | None = None
    exhaustion_policy: ExhaustionPolicy | None = None
    unavailable_policy: UnavailablePolicy | None = None

    @property
    def servable(self) -> bool:
        return self.reason is None

    @property
    def served(self) -> IndexedRecord | None:
        """The record whose recorded response is sent: the substitute, else the record."""
        return self.substitute if self.substitute is not None else self.record


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
        self.rules.validate_levels(self.config.levels)
        #: ``(run_id, record_id)`` of every record served (or repeated) so far.
        self._used: set[tuple[str, str]] = set()
        #: The run of the episode's first hit.
        self.affinity: str | None = None

    # ── the one entry point ──────────────────────────────────────────────

    def match(self, request: LiveRequest) -> MatchResult:
        """Select and consume the record for *request* — synchronously, in one call."""
        rule = self.rules.for_request(request.method, request.url)
        keys = request_keys(request.method, request.url, request.body, request.content_type, rule)
        if request.header("range") is not None:
            result = self._match_range(request, keys, rule)
        else:
            result = self._match_sequence(request, keys, rule)
        return dataclasses.replace(
            result,
            rule_id=rule.id if rule else None,
            exhaustion_policy=rule.exhaustion if rule else None,
            unavailable_policy=rule.unavailable if rule else None,
        )

    def _match_sequence(
        self, request: LiveRequest, keys: RequestKeys, rule: SiteRule | None
    ) -> MatchResult:
        conditional = any(request.header(name) is not None for name in CONDITIONAL_HEADERS)
        live_headers = {(name.lower(), value) for name, value in request.headers.items()}
        for level in self._levels(request, rule):
            groups = self._groups(level, keys, conditional, live_headers, rule)
            if not groups:
                continue
            selected = self.rules.require_for_request(request.method, request.url)
            group = groups[0]
            record = group.next_unused(self._used, self.affinity)
            if record is not None:
                substitute = (
                    self._substitute(group, record)
                    if selected.unavailable == "serve_identical"
                    else None
                )
                return self._hit(keys, level, group, record, exhausted=False, substitute=substitute)
            if selected.exhaustion == "repeat_last":
                # The last available response; with none, the last record and its own reason.
                last = group.last_available(self.affinity) or group.last(self.affinity)
                return self._hit(keys, level, group, last, exhausted=True)
            return MatchResult(
                keys=keys,
                level=level,
                exhausted=True,
                reason="sequence_exhausted",
                fuzzy_score=group.score if level == "fuzzy" else None,
                candidates=self._nearest(keys, request),
            )
        return MatchResult(keys=keys, reason="no_match", candidates=self._nearest(keys, request))

    def _levels(self, request: LiveRequest, rule: SiteRule | None) -> list[MatchLevel]:
        allowed = set(self.config.levels)
        if rule is not None and rule.levels is not None:
            allowed.intersection_update(rule.levels)
        if (request.method.upper() != "GET" or request.is_document) and not (rule and rule.fuzzy):
            allowed.discard("fuzzy")
        return [level for level in ("exact", "normalized", "fuzzy") if level in allowed]

    def _groups(
        self,
        level: MatchLevel,
        keys: RequestKeys,
        conditional: bool,
        live_headers: set[tuple[str, str]],
        rule: SiteRule | None,
        records: Sequence[IndexedRecord] | None = None,
    ) -> list["_Group"]:
        """The candidate groups of one level, best first."""

        def headers_of(record: IndexedRecord) -> int:
            return _shared_headers(record, live_headers, rule)

        def eligible(records: Sequence[IndexedRecord]) -> list[IndexedRecord]:
            selected_id = rule.id if rule is not None else None
            found: list[IndexedRecord] = []
            for record in records:
                if not conditional and record.status == 304:
                    continue
                recorded_rule = self.rules.for_request(record.method, record.url)
                recorded_id = recorded_rule.id if recorded_rule is not None else None
                if recorded_id == selected_id:
                    found.append(record)
            return found

        if level == "exact":
            found = eligible(
                self.index.exact(keys.exact)
                if records is None
                else [record for record in records if record.keys.exact == keys.exact]
            )
            return [_Group(found, headers_of)] if found else []
        if level == "normalized":
            found = eligible(
                self.index.normalized(keys.normalized)
                if records is None
                else [record for record in records if record.keys.normalized == keys.normalized]
            )
            return [_Group(found, headers_of)] if found else []
        verb = keys.exact[0]
        by_key: dict[str, list[IndexedRecord]] = {}
        scores: dict[str, float] = {}
        pool = self.index.same_path(keys.host, keys.path) if records is None else records
        for record in eligible(pool):
            if (
                record.method != verb
                or record.keys.host != keys.host
                or record.keys.path != keys.path
                or record.keys.port != keys.port
            ):
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
        substitute: IndexedRecord | None = None,
    ) -> MatchResult:
        """Consume *record*; a *substitute* answers for it and stays unused at its own place."""
        self._used.add((record.run_id, record.record_id))
        if self.affinity is None:
            self.affinity = record.run_id
        return MatchResult(
            keys=keys,
            record=record,
            substitute=substitute,
            level=level,
            fuzzy_score=group.score if level == "fuzzy" else None,
            exhausted=exhausted,
            reason=None if substitute is not None else record.miss_reason,
        )

    def _substitute(self, group: "_Group", record: IndexedRecord) -> IndexedRecord | None:
        """The available record that answers for *record*, which has no body — if one may.

        Only for a ``body_unavailable`` record whose source request did not
        fail on the network (an ``unknown`` outcome is eligible), and only
        when every available record of the sequence has the same status and
        the same recorded body digest (a record without a digest proves
        nothing). A consumed record whose response status was recorded (its
        headers arrived before the body was lost) must have that same status:
        the source server answered it, and a different answer is never
        substituted for it. One without a status (cancelled before any
        response, still in flight) stays eligible. Among the available
        records: the affinity run, the most request headers shared with the
        live request, the nearest in recording order (the earlier on a tie).
        """
        if record.miss_reason != "body_unavailable" or record.source_outcome in NEVER_SUBSTITUTED:
            return None
        available = [r for r in group.records if r.servable]
        if not available or any(r.payload_sha256 is None for r in available):
            return None
        answers = {(r.status, r.payload_sha256) for r in available}
        if len(answers) != 1:
            return None
        ((status, _digest),) = answers
        if record.status is not None and record.status != status:
            return None
        return group.nearest(available, record, self.affinity)

    # ── ranges ───────────────────────────────────────────────────────────

    def _match_range(
        self, request: LiveRequest, keys: RequestKeys, rule: SiteRule | None
    ) -> MatchResult:
        """Select a resource under the ordinary level boundary, then cover its bytes."""
        ranges = self.index.ranges
        header = request.header("range")
        assert header is not None
        wanted = parse_range(header)
        live_headers = {(name.lower(), value) for name, value in request.headers.items()}
        allowed = self._levels(request, rule)
        for level in allowed:
            groups = self._groups(level, keys, True, live_headers, rule, ranges.records)
            if not groups:
                continue
            self.rules.require_for_request(request.method, request.url)
            group = groups[0]
            cover = (
                ranges.cover(group.head.keys.normalized, *wanted, records=group.records)
                if wanted is not None
                else None
            )
            if cover is None:
                return MatchResult(keys=keys, level=level, reason="range_uncovered")
            return MatchResult(
                keys=keys,
                record=cover.parts[0][0],
                level=level,
                range=cover,
                fuzzy_score=group.score if level == "fuzzy" else None,
            )
        for level in ("exact", "normalized", "fuzzy"):
            if level not in allowed and self._groups(
                level, keys, True, live_headers, rule, ranges.records
            ):
                return MatchResult(keys=keys, reason="match_level_disallowed")
        return MatchResult(keys=keys, reason="no_match", candidates=self._nearest(keys, request))

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
        return _last(self.records, affinity)

    def last_available(self, affinity: str | None) -> IndexedRecord | None:
        """The last record with a servable body, like :meth:`last`; ``None`` when none has one."""
        available = [r for r in self.records if r.servable]
        return _last(available, affinity) if available else None

    def nearest(
        self, pool: Sequence[IndexedRecord], to: IndexedRecord, affinity: str | None
    ) -> IndexedRecord:
        """The record of *pool* that answers for *to*: affinity, shared headers, distance, order."""
        return min(
            pool,
            key=lambda r: (
                affinity is not None and r.run_id != affinity,
                -self._headers_of(r),
                abs(r.order - to.order) if r.run_id == to.run_id else float("inf"),
                r.order,
                r.url,
                r.record_id,
            ),
        )


def _last(records: Sequence[IndexedRecord], affinity: str | None) -> IndexedRecord:
    """The last of *records* in recording order — of the affinity run, when it has one there."""
    own = [r for r in records if affinity is not None and r.run_id == affinity]
    return max(own or records, key=lambda r: (r.order, r.url, r.record_id))


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
