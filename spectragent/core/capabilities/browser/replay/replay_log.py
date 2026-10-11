"""The replay log, ``replay.jsonl`` (§4.14 "回放日志").

The replay server writes every decision it makes — a hit, a repeated
exhausted record, a synthesised preflight, a miss — into one
:class:`ReplayLog`; ``browser.network``'s replay-mode session drains it at
each collection into that step's ``replay.jsonl``. **The step whose
collection takes an entry is the step it is attributed to** (the capability
knows no step numbers — the model network evidence already follows), and the
episode is the run.

Writing side: :class:`ReplayLog` hands the archive side plain JSON dicts
(``drain()``), so ``archive/`` never imports a replay type
(``ReplayLogFeed``). It folds repeats — a polling page can miss a thousand
times a minute: past ``log_repeat_limit`` identical entries in one
collection window, the rest are only counted and come out as one
``summary`` entry with ``count``. Identical means every field but when it
happened (``captured_at``, ``cycle``) and the candidates listed — the same
decision (kind, reason, answer, status, request class, frame, ``diverged``,
record) about the same request in the same frame — where the request is
named by its canonical key (a cache-busted poll is one request asked again)
or, without one (a WebSocket, an escaped request), by its URL. So different
URLs never fold together, and a divergence is never folded into misses that
are not one.

Reading side, over the :class:`~spectragent.core.archive.source.ArchiveReader`
Protocol (no trace import here): :func:`replay_log` (one step),
:func:`iter_replay_log` (every step, then the trailing commits) and
:func:`diverged_steps` — "this step diverged" is derived at read time: the
step's log holds a main-frame document miss. No ``StepRecord`` field
carries it (M31's ``restoration`` is separate).

.. note::
   No ``from __future__ import annotations`` — the browser replay package
   rule.
"""

import hashlib
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.browser.archive.network import REPLAY_LOG_NAME
from spectragent.core.capabilities.browser.replay.archive_index import NETWORK_ARCHIVER
from spectragent.core.capabilities.browser.replay.miss_policy import MissReason, RequestClass

#: What one entry records. ``hit``: served from the archive; ``exhausted``:
#: every candidate was used and the last one was served again; ``synthetic``:
#: a CORS preflight the archive does not hold, answered 204; ``miss``: not
#: served from the archive (answered by the miss policy, or unanswerable);
#: ``summary``: entries folded by the rate limit.
EntryKind = Literal["hit", "exhausted", "synthetic", "miss", "summary"]
#: How the request was answered: fulfilled, failed, or not at all (Chrome
#: never paused it — a WebSocket handshake, a nested worker's script).
Answer = Literal["fulfilled", "failed", "none"]
#: Longest key kept verbatim in an entry; a longer one is its sha256.
MAX_KEY_CHARS = 512


class ReplayCandidate(BaseModel):
    """One recorded exchange near a missed request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    method: str
    url: str
    score: float
    consumed: bool


class ReplayLogEntry(BaseModel):
    """One line of ``replay.jsonl``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind
    #: Wall-clock seconds of the decision.
    captured_at: float
    #: The observation cycle it happened in.
    cycle: int
    # ── the request ──
    method: str
    url: str
    resource_type: str | None = None
    frame_id: str | None = None
    #: For a document: ``main`` (the page target's own frame) or ``sub``.
    frame: Literal["main", "sub"] | None = None
    target_id: str | None = None
    session_id: str | None = None
    # ── a hit (and the record a ``too_large`` / ``body_unavailable`` miss matched) ──
    record_id: str | None = None
    run_id: str | None = None
    level: Literal["exact", "normalized", "fuzzy"] | None = None
    fuzzy_score: float | None = None
    #: ``bytes N-E/T`` of a range answer.
    range: str | None = None
    # ── the answer ──
    request_class: RequestClass | None = None
    reason: MissReason | None = None
    answer: Answer | None = None
    status: int | None = None
    #: The request's canonical key and body key (sha256 when longer than 512 chars).
    match_key: str | None = None
    body_key: str | None = None
    candidates: list[ReplayCandidate] = Field(default_factory=list)
    #: A main-frame document miss: the replay left the recording here.
    diverged: bool = False
    # ── a summary ──
    #: The kind of the entries a ``summary`` folds, and how many it folds
    #: (beyond the ``log_repeat_limit`` written in full).
    folded_kind: Literal["hit", "exhausted", "synthetic", "miss"] | None = None
    count: int | None = None


#: Fields that differ between repeats of one decision and never keep two
#: entries apart: when it happened, and the candidates the archive still held.
_PER_OCCURRENCE = frozenset({"captured_at", "cycle", "candidates"})

_FoldKey = tuple[tuple[str, Any], ...]


