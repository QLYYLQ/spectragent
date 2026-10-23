"""Offline statistics over a recorded run's browser evidence (§4.16 "browser 一侧的读法").

The flywheel metrics (``spectragent/flywheel/metrics.py``) organise the
calls; what the browser's evidence *means* is decided here, next to the
code that writes and replays it:

- :func:`body_completeness` — every ``browser.network`` record's final body
  state (its **last** index line, as the replay loader reads it: a record
  whose last line is ``pending`` was incomplete at run end, a
  ``streamed_later`` record has a body when its ``kind: body`` line says
  ``ok``), counted by state;
- :func:`revisit_saving` — what the run's WARC ``revisit`` records saved
  against writing every repeated payload in full (M29's accounting: the
  stored length of the response record a revisit points at, minus the
  revisit's own — negative for a tiny payload);
- :func:`challenge_documents` — document responses that carry an anti-bot
  challenge's marks (status 403 / 429 / 503 with ``cf-mitigated: challenge``,
  or ``server: cloudflare`` with a ``cf-ray``), main frame and sub-frame
  alike: a challenge iframe blocks the page too. Response headers only — a
  block page without these headers is not recognised;
- :func:`near_misses` and :func:`site_rule_candidates` — replay misses whose
  nearest recorded candidate is the same endpoint (same host, path and
  method) but for some query parameters or body fields: what a rule scoped
  to that endpoint (``ignore_query`` / ``ignore_body_paths``) would have
  matched. Requests that do not feed the page are set aside by what the
  recording and the replay's rules say about them (:data:`NOT_FEEDING_PAGE`);
  the rest are counted per site and endpoint in replay runs, not requests,
  and ranked by how strongly their near misses go with failed restoration
  checks (:func:`failure_association`).

Pure functions over what they are handed: index lines
(:func:`~spectragent.core.capabilities.browser.replay.archive_index.iter_index_lines`),
replay-log decisions
(:func:`~spectragent.core.capabilities.browser.replay.replay_log.iter_decisions`),
a loaded source archive
(:func:`~spectragent.core.capabilities.browser.replay.archive_index.load_archive`)
or an :class:`~spectragent.core.archive.source.ArchiveReader`. No
``core.trace`` and no ``spectragent.flywheel`` import — the boundary
``replay_log.py`` keeps (``tests/test_core/test_archive/test_archive_boundaries.py``).
"""

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.browser.replay.archive_index import (
    BodyLine,
    ExchangeLine,
    IndexedRecord,
    IndexEntry,
    PendingLine,
    RecordedArchive,
    ReplayArchiveError,
    iter_index_lines,
    load_archive,
)
from spectragent.core.capabilities.browser.replay.canonical import (
    BODY_PARAM_PREFIX,
    BODY_SHA256_PARAM,
    POST_DATA_PARAM,
    Params,
    canonical_query,
    decode_params,
    host_of,
)
from spectragent.core.capabilities.browser.replay.replay_log import ReplayLogEntry
from spectragent.core.capabilities.browser.replay.site_rules import SiteRule

#: Final states with a body in the archive: written whole, or streamed as a later blob.
WITH_BODY: tuple[str, ...] = ("ok", "streamed_later")
#: Final states that need no body: a redirect hop, a WebSocket handshake.
NO_BODY_NEEDED: tuple[str, ...] = ("redirect", "websocket")
#: Final states that say explicitly there is no body: over the size cap, or
#: never completed (a record whose last line is ``pending`` included).
EXPLICIT_NO_BODY: tuple[str, ...] = ("too_large", "incomplete")
#: Every final state, in report order.
FINAL_STATES: tuple[str, ...] = (*WITH_BODY, *NO_BODY_NEEDED, *EXPLICIT_NO_BODY)

#: Response statuses an anti-bot challenge answers a document with.
CHALLENGE_STATUSES: frozenset[int] = frozenset({403, 429, 503})


