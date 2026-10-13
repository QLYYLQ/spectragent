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
- :func:`site_rule_candidates` — replay misses whose nearest recorded
  candidate is the same request but for its query parameters (same host,
  path and method): what a per-site ``ignore_query`` rule would have matched,
  counted by host and parameter.

Pure functions over what they are handed: index lines
(:func:`~spectragent.core.capabilities.browser.replay.archive_index.iter_index_lines`),
replay-log decisions
(:func:`~spectragent.core.capabilities.browser.replay.replay_log.iter_decisions`)
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
    iter_index_lines,
    load_archive,
)
from spectragent.core.capabilities.browser.replay.canonical import canonical_query, host_of
from spectragent.core.capabilities.browser.replay.replay_log import ReplayLogEntry

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
class SiteRuleCandidate:
    """Replay misses on one host that only their query parameters kept from a recorded request.

    Attributes:
        host: The host.
        misses: How many misses (folded ones counted by how many they fold).
        params: Per query parameter whose values differed, how many of those
            misses it differed in.
    """

    host: str
    misses: int
    params: Mapping[str, int]


def _differing(live: Sequence[tuple[str, str]], recorded: Sequence[tuple[str, str]]) -> list[str]:
    """Names whose canonical values differ between two parameter lists (present on one side too)."""
    values: dict[str, tuple[list[str], list[str]]] = {}
    for name, value in live:
        values.setdefault(name, ([], []))[0].append(value)
    for name, value in recorded:
        values.setdefault(name, ([], []))[1].append(value)
    return sorted(name for name, (a, b) in values.items() if sorted(a) != sorted(b))


def site_rule_candidates(
    decisions: Iterable[tuple[int | None, ReplayLogEntry, int]],
) -> dict[str, SiteRuleCandidate]:
    """Per host, the ``no_match`` misses a query-parameter rule would have matched.

    *decisions* as ``iter_decisions`` yields them. A miss counts when its
    nearest candidate (the first: the matcher ranks same method, same path,
    best score first) has the same host, path and method and their canonical
    queries (``canonical_query``, no site rule) differ — then by host, and by
    every parameter whose values differ. Folded misses carry no candidates
    and do not count.
    """
    misses: Counter[str] = Counter()
    params: dict[str, Counter[str]] = {}
    for _step, entry, weight in decisions:
        if entry.kind != "miss" or entry.reason != "no_match" or not entry.candidates:
            continue
        if weight <= 0:
            continue
        best = entry.candidates[0]
        host = host_of(entry.url)
        if not host or host != host_of(best.url) or entry.method.upper() != best.method.upper():
            continue
        try:
            live, recorded = urlsplit(entry.url), urlsplit(best.url)
        except ValueError:
            continue
        if (live.path or "/") != (recorded.path or "/"):
            continue
        differing = _differing(canonical_query(live.query), canonical_query(recorded.query))
        if not differing:
            continue
        misses[host] += weight
        params.setdefault(host, Counter()).update(dict.fromkeys(differing, weight))
    return {
        host: SiteRuleCandidate(
            host=host, misses=misses[host], params=dict(sorted(params[host].items()))
        )
        for host in sorted(misses)
    }


__all__ = [
    "CHALLENGE_STATUSES",
    "EXPLICIT_NO_BODY",
    "FINAL_STATES",
    "NO_BODY_NEEDED",
    "WITH_BODY",
    "BodyCompleteness",
    "ChallengeDocument",
    "SiteRuleCandidate",
    "body_completeness",
    "challenge_documents",
    "challenge_signals",
    "revisit_saving",
    "site_rule_candidates",
]
