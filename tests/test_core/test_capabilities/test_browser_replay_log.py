"""The replay log: entries, the repeat limit, the ``browser.replay_log`` archiver, the readers.

§4.14 "回放日志". The step whose collection takes an entry is the step it is
attributed to; "diverged" is derived at read time from main-frame document
misses. The readers find the log by the archiver that writes it. The live
counterpart (entries attributed to step, frame and resourceType in a real
replay) is ``test_browser_replay_live.py``.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.archive.source import open_archive
from spectragent.core.capabilities.browser.archive import NetworkArchiver, ReplayLogArchiver
from spectragent.core.capabilities.browser.archive.network import NetworkSession
from spectragent.core.capabilities.browser.archive.redaction import MARKER_BROWSER_REDACTED
from spectragent.core.capabilities.browser.archive.replay_log import (
    REPLAY_LOG_NAME,
    ReplayLogSession,
)
from spectragent.core.capabilities.browser.recorder import RecorderTarget
from spectragent.core.capabilities.browser.replay.replay_log import (
    MAX_KEY_CHARS,
    ReplayCandidate,
    ReplayLog,
    ReplayLogEntry,
    diverged_steps,
    document_miss_total,
    iter_decisions,
    iter_replay_log,
    replay_log,
    short_key,
)
from spectragent.core.trace.encoding import encode_archive_file
from spectragent.core.trace.models import TraceStatus
from spectragent.core.trace.reader import TraceSchemaError
from tests._helpers.archive_recorder import ScriptedRecorder, open_session
from tests._helpers.trace_builder import agent_result, new_wal, step_record


def _entry(kind: str = "miss", **fields: Any) -> ReplayLogEntry:
    base: dict[str, Any] = {
        "kind": kind,
        "captured_at": 1.0,
        "cycle": 1,
        "method": "GET",
        "url": "http://shop.test/poll",
        "resource_type": "XHR",
        "match_key": "http://shop.test/poll?__wb_method=GET",
    }
    if kind == "miss":
        base.update(request_class="subresource", reason="no_match", answer="fulfilled", status=404)
    if kind in ("hit", "exhausted"):
        base.update(
            record_id="rec-1", run_id="run-a", level="exact", answer="fulfilled", status=200
        )
    base.update(fields)
    return ReplayLogEntry(**base)


# ── the entry ─────────────────────────────────────────────────────────


class TestTheEntry:
    def test_unknown_fields_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            ReplayLogEntry(**{**_entry().model_dump(), "extra": 1})
        with pytest.raises(ValidationError):
            ReplayCandidate(record_id="r", method="GET", url="u", score=1.0, consumed=False, x=1)  # type: ignore[call-arg]

    def test_no_switch_vocabulary_beside_the_receptors(self) -> None:
        """No field named like a strategy and no Literal arm naming one (receptor hygiene)."""
        for model in (ReplayLogEntry, ReplayCandidate):
            assert not {"source", "policy", "network_source", "miss_policy"} & set(
                model.model_fields
            )
            for field in model.model_fields.values():
                arms = _literal_arms(field.annotation)
                assert not {"live", "archive", "strict"} & arms, (model, field)

    def test_a_long_key_is_its_sha256(self) -> None:
        long = "k" * (MAX_KEY_CHARS + 1)
        assert short_key("short") == "short" and short_key(None) is None
        digest = short_key(long)
        assert digest is not None and digest.startswith("sha256:") and len(digest) == 71


def _literal_arms(annotation: Any) -> set[str]:
    arms: set[str] = set()
    if typing.get_origin(annotation) is typing.Literal:
        arms.update(str(a) for a in typing.get_args(annotation))
    for arg in typing.get_args(annotation):
        arms |= _literal_arms(arg)
    return arms


# ── the log ───────────────────────────────────────────────────────────


class TestTheRepeatLimit:
    def test_repeats_past_the_limit_fold_into_one_summary(self) -> None:
        log = ReplayLog(repeat_limit=3)
        for n in range(5):
            log.append(_entry(captured_at=float(n)))
        drained = log.drain()
        assert [line["kind"] for line in drained] == ["miss", "miss", "miss", "summary"]
        summary = drained[-1]
        assert summary["folded_kind"] == "miss" and summary["count"] == 2
        assert summary["captured_at"] == 4.0, "the summary carries the last folded entry"

    def test_different_keys_never_fold_together(self) -> None:
        log = ReplayLog(repeat_limit=1)
        log.append(_entry())
        log.append(_entry(reason="too_large", record_id="rec-9"))
        log.append(_entry(method="POST"))
        log.append(_entry(match_key="other"))
        log.append(_entry("hit"))
        assert [line["kind"] for line in log.drain()] == ["miss", "miss", "miss", "miss", "hit"]

    def test_a_cache_busted_poll_is_one_request(self) -> None:
        """Repeats are told apart by canonical key, not URL: the poll still folds."""
        log = ReplayLog(repeat_limit=1)
        for n in range(3):
            log.append(_entry(url=f"http://shop.test/poll?_={1700000000000 + n}", cycle=n))
        drained = log.drain()
        assert [line["kind"] for line in drained] == ["miss", "summary"]
        assert drained[-1]["count"] == 2

    def test_requests_without_a_canonical_key_fold_by_url(self) -> None:
        """WebSockets and escaped requests never reach the matcher: each URL stays visible."""

        def socket(host: str) -> ReplayLogEntry:
            return _entry(
                url=f"ws://{host}/socket",
                match_key=None,
                resource_type="WebSocket",
                request_class=None,
                reason="websocket",
                answer="none",
                status=None,
            )

        log = ReplayLog(repeat_limit=3)
        for n in range(5):
            log.append(socket(f"h{n}.example.test"))
        assert [(line["kind"], line["url"]) for line in log.drain()] == [
            ("miss", f"ws://h{n}.example.test/socket") for n in range(5)
        ]
        for _ in range(4):
            log.append(socket("h0.example.test"))
        assert [line["kind"] for line in log.drain()] == ["miss", "miss", "miss", "summary"]

    def test_a_diverged_document_miss_is_never_folded_into_other_misses(self) -> None:
        """Same canonical key, another decision: the divergence is written in full."""
        log = ReplayLog(repeat_limit=3)
        for _ in range(3):
            log.append(_entry())
        document = _entry(
            resource_type="Document",
            frame="main",
            request_class="document",
            answer="failed",
            status=None,
            diverged=True,
        )
        log.append(document)
        log.append(_entry())
        drained = log.drain()
        assert [(line["kind"], line["diverged"]) for line in drained] == [
            ("miss", False),
            ("miss", False),
            ("miss", False),
            ("miss", True),
            ("summary", False),
        ]
        assert ReplayLogEntry.model_validate(drained[3]) == document

    def test_diverged_total_counts_folded_entries(self) -> None:
        """Every appended divergence counts — the folded ones too — and drain() never resets it."""
        log = ReplayLog(repeat_limit=1)
        document = _entry(
            resource_type="Document",
            frame="main",
            request_class="document",
            answer="failed",
            status=None,
            diverged=True,
        )
        assert log.diverged_total == 0
        for _ in range(3):
            log.append(document)
        log.append(_entry())
        log.append(_entry())
        drained = log.drain()
        assert [(line["kind"], line.get("diverged")) for line in drained] == [
            ("miss", True),
            ("miss", False),
            ("summary", True),
            ("summary", False),
        ], "two of the three divergences were folded into a summary"
        assert log.diverged_total == 3, "folded divergences count; ordinary misses do not"
        log.append(document)
        assert log.diverged_total == 4
        log.drain()
        assert log.diverged_total == 4, "drain() leaves the count alone"

    def test_each_collection_is_its_own_window(self) -> None:
        log = ReplayLog(repeat_limit=1)
        log.append(_entry())
        log.append(_entry())
        assert [line["kind"] for line in log.drain()] == ["miss", "summary"]
        log.append(_entry())
        assert [line["kind"] for line in log.drain()] == ["miss"]
        assert log.drain() == [] and len(log) == 0

    def test_drained_entries_are_json_and_read_back(self) -> None:
        log = ReplayLog()
        candidate = ReplayCandidate(
            record_id="r1", method="GET", url="http://shop.test/a", score=0.5, consumed=True
        )
        log.append(_entry(candidates=[candidate], diverged=False))
        (line,) = log.drain()
        assert "record_id" not in line, "None fields are left out"
        assert ReplayLogEntry.model_validate(json.loads(json.dumps(line))).candidates == [candidate]

    def test_summaries_are_the_logs_own(self) -> None:
        with pytest.raises(ValueError, match="summaries"):
            ReplayLog().append(_entry("summary"))
        with pytest.raises(ValueError):
            ReplayLog(repeat_limit=0)


# ── browser.replay_log ────────────────────────────────────────────────


class TestTheReplayLogArchiver:
    def test_it_alone_needs_the_replay_log(self) -> None:
        """The cooperation check finds the archiver by this need, never by its id."""
        assert ReplayLogArchiver.needs == frozenset({"replay_log"})
        assert "replay_log" not in NetworkArchiver.needs

    def test_its_only_knob_and_its_privacy_marker(self) -> None:
        archiver = ReplayLogArchiver()
        assert archiver.archiver_id == "browser.replay_log"
        assert archiver.allow_session_state is False
        assert ReplayLogArchiver(allow_session_state=True).allow_session_state is True
        assert archiver.privacy_markers == frozenset({MARKER_BROWSER_REDACTED})
        assert archiver.requires_replayable is False

    def test_the_network_archiver_always_records_traffic(self) -> None:
        """``browser.network`` has one meaning: a replay's log in the context changes nothing."""
        session, _ = open_session(NetworkArchiver(require_replayable=False), replay_log=ReplayLog())
        assert isinstance(session, NetworkSession)

    def test_opening_it_in_a_run_that_replays_nothing_raises(self) -> None:
        with pytest.raises(RuntimeError, match="does not replay an archive"):
            open_session(ReplayLogArchiver())

    async def test_a_replaying_run_records_its_log(self) -> None:
        log = ReplayLog()
        session, _ = open_session(ReplayLogArchiver(), replay_log=log, clock=lambda: 42.0)
        assert isinstance(session, ReplayLogSession)
        target = RecorderTarget(session_id="s", target_id="t", type="page")
        assert session.target_commands(target) == ()
        assert session.fetch_patterns(target) == ()
        assert session.event_methods() == ()
        assert not session.serves_requests and not session.wants_cycles
        assert await session.collect() == [], "an empty window writes nothing"

        log.append(_entry("hit"))
        log.append(_entry())
        (written,) = await session.collect()
        assert written.archiver_id == "browser.replay_log" and written.name == REPLAY_LOG_NAME
        assert written.scope == "step" and written.meta.captured_at == 42.0
        lines = [json.loads(line) for line in written.data.splitlines()]
        assert [line["kind"] for line in lines] == ["hit", "miss"]
        assert await session.collect() == []


