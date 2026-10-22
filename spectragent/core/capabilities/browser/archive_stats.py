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
  matched, counted by endpoint, by field and by the combination of fields
  that differed together.

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

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.browser.replay.archive_index import (
    BodyLine,
    ExchangeLine,
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
from spectragent.core.capabilities.browser.replay.replay_log import (
    KEY_DIGEST_PREFIX,
    ReplayLogEntry,
    short_key,
)

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
        opaque_body: The two bodies differ, but one of them is known only by
            a digest, so which fields differ is unknown.
        weight: How many misses it stands for (a folded summary's count).
    """

    host: str
    method: str
    path: str
    query: tuple[str, ...]
    body: tuple[str, ...]
    opaque_body: bool
    weight: int


@dataclass(frozen=True)
class FieldSet:
    """One combination of differing fields, and how many near misses differ in exactly it."""

    query: tuple[str, ...]
    body: tuple[str, ...]
    opaque_body: bool
    misses: int


@dataclass(frozen=True)
class SiteRuleCandidate:
    """The near misses of one endpoint: what a rule scoped to it would have to ignore.

    Attributes:
        host: The host.
        method: The method.
        path: The path.
        misses: How many near misses (folded ones counted by how many they fold).
        query: Per query parameter, how many of those misses it differed in.
        body: Per body field, how many of those misses it differed in.
        opaque_body: How many of those misses had bodies that differ where a
            digest hides which fields.
        field_sets: Every distinct combination of differing fields, most
            frequent first: the fields that change together.
    """

    host: str
    method: str
    path: str
    misses: int
    query: Mapping[str, int]
    body: Mapping[str, int]
    opaque_body: int
    field_sets: tuple[FieldSet, ...]


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


def _body_fields(body_key: str | None) -> tuple[str, Params | None]:
    """``(digest, fields)`` of a canonical body key: ``fields`` is ``None`` when only a digest is known.

    ``digest`` identifies the body for an equality check either way: the
    replay log shortens a key longer than its cap to ``sha256:<hex>``, and
    the canonicaliser replaces a body past its own cap, or one that does not
    parse, by a single digest parameter.
    """
    if body_key is None:
        return "", ()
    if body_key.startswith(KEY_DIGEST_PREFIX):
        return body_key, None
    params = decode_params(body_key)
    if len(params) == 1 and params[0][0] in _DIGEST_PARAMS:
        return body_key, None
    return body_key, params


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
    misses carry no candidates and do not count.

    Raises:
        ReplayArchiveError: A candidate names a record *recorded* does not
            hold: the replay was not served from this archive.
    """
    records = {record.record_id: record for record in recorded.index.records}
    found: list[NearMiss] = []
    for _step, entry, weight in decisions:
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
        live_digest, live_body = _body_fields(entry.body_key)
        recorded_digest, recorded_body = _body_fields(record.keys.body_key)
        body: list[str] = []
        opaque = False
        if live_body is not None and recorded_body is not None:
            body = _differing(live_body, recorded_body)
        else:  # compared as the replay log wrote the live key: shortened past its cap
            opaque = live_digest != (short_key(recorded_digest) or "")
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
            )
        )
    return found


def site_rule_candidates(found: Iterable[NearMiss]) -> list[SiteRuleCandidate]:
    """Near misses by endpoint (host, method, path), most frequent first.

    Per endpoint: how many misses, how many of them each query parameter
    and each body field differed in, how many bodies differed only by a
    digest, and each distinct combination of differing fields with its
    count — a parameter that always appears with another is a set a rule
    would ignore together. Ties are ordered by host, path and method, and
    a combination's ties by its fields, so the result is deterministic.
    """
    misses: Counter[tuple[str, str, str]] = Counter()
    query: dict[tuple[str, str, str], Counter[str]] = {}
    body: dict[tuple[str, str, str], Counter[str]] = {}
    opaque: Counter[tuple[str, str, str]] = Counter()
    sets: dict[tuple[str, str, str], Counter[tuple[tuple[str, ...], tuple[str, ...], bool]]] = {}
    for miss in found:
        endpoint = (miss.host, miss.path, miss.method)
        misses[endpoint] += miss.weight
        query.setdefault(endpoint, Counter()).update(dict.fromkeys(miss.query, miss.weight))
        body.setdefault(endpoint, Counter()).update(dict.fromkeys(miss.body, miss.weight))
        opaque[endpoint] += miss.weight if miss.opaque_body else 0
        sets.setdefault(endpoint, Counter())[(miss.query, miss.body, miss.opaque_body)] += (
            miss.weight
        )
    return [
        SiteRuleCandidate(
            host=host,
            method=method,
            path=path,
            misses=misses[(host, path, method)],
            query=dict(sorted(query[(host, path, method)].items())),
            body=dict(sorted(body[(host, path, method)].items())),
            opaque_body=opaque[(host, path, method)],
            field_sets=tuple(
                FieldSet(query=fields[0], body=fields[1], opaque_body=fields[2], misses=count)
                for fields, count in sorted(
                    sets[(host, path, method)].items(), key=lambda item: (-item[1], item[0])
                )
            ),
        )
        for host, path, method in sorted(misses, key=lambda key: (-misses[key], key))
    ]


__all__ = [
    "CHALLENGE_STATUSES",
    "EXPLICIT_NO_BODY",
    "FINAL_STATES",
    "NO_BODY_NEEDED",
    "WITH_BODY",
    "BodyCompleteness",
    "ChallengeDocument",
    "FieldSet",
    "NearMiss",
    "SiteRuleCandidate",
    "body_completeness",
    "challenge_documents",
    "challenge_signals",
    "near_misses",
    "revisit_saving",
    "site_rule_candidates",
]