@dataclass(frozen=True)
class BodyCompleteness:
    """The final body state of every network record of one run (or several, added up).

    ``by_state`` maps each of :data:`FINAL_STATES` to how many records ended
    in it; a state no record ended in is absent.
    """

    by_state: Mapping[str, int] = field(default_factory=dict)

    @property
    def records(self) -> int:
        return sum(self.by_state.values())

    @property
    def with_body(self) -> int:
        return sum(self.by_state.get(state, 0) for state in WITH_BODY)

    @property
    def no_body_needed(self) -> int:
        return sum(self.by_state.get(state, 0) for state in NO_BODY_NEEDED)

    @property
    def explicit_no_body(self) -> int:
        return sum(self.by_state.get(state, 0) for state in EXPLICIT_NO_BODY)

    def __add__(self, other: "BodyCompleteness") -> "BodyCompleteness":
        total = Counter(self.by_state)
        total.update(other.by_state)
        return BodyCompleteness(by_state=dict(sorted(total.items())))


def _final_state(line: IndexEntry) -> str:
    """The body state a record's last line says it ended in."""
    if isinstance(line, PendingLine):
        return "incomplete"
    if isinstance(line, BodyLine):
        if line.body_status == "ok":
            return "streamed_later"
        return line.body_status if line.body_status in EXPLICIT_NO_BODY else "incomplete"
    if line.body_status == "streamed_later":  # its body never arrived, and nothing says why
        return "incomplete"
    return line.body_status


def body_completeness(lines: Iterable[IndexEntry]) -> BodyCompleteness:
    """Each record's final body state, counted (module docstring).

    *lines* in index order (``iter_index_lines``). A record is an id an
    ``exchange`` or ``pending`` line names; its state is read from its last
    line of any kind, so a ``body`` line after the exchange settles a
    ``streamed_later`` record. A ``body`` line naming no record is not one.
    """
    last: dict[str, IndexEntry] = {}
    records: set[str] = set()
    for line in lines:
        last[line.record_id] = line
        if not isinstance(line, BodyLine):
            records.add(line.record_id)
    counts = Counter(_final_state(last[record_id]) for record_id in records)
    return BodyCompleteness(by_state=dict(sorted(counts.items())))


def revisit_saving(lines: Iterable[IndexEntry]) -> int:
    """Bytes the run's ``revisit`` records saved against writing each payload in full.

    For every exchange written as a revisit (``revisit_of``): the stored
    length of the full response record it points at, minus its own stored
    length — the same payload under the same compression, so the two differ
    by a few header bytes; a revisit of a tiny payload outweighs it and the
    saving is negative.

    Raises:
        ValueError: A revisit points at a response record *lines* do not
            hold (the archive is incomplete; nothing can be accounted).
    """
    responses = [
        line
        for line in lines
        if isinstance(line, ExchangeLine)
        and line.response_record_id
        and line.warc.response is not None
    ]
    full = {
        line.response_record_id: line.warc.response.length
        for line in responses
        if not line.revisit_of and line.warc.response is not None
    }
    saved = 0
    for line in responses:
        first = line.revisit_of
        if not first or line.warc.response is None:
            continue
        if first not in full:
            raise ValueError(f"revisit of {first!r}, a response record this archive lacks")
        saved += full[first] - line.warc.response.length
    return saved


@dataclass(frozen=True)
class ChallengeDocument:
    """A document response carrying an anti-bot challenge's marks.

    Attributes:
        step: The step whose commit took the exchange (``None``: the run's
            close-out).
        url: The document's URL.
        status: Its response status.
        signals: Which marks it carries, in a fixed order.
    """

    step: int | None
    url: str
    status: int
    signals: tuple[str, ...]