# ── reading it back ───────────────────────────────────────────────────


async def _committed(
    base: Path, steps: dict[int | None, list[ReplayLogEntry]], log: ReplayLog | None = None
) -> Path:
    """A run whose ``browser.replay_log`` session committed these logs (``None``: trailing).

    Each batch is what the real :class:`ReplayLogSession` collects, committed
    with ``encode_archive_file`` like ``TraceWriter`` does.
    """
    wal = new_wal(base, "run-replay")
    log = log if log is not None else ReplayLog(repeat_limit=100)
    session, _ = open_session(ReplayLogArchiver(), replay_log=log)
    for step, entries in steps.items():
        for entry in entries:
            log.append(entry)
        files = await session.collect()
        if step is None:
            wal.commit_trailing(extra_files=[encode_archive_file(f, trailing=0) for f in files])
        else:
            wal.commit_step(
                step,
                step_record(step),
                extra_files=[encode_archive_file(f, step=step) for f in files],
            )
    wal.write_result(agent_result())
    wal.finalize(TraceStatus.COMPLETE)
    return wal.run_dir


class TestReading:
    async def test_each_step_reads_its_own_log(self, tmp_path: Path) -> None:
        main_miss = _entry(
            resource_type="Document",
            frame="main",
            request_class="document",
            answer="failed",
            status=None,
            diverged=True,
        )
        run = await _committed(
            tmp_path,
            {
                1: [_entry("hit")],
                2: [main_miss, _entry()],
                3: [],
                None: [_entry(frame="main", diverged=True)],
            },
        )
        reader = open_archive(run)
        assert [entry.kind for entry in replay_log(reader, 1)] == ["hit"]
        assert [entry.diverged for entry in replay_log(reader, 2)] == [True, False]
        assert replay_log(reader, 3) == [] and replay_log(reader, 9) == []
        assert [step for step, _ in iter_replay_log(reader)] == [1, 2, 2, None]
        assert diverged_steps(reader) == [2], "a trailing divergence belongs to no step"

    async def test_the_document_miss_total_is_the_live_counter(self, tmp_path: Path) -> None:
        """Read back, the run's document misses equal ``diverged_total`` — folded ones and the
        close-out's included (the export's ``max_document_misses`` reads it, arch §4.15)."""
        document = _entry(
            resource_type="Document",
            frame="main",
            request_class="document",
            answer="failed",
            status=None,
            diverged=True,
        )
        log = ReplayLog(repeat_limit=1)
        run = await _committed(
            tmp_path,
            {
                1: [_entry("hit"), document, document, document, _entry(), _entry()],
                2: [_entry()],
                None: [_entry(frame="main", diverged=True)],
            },
            log,
        )
        reader = open_archive(run)
        kinds = [(entry.kind, entry.diverged) for _step, entry in iter_replay_log(reader)]
        assert ("summary", True) in kinds, "the fixture folds divergences into a summary"
        assert log.diverged_total == 4
        assert document_miss_total(reader) == log.diverged_total

    async def test_a_live_run_has_no_document_misses(self, tmp_path: Path) -> None:
        assert document_miss_total(open_archive(await _committed(tmp_path, {1: []}))) == 0

    def test_the_log_is_read_from_the_archiver_that_writes_it(self, tmp_path: Path) -> None:
        """A ``replay.jsonl`` under another archiver is not the replay log."""
        wal = new_wal(tmp_path, "run-other")
        elsewhere = ArchiveFile(
            archiver_id="browser.network",
            name=REPLAY_LOG_NAME,
            media_type="application/x-ndjson",
            data=json.dumps(_entry(frame="main", diverged=True).model_dump(mode="json")).encode()
            + b"\n",
            meta=ArchiveProvenance(captured_at=1.0),
        )
        wal.commit_step(1, step_record(1), extra_files=[encode_archive_file(elsewhere, step=1)])
        wal.write_result(agent_result())
        wal.finalize(TraceStatus.COMPLETE)
        reader = open_archive(wal.run_dir)
        assert replay_log(reader, 1) == [] and list(iter_replay_log(reader)) == []
        assert document_miss_total(reader) == 0

    def test_a_line_that_is_not_an_entry_names_the_file_and_line(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path, "run-bad")
        bad = ArchiveFile(
            archiver_id="browser.replay_log",
            name=REPLAY_LOG_NAME,
            media_type="application/x-ndjson",
            data=json.dumps(_entry().model_dump(mode="json")).encode()
            + b"\n"
            + b'{"kind": "miss", "surprise": 1}\n',
            meta=ArchiveProvenance(captured_at=1.0),
        )
        wal.commit_step(1, step_record(1), extra_files=[encode_archive_file(bad, step=1)])
        wal.write_result(agent_result())
        wal.finalize(TraceStatus.COMPLETE)
        with pytest.raises(TraceSchemaError, match=r"replay\.jsonl.*line 2|line 2.*replay\.jsonl"):
            replay_log(open_archive(wal.run_dir), 1)


