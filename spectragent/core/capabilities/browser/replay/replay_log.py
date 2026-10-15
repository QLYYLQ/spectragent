"""The replay log, ``replay.jsonl`` (§4.14 "回放日志").

The replay server writes every decision it makes — a hit, a repeated
exhausted record, a synthesised preflight, a miss — into one
:class:`ReplayLog`; the ``browser.replay_log`` archiver's session
(``archive/replay_log.py``) drains it at each collection into that step's
``replay.jsonl``. Collection assigns the batch to a step; it does not claim
that step's action caused these asynchronous events. Every event keeps its
own occurrence time and request identity. The capability knows no step
numbers, and the episode is the run.

Writing side: :class:`ReplayLog` hands the archive side plain JSON dicts
(``drain()``), so ``archive/`` never imports a replay type
(``ReplayLogFeed``). ``full`` preserves each event. Explicit ``summary``
mode folds repeats — a polling page can miss a thousand times a minute:
past ``log_repeat_limit`` identical entries in one collection window, the
rest come out as one ``summary`` with ``count``, ``first_captured_at`` and
``last_captured_at``. It does not preserve each folded event's time.
Identical means every field but when it
happened (``captured_at``, ``cycle``) and the candidates listed — the same
decision (kind, reason, answer, status, request class, frame, ``diverged``,
record, the callback rewrite's parameter and recorded name) about the same
request in the same frame — where the request is named by its canonical key
(a cache-busted poll is one request asked again, and so is a JSONP poll
asking a fresh callback name each time) or, without one (a WebSocket, an
escaped request), by its URL. So different URLs never fold together, and a
divergence is never folded into misses that are not one.

Reading side, over the :class:`~spectragent.core.archive.source.ArchiveReader`
Protocol (no trace import here), finding the files by the archiver that
writes them: :func:`replay_log` (one step),
:func:`iter_replay_log` (every step, then the trailing commits),
:func:`iter_decisions` (the same entries with the folding undone: a
``summary`` read as the kind it folds, weighted by how many it folds — the
one place that rule is spelled; :func:`document_miss_total` and the flywheel
metrics count through it) and :func:`diverged_steps` — "this step diverged"
is derived at read time: the step's log holds a main-frame document miss. No
``StepRecord`` field carries it (M31's ``restoration`` is separate).
"""

import hashlib
import json
from collections.abc import Iterator
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.core.archive.source import ArchiveReader
from spectragent.core.capabilities.browser.archive.replay_log import (
    REPLAY_LOG_ARCHIVER,
    REPLAY_LOG_NAME,
)
from spectragent.core.capabilities.browser.replay.jsonp import CallbackRewrite
from spectragent.core.capabilities.browser.replay.miss_policy import MissReason, RequestClass

#: What one entry records. ``hit``: served from the archive; ``exhausted``:
#: every candidate was used and the last one was served again; ``synthetic``:
#: a CORS preflight the archive does not hold, answered 204; ``miss``: not
#: served from the archive (answered by the miss policy, or unanswerable);
#: ``summary``: entries folded by the rate limit.
EntryKind = Literal["hit", "exhausted", "synthetic", "miss", "summary"]
LogMode = Literal["full", "summary"]
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
    #: The selected request rule and its explicit response exhaustion policy.
    rule_id: str | None = None
    exhaustion_policy: Literal["repeat_last", "miss"] | None = None
    #: ``bytes N-E/T`` of a range answer.
    range: str | None = None
    #: A JSONP answer served calling the callback name this request asked for
    #: instead of the recorded one: which parameter, which two names (``jsonp.py``).
    callback_rewrite: CallbackRewrite | None = None
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
    count: int | None = Field(default=None, ge=1)
    #: Occurrence times of the first and last events represented by the summary.
    first_captured_at: float | None = None
    last_captured_at: float | None = None

    @model_validator(mode="after")
    def check_summary(self) -> Self:
        summary_fields = (
            self.folded_kind,
            self.count,
            self.first_captured_at,
            self.last_captured_at,
        )
        if self.kind == "summary":
            if any(value is None for value in summary_fields):
                raise ValueError("a summary requires folded_kind, count and first/last_captured_at")
            if self.captured_at != self.last_captured_at:
                raise ValueError("a summary's captured_at must be its last_captured_at")
        elif any(value is not None for value in summary_fields):
            raise ValueError("only a summary carries folding metadata")
        return self

    @property
    def occurrence_times_complete(self) -> bool:
        """Whether this entry preserves an occurrence time for every decision it represents.

        :func:`iter_decisions` keeps this distinction after normalising a
        summary's kind for counting. Its time bounds do not reconstruct the
        folded event sequence.
        """
        return self.folded_kind is None


#: Fields that differ between repeats of one decision and never keep two
#: entries apart: when it happened, and the candidates the archive still held.
_PER_OCCURRENCE = frozenset({"captured_at", "cycle", "candidates"})