def challenge_signals(status: int | None, headers: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """The challenge marks a response with *status* and *headers* carries (``()``: none).

    ``cf-mitigated: challenge``; or ``server: cloudflare`` together with a
    ``cf-ray`` header — only on a status in :data:`CHALLENGE_STATUSES`.
    Header names and the two values compare case-insensitively.
    """
    if status not in CHALLENGE_STATUSES:
        return ()
    seen: dict[str, list[str]] = {}
    for name, value in headers:
        seen.setdefault(name.strip().lower(), []).append(value.strip().lower())
    signals: list[str] = []
    if "challenge" in seen.get("cf-mitigated", []):
        signals.append("cf-mitigated: challenge")
    if "cloudflare" in seen.get("server", []) and "cf-ray" in seen:
        signals.append("server: cloudflare + cf-ray")
    return tuple(signals)


def challenge_documents(reader: ArchiveReader) -> list[ChallengeDocument]:
    """Every document response of the run that carries challenge marks, in recording order.

    Reads the network archive the way a replay loads it (``load_archive``:
    a response's headers are in its WARC record), so the same archive
    problems raise here: ``ReplayArchiveError`` for a record that is not what
    its index line says, the reader's schema error for a line that does not
    parse.
    """
    steps = {
        line.record_id: step
        for step, line in iter_index_lines(reader)
        if isinstance(line, ExchangeLine)
    }
    found: list[ChallengeDocument] = []
    for record in load_archive(reader).index.records:
        if record.resource_type != "Document" or record.status is None:
            continue
        signals = challenge_signals(record.status, record.response_headers)
        if signals:
            found.append(
                ChallengeDocument(
                    step=steps.get(record.record_id),
                    url=record.url,
                    status=record.status,
                    signals=signals,
                )
            )
    return found


#: Why a near miss says nothing about the page, decided from the recording and
#: the replay's own rules, never from a list of domains. ``rule_answers_204``:
#: a rule the replay used already answers the request 204 (``synthesize_204``);
#: ``ping_or_image``: Chrome typed the request ``Ping`` (``navigator.sendBeacon``
#: and ``<a ping>``: Chrome has no separate beacon type) or ``Image``;
#: ``recorded_empty``: the nearest recorded answer was a 204 or had an empty
#: body. Checked in this order; the first that applies is the reason.
NotFeedingPage = Literal["rule_answers_204", "ping_or_image", "recorded_empty"]
NOT_FEEDING_PAGE: tuple[NotFeedingPage, ...] = (
    "rule_answers_204",
    "ping_or_image",
    "recorded_empty",
)
#: Chrome's resource types for requests whose answer the page does not read.
_NOT_FEEDING_TYPES = frozenset({"Ping", "Image"})


@dataclass(frozen=True)
class NearMiss:
    """One replay miss that only its query parameters or body fields kept from a recorded request.

    Attributes:
        host: The request's host.
        method: Its method (upper case).
        path: Its URL path (``/`` when empty).
        query: The query parameters whose values differ from the nearest
            recorded request's, sorted.
        body: The body fields (flattened paths, as ``ignore_body_paths``
            names them) whose values differ, sorted.
        opaque_body: The two bodies differ, but the canonicaliser keyed one
            of them by a digest (past its cap, or a body that does not
            parse), so which fields differ is unknown: the matcher itself
            had nothing finer.
        weight: How many misses it stands for (a folded summary's count).
        step: The step whose collection took it (``None``: a close-out collection).
        not_feeding_page: Why the request does not feed the page, or ``None``
            when nothing says so (:data:`NotFeedingPage`).
    """

    host: str
    method: str
    path: str
    query: tuple[str, ...]
    body: tuple[str, ...]
    opaque_body: bool
    weight: int
    step: int | None = None
    not_feeding_page: NotFeedingPage | None = None


@dataclass(frozen=True)
class ReplayNearMisses:
    """One replay run's near misses, and how its restoration checks went.

    Attributes:
        run: The run's name: every count :func:`site_rule_candidates` makes
            is in distinct runs (or distinct steps of them).
        site: The site its task belongs to: endpoints are ranked per site.
        misses: Its near misses (:func:`near_misses`).
        checks: Per step with a restoration check, whether it passed.
    """

    run: str
    site: str
    misses: Sequence[NearMiss]
    checks: Mapping[int, bool] = field(default_factory=dict)

    @property
    def failed_at(self) -> int | None:
        """The first step whose restoration check did not pass (``None``: none failed)."""
        return min((step for step, passed in self.checks.items() if not passed), default=None)


@dataclass(frozen=True)
class FieldSet:
    """One combination of differing fields, and in how many replay runs a near miss differed in exactly it."""

    query: tuple[str, ...]
    body: tuple[str, ...]
    opaque_body: bool
    runs: int


@dataclass(frozen=True)
class SiteRuleCandidate:
    """The near misses of one endpoint on one site: what a rule scoped to it would have to ignore.

    Every count but ``requests`` is in replay runs: a request a page repeats
    every second counts once per run, like one it sends once.

    Attributes:
        site: The site of the replay runs counted.
        host: The host.
        method: The method.
        path: The path.
        runs: Replay runs with a near miss at this endpoint.
        failed_restoration_runs: Of those, the runs whose restoration did not
            pass with a near miss here at or before the first failed step.
        failed_check_steps: Steps (of those runs) with a near miss here whose
            own restoration check failed.
        passed_check_steps: Steps with a near miss here whose own check passed.
        failure_association: The lower bound of the 95% Wilson interval of
            ``failed_check_steps / (failed_check_steps + passed_check_steps)``
            (0 without a checked step): what the ranking puts first. A request
            the page makes at every step misses at the steps that restore well
            too, so it ranks below one that misses only where restoration fails.
        requests: Near-miss requests in those runs (folded ones counted by
            how many they fold): context, not ranked on.
        query: Per query parameter, how many of those runs it differed in.
        body: Per body field, how many of those runs it differed in.
        opaque_body: How many of those runs had bodies that differ where a
            digest hides which fields.
        field_sets: Every distinct combination of differing fields, in the
            most runs first: the fields that change together.
    """

    site: str
    host: str
    method: str
    path: str
    runs: int
    failed_restoration_runs: int
    failed_check_steps: int
    passed_check_steps: int
    failure_association: float
    requests: int
    query: Mapping[str, int]
    body: Mapping[str, int]
    opaque_body: int
    field_sets: tuple[FieldSet, ...]


@dataclass(frozen=True)
class NotFeedingPageCount:
    """Near misses set aside for one :data:`NotFeedingPage` reason.

    Attributes:
        endpoints: Distinct (site, host, method, path) they were at.
        requests: The near-miss requests (folded ones by how many they fold).
    """

    endpoints: int
    requests: int


def _differing(live: Sequence[tuple[str, str]], recorded: Sequence[tuple[str, str]]) -> list[str]:
    """Names whose canonical values differ between two parameter lists (present on one side too)."""
    values: dict[str, tuple[list[str], list[str]]] = {}
    for name, value in live:
        values.setdefault(name, ([], []))[0].append(value)
    for name, value in recorded:
        values.setdefault(name, ([], []))[1].append(value)
    return sorted(name for name, (a, b) in values.items() if sorted(a) != sorted(b))


#: Canonical body parameters that stand for a whole body rather than for one of its fields.
_DIGEST_PARAMS = frozenset({BODY_SHA256_PARAM, POST_DATA_PARAM})


def _body_fields(body_key: str | None) -> Params | None:
    """The fields of a canonical body key; ``None`` when the key is only a digest of the body.

    The replay log keeps the live body key as the matcher computed it, so a
    digest is left only where the canonicaliser itself replaced the body by
    one: past its own cap, or a body that does not parse. Fields are named
    only for a canonical parameter string (every item ``name=value``, as
    ``encode_params`` writes it); any other key still identifies the body
    for an equality check, but names no field.
    """
    if not body_key:
        return ()
    if any("=" not in item for item in body_key.split("&")):
        return None
    params = decode_params(body_key)
    if len(params) == 1 and params[0][0] in _DIGEST_PARAMS:
        return None
    return params


def near_misses(
    decisions: Iterable[tuple[int | None, ReplayLogEntry, int]],
    recorded: RecordedArchive,
) -> list[NearMiss]:
    """The ``no_match`` misses of one replay whose nearest recorded request is the same endpoint.

    *decisions* as ``iter_decisions`` yields them; *recorded* is the replay's
    source archive, loaded with the rules the replay used
    (``recorded_site_rules``), so both sides are keyed alike: the live
    request's query is canonicalised with the rule the source's rules select
    for it, its body is the replay log's body key (already keyed with that
    rule), and the candidate's query and body come from the record's own
    keys. A miss counts when its nearest candidate (the first: the matcher
    ranks same method, same path, best score first) has the same host, path
    and method and their query parameters or body fields differ. Folded
    misses carry no candidates and do not count. Each keeps the step whose
    collection took it, and why its request does not feed the page when the
    recording or the rules say so (:data:`NOT_FEEDING_PAGE`).

    Raises:
        ReplayArchiveError: A candidate names a record *recorded* does not
            hold: the replay was not served from this archive.
    """
    records = {record.record_id: record for record in recorded.index.records}
    found: list[NearMiss] = []
    for step, entry, weight in decisions:
        if entry.kind != "miss" or entry.reason != "no_match" or not entry.candidates:
            continue
        if weight <= 0:
            continue
        best = entry.candidates[0]
        host = host_of(entry.url)
        method = entry.method.upper()
        if not host or host != host_of(best.url) or method != best.method.upper():
            continue
        try:
            live = urlsplit(entry.url.split("#", 1)[0])
            path = live.path or "/"
        except ValueError:
            continue
        record = records.get(best.record_id)
        if record is None:
            raise ReplayArchiveError(
                "a replay candidate names a record its source archive does not hold",
                run_id=recorded.run_id,
                record_id=best.record_id,
            )
        if path != record.keys.path:
            continue
        rule = recorded.rules.for_request(method, entry.url)
        recorded_query = [
            (name, value)
            for name, value in record.keys.params
            if not name.startswith(BODY_PARAM_PREFIX)
        ]
        query = _differing(canonical_query(live.query, rule), recorded_query)
        live_body = _body_fields(entry.body_key)
        recorded_body = _body_fields(record.keys.body_key)
        body: list[str] = []
        opaque = False
        if live_body is not None and recorded_body is not None:
            body = _differing(live_body, recorded_body)
        else:  # one side is a digest: whether the bodies differ, but not where
            opaque = (entry.body_key or "") != (record.keys.body_key or "")
        if not query and not body and not opaque:
            continue
        found.append(
            NearMiss(
                host=host,
                method=method,
                path=path,
                query=tuple(query),
                body=tuple(body),
                opaque_body=opaque,
                weight=weight,
                step=step,
                not_feeding_page=_not_feeding_page(entry, record, rule),
            )
        )
    return found


def _not_feeding_page(
    entry: ReplayLogEntry, record: IndexedRecord, rule: SiteRule | None
) -> NotFeedingPage | None:
    """Why *entry*'s request does not feed the page (:data:`NOT_FEEDING_PAGE`), or ``None``."""
    if rule is not None and rule.synthesizes_204(entry.url):
        return "rule_answers_204"
    if (record.resource_type or entry.resource_type) in _NOT_FEEDING_TYPES:
        return "ping_or_image"
    if record.status == 204 or (record.body_status == "ok" and record.payload_length == 0):
        return "recorded_empty"
    return None


_Endpoint = tuple[str, str, str, str]  # site, host, path, method
_Fields = tuple[tuple[str, ...], tuple[str, ...], bool]


#: The standard normal quantile of a two-sided 95% interval (Wilson's bound).
_WILSON_Z = 1.96


def failure_association(failed: int, passed: int) -> float:
    """The lower bound of the 95% Wilson score interval of ``failed / (failed + passed)``.

    A share with its sample size: 32 of 32 failed checks ranks above 1 of 1,
    and 57 of 58 above 267 of 323. ``0.0`` when nothing was checked.
    """
    n = failed + passed
    if n == 0:
        return 0.0
    share = failed / n
    z2 = _WILSON_Z * _WILSON_Z
    centre = share + z2 / (2 * n)
    margin = _WILSON_Z * math.sqrt(share * (1 - share) / n + z2 / (4 * n * n))
    return round(max(0.0, (centre - margin) / (1 + z2 / n)), 6)


@dataclass
class _EndpointTally:
    runs: int = 0
    failed: int = 0
    failed_steps: int = 0
    passed_steps: int = 0
    requests: int = 0
    query: Counter[str] = field(default_factory=Counter)
    body: Counter[str] = field(default_factory=Counter)
    opaque: int = 0
    sets: Counter[_Fields] = field(default_factory=Counter)


def site_rule_candidates(runs: Iterable[ReplayNearMisses]) -> list[SiteRuleCandidate]:
    """Near misses by site and endpoint (host, method, path), counted in replay runs and ranked.

    A near miss whose request does not feed the page (``not_feeding_page``)
    is left out (:func:`not_feeding_page_counts` says how many). Counts are
    of distinct runs: a run counts once for an endpoint, once for each field
    that differed in any of its near misses there and once for each
    combination of fields that differed together. A run counts as a failed
    restoration for an endpoint when it has a near miss there at or before
    its ``failed_at`` step (a close-out collection's never does). The step
    counts are of distinct (run, step) pairs whose step has a restoration
    check, split by whether that check passed.

    Sites in order; within a site, the highest ``failure_association``
    first, then the most failed-restoration runs, then the most runs, ties
    by host, path and method; a combination's ties by its fields — so the
    result is deterministic.
    """
    tallies: dict[_Endpoint, _EndpointTally] = {}
    for run in runs:
        by_endpoint: dict[tuple[str, str, str], list[NearMiss]] = {}
        for miss in run.misses:
            if miss.not_feeding_page is None:
                by_endpoint.setdefault((miss.host, miss.path, miss.method), []).append(miss)
        for (host, path, method), misses in by_endpoint.items():
            tally = tallies.setdefault((run.site, host, path, method), _EndpointTally())
            tally.runs += 1
            failed_at = run.failed_at
            tally.failed += int(
                failed_at is not None
                and any(m.step is not None and m.step <= failed_at for m in misses)
            )
            checked = {m.step for m in misses if m.step is not None and m.step in run.checks}
            tally.passed_steps += sum(1 for step in checked if run.checks[step])
            tally.failed_steps += sum(1 for step in checked if not run.checks[step])
            tally.requests += sum(m.weight for m in misses)
            tally.query.update({name for m in misses for name in m.query})
            tally.body.update({name for m in misses for name in m.body})
            tally.opaque += int(any(m.opaque_body for m in misses))
            tally.sets.update({(m.query, m.body, m.opaque_body) for m in misses})
    association = {
        key: failure_association(tally.failed_steps, tally.passed_steps)
        for key, tally in tallies.items()
    }
    ranked = sorted(
        tallies.items(),
        key=lambda item: (
            item[0][0],
            -association[item[0]],
            -item[1].failed,
            -item[1].runs,
            item[0][1:],
        ),
    )
    return [
        SiteRuleCandidate(
            site=site,
            host=host,
            method=method,
            path=path,
            runs=tally.runs,
            failed_restoration_runs=tally.failed,
            failed_check_steps=tally.failed_steps,
            passed_check_steps=tally.passed_steps,
            failure_association=association[(site, host, path, method)],
            requests=tally.requests,
            query=dict(sorted(tally.query.items())),
            body=dict(sorted(tally.body.items())),
            opaque_body=tally.opaque,
            field_sets=tuple(
                FieldSet(query=fields[0], body=fields[1], opaque_body=fields[2], runs=count)
                for fields, count in sorted(tally.sets.items(), key=lambda i: (-i[1], i[0]))
            ),
        )
        for (site, host, path, method), tally in ranked
    ]


def not_feeding_page_counts(runs: Iterable[ReplayNearMisses]) -> dict[str, NotFeedingPageCount]:
    """The near misses :func:`site_rule_candidates` leaves out, by reason (in :data:`NOT_FEEDING_PAGE` order)."""
    endpoints: dict[str, set[_Endpoint]] = {}
    requests: Counter[str] = Counter()
    for run in runs:
        for miss in run.misses:
            reason = miss.not_feeding_page
            if reason is None:
                continue
            endpoints.setdefault(reason, set()).add((run.site, miss.host, miss.path, miss.method))
            requests[reason] += miss.weight
    return {
        reason: NotFeedingPageCount(endpoints=len(endpoints[reason]), requests=requests[reason])
        for reason in NOT_FEEDING_PAGE
        if reason in endpoints
    }


__all__ = [
    "CHALLENGE_STATUSES",
    "EXPLICIT_NO_BODY",
    "FINAL_STATES",
    "NOT_FEEDING_PAGE",
    "NO_BODY_NEEDED",
    "WITH_BODY",
    "BodyCompleteness",
    "ChallengeDocument",
    "FieldSet",
    "NearMiss",
    "NotFeedingPage",
    "NotFeedingPageCount",
    "ReplayNearMisses",
    "SiteRuleCandidate",
    "body_completeness",
    "challenge_documents",
    "challenge_signals",
    "failure_association",
    "near_misses",
    "not_feeding_page_counts",
    "revisit_saving",
    "site_rule_candidates",
]