def _fold_key(entry: ReplayLogEntry) -> _FoldKey:
    """What makes two entries the same decision about the same request (module docstring)."""
    identity = entry.model_dump(exclude=set(_PER_OCCURRENCE))
    if entry.match_key is not None:
        # The canonical key names the request; the URL may differ in volatile parameters.
        del identity["url"]
    return tuple(identity.items())


class ReplayLog:
    """The run's replay log: entries since the last collection, repeats folded."""

    def __init__(self, repeat_limit: int = 3) -> None:
        if repeat_limit < 1:
            raise ValueError(f"repeat_limit must be >= 1, got {repeat_limit}")
        self.repeat_limit = repeat_limit
        self._entries: list[ReplayLogEntry] = []
        self._seen: dict[_FoldKey, int] = {}
        #: Per key: the last folded entry and how many were folded.
        self._folded: dict[_FoldKey, tuple[ReplayLogEntry, int]] = {}
        #: Failures of the server's own code since the last drain.
        self._errors: list[str] = []
        self._diverged_total = 0

    def append(self, entry: ReplayLogEntry) -> None:
        if entry.kind == "summary":
            raise ValueError("summaries are made by the log, not appended to it")
        if entry.diverged:
            self._diverged_total += 1
        key = _fold_key(entry)
        seen = self._seen.get(key, 0) + 1
        self._seen[key] = seen
        if seen <= self.repeat_limit:
            self._entries.append(entry)
            return
        _, folded = self._folded.get(key, (entry, 0))
        self._folded[key] = (entry, folded + 1)

    @property
    def diverged_total(self) -> int:
        """Every ``diverged`` entry ever appended — folded or not.

        A monotonic count of the run's main-frame document misses: each
        appended entry with ``diverged`` adds one, including one past the
        repeat limit that only a ``summary`` will show, and :meth:`drain`
        leaves it alone (``browser.network`` drains the log at every
        collection). It is what a checkpoint restoration reads to ask "did
        the replay leave the recording since I last looked?" — the
        capability keeps the total it last saw (§4.14 "恢复：重定位与校验").
        """
        return self._diverged_total

    def fail(self, message: str) -> None:
        """The server could not decide a request: the next :meth:`drain` raises (never silent)."""
        self._errors.append(message)

    def drain(self) -> list[dict[str, Any]]:
        """Everything since the last drain as JSON-ready dicts; the window starts afresh.

        One synchronous swap: nothing appended during a collection is lost
        or counted twice.

        Raises:
            RuntimeError: The server failed to decide a request since the last
                drain (:meth:`fail`); the log would be incomplete. The entries
                stay for the next drain.
        """
        if self._errors:
            errors, self._errors = self._errors, []
            raise RuntimeError(
                f"the replay server failed and its log would be incomplete: {'; '.join(errors)}"
            )
        entries, self._entries = self._entries, []
        folded, self._folded = self._folded, {}
        self._seen = {}
        summaries = [
            last.model_copy(
                update={
                    "kind": "summary",
                    "folded_kind": last.kind,
                    "count": count,
                    "candidates": [],
                }
            )
            for last, count in folded.values()
        ]
        return [
            entry.model_dump(mode="json", exclude_none=True) for entry in (*entries, *summaries)
        ]

    def __len__(self) -> int:
        return len(self._entries) + len(self._folded)


def short_key(key: str | None) -> str | None:
    """*key* itself, or ``sha256:<hex>`` when it is longer than :data:`MAX_KEY_CHARS`."""
    if key is None or len(key) <= MAX_KEY_CHARS:
        return key
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


# ── reading ───────────────────────────────────────────────────────────


def replay_log(reader: ArchiveReader, step: int) -> list[ReplayLogEntry]:
    """The replay log *step*'s collection took (``[]`` when it took none)."""
    entries: list[ReplayLogEntry] = []
    for entry in reader.archive_files(step, NETWORK_ARCHIVER):
        if entry.name == REPLAY_LOG_NAME:
            entries.extend(reader.archive_lines(entry.path, ReplayLogEntry))
    return entries


def iter_replay_log(reader: ArchiveReader) -> Iterator[tuple[int | None, ReplayLogEntry]]:
    """``(step, entry)`` for every entry of the run, in step order, then the trailing ones (``None``)."""
    for step, entry in reader.iter_archive(NETWORK_ARCHIVER, include_trailing=True):
        if entry.name == REPLAY_LOG_NAME:
            for line in reader.archive_lines(entry.path, ReplayLogEntry):
                yield step, line


def diverged_steps(reader: ArchiveReader) -> list[int]:
    """The steps whose replay log holds a main-frame document miss, in order.

    A divergence the run's close-out collection took belongs to no step; it
    is in :func:`iter_replay_log` under ``None``.
    """
    return sorted(
        {
            step
            for step, entry in iter_replay_log(reader)
            if step is not None and entry.diverged and entry.kind in ("miss", "summary")
        }
    )