def _fold_key(entry: ReplayLogEntry) -> str:
    """What makes two entries the same decision about the same request (module docstring)."""
    identity = entry.model_dump(mode="json", exclude=set(_PER_OCCURRENCE))
    if entry.match_key is not None:
        # The canonical key names the request; the URL may differ in volatile parameters.
        del identity["url"]
    rewrite = entry.callback_rewrite
    if rewrite is not None:
        # The name asked for is part of the URL, so it is per occurrence too: a
        # poll asking a fresh name each time is still one request asked again.
        identity["callback_rewrite"] = (rewrite.parameter, rewrite.recorded)
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


class ReplayLog:
    """The run's replay log, preserving all events unless summary mode is selected."""

    def __init__(self, *, mode: LogMode = "full", repeat_limit: int = 3) -> None:
        if mode not in ("full", "summary"):
            raise ValueError(f"mode must be 'full' or 'summary', got {mode!r}")
        if repeat_limit < 1:
            raise ValueError(f"repeat_limit must be >= 1, got {repeat_limit}")
        self.mode = mode
        self.repeat_limit = repeat_limit
        self._entries: list[ReplayLogEntry] = []
        self._seen: dict[str, int] = {}
        #: Per key: the last folded entry, how many were folded and the first event's time.
        self._folded: dict[str, tuple[ReplayLogEntry, int, float]] = {}
        #: Failures of the server's own code since the last drain.
        self._errors: list[str] = []
        self._diverged_total = 0

    def append(self, entry: ReplayLogEntry) -> None:
        if entry.kind == "summary" or not entry.occurrence_times_complete:
            raise ValueError("summaries are made by the log, not appended to it")
        if entry.diverged:
            self._diverged_total += 1
        if self.mode == "full":
            self._entries.append(entry)
            return
        key = _fold_key(entry)
        seen = self._seen.get(key, 0) + 1
        self._seen[key] = seen
        if seen <= self.repeat_limit:
            self._entries.append(entry)
            return
        _, folded, first_captured_at = self._folded.get(key, (entry, 0, entry.captured_at))
        self._folded[key] = (entry, folded + 1, first_captured_at)

    @property
    def diverged_total(self) -> int:
        """Every ``diverged`` entry ever appended — folded or not.

        A monotonic count of the run's main-frame document misses: each
        appended entry with ``diverged`` adds one, including one past the
        repeat limit that only a ``summary`` will show, and :meth:`drain`
        leaves it alone (``browser.replay_log`` drains the log at every
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
                    "first_captured_at": first_captured_at,
                    "last_captured_at": last.captured_at,
                    "candidates": [],
                }
            )
            for last, count, first_captured_at in folded.values()
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
    for entry in reader.archive_files(step, REPLAY_LOG_ARCHIVER):
        if entry.name == REPLAY_LOG_NAME:
            entries.extend(reader.archive_lines(entry.path, ReplayLogEntry))
    return entries


def iter_replay_log(reader: ArchiveReader) -> Iterator[tuple[int | None, ReplayLogEntry]]:
    """``(step, entry)`` for every entry of the run, in step order, then the trailing ones (``None``)."""
    for step, entry in reader.iter_archive(REPLAY_LOG_ARCHIVER, include_trailing=True):
        if entry.name == REPLAY_LOG_NAME:
            for line in reader.archive_lines(entry.path, ReplayLogEntry):
                yield step, line


def iter_decisions(reader: ArchiveReader) -> Iterator[tuple[int | None, ReplayLogEntry, int]]:
    """``(step, entry, weight)`` for every decision of the run, in :func:`iter_replay_log` order.

    An entry written in full weighs 1. A ``summary`` stands for the entries
    the log folded past ``log_repeat_limit``: it comes back as the kind it
    folds (``folded_kind``), weighing ``count`` — so a polling page that
    missed a thousand times weighs a thousand, not the three written in
    full. Its folding metadata and time bounds remain present and
    ``occurrence_times_complete`` stays false. A summary's ``candidates``
    are empty (the log drops them when it folds).
    """
    for step, entry in iter_replay_log(reader):
        if entry.kind != "summary":
            yield step, entry, 1
        else:
            # Summary validation guarantees both values; keep the metadata
            # when exposing its decision kind so consumers can see the loss
            # of per-occurrence details.
            assert entry.folded_kind is not None and entry.count is not None
            yield step, entry.model_copy(update={"kind": entry.folded_kind}), entry.count


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


def document_miss_total(reader: ArchiveReader) -> int:
    """The run's main-frame document misses: what :attr:`ReplayLog.diverged_total` counted.

    The same rule, read back: every ``diverged`` entry counts once, and a
    ``summary`` of folded ``diverged`` entries counts every entry it folds
    (``count``) — so a polling page that missed its document a thousand
    times counts a thousand, not the three written in full. The trailing
    (close-out) collections count too. ``0`` for a run with no replay log
    (a live run). The training export's ``max_document_misses`` filter reads
    it (arch §4.15).
    """
    return sum(weight for _step, entry, weight in iter_decisions(reader) if entry.diverged)