class TestDecisions:
    """``iter_decisions``: the log with its folding undone — the one place the rule is spelled."""

    async def test_a_summary_reads_as_the_kind_it_folds_weighing_its_count(
        self, tmp_path: Path
    ) -> None:
        log = ReplayLog(repeat_limit=1)
        run = await _committed(
            tmp_path,
            {1: [_entry("hit"), _entry(), _entry(), _entry(), _entry()], None: [_entry()]},
            log,
        )
        reader = open_archive(run)
        written = [(step, entry.kind) for step, entry in iter_replay_log(reader)]
        assert written == [(1, "hit"), (1, "miss"), (1, "summary"), (None, "miss")]

        decisions = list(iter_decisions(reader))

        assert [(step, entry.kind, weight) for step, entry, weight in decisions] == [
            (1, "hit", 1),
            (1, "miss", 1),
            (1, "miss", 3),  # the summary of the three folded misses
            (None, "miss", 1),
        ]
        assert sum(w for _s, e, w in decisions if e.kind == "miss") == 5, "every miss appended"

    async def test_the_document_miss_total_is_counted_through_it(self, tmp_path: Path) -> None:
        document = _entry(
            resource_type="Document",
            frame="main",
            request_class="document",
            answer="failed",
            status=None,
            diverged=True,
        )
        run = await _committed(
            tmp_path, {1: [document] * 4, 2: [_entry()]}, ReplayLog(repeat_limit=2)
        )
        reader = open_archive(run)
        diverged = [(e.kind, w) for _s, e, w in iter_decisions(reader) if e.diverged]
        assert diverged == [("miss", 1), ("miss", 1), ("miss", 2)]
        assert document_miss_total(reader) == 4

    async def test_a_live_run_has_no_decisions(self, tmp_path: Path) -> None:
        assert list(iter_decisions(open_archive(await _committed(tmp_path, {1: []})))) == []


def test_the_scripted_recorder_is_untouched_by_the_replay_session() -> None:
    """The replay-log session sends nothing on the recorder (no collect-time reads)."""
    recorder = ScriptedRecorder()
    session, _ = open_session(ReplayLogArchiver(), recorder, replay_log=ReplayLog())
    assert isinstance(session, ReplayLogSession)
    assert getattr(recorder, "sent", []) == []
