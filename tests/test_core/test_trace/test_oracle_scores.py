"""The oracle score record: committed before ``result.json``, pointed to by the manifest.

``docs/architecture_design.md`` §5.3 "判分记录": ``TraceWriter`` keeps what the
scoring point's ``read_oracle_scores`` returned (``after``, role ``env``, only
when there is an environment) and commits it as ``oracle_scores.json`` in
``on_run_end``, before ``result.json``; an empty list writes no file; the
recording plan's channels do not apply; a write failure is a recording problem
naming ``oracle_scores`` and never changes the outcome. ``RunTrace.oracle_scores()``
reads it back, ``[]`` when there is no record.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.trace import RunTrace, WALWriter, fsio
from spectragent.core.trace.encoding import ORACLE_SCORES_FILE, TraceFile
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.oracle_score import OracleScore
from spectragent.schemas.recording import RecordingPlan
from tests._helpers.trace_builder import CAPTURED_AT, agent_result, new_wal


def _scores() -> list[OracleScore]:
    """A scored entry and a not-scored one, the same on every call."""
    return [
        OracleScore(
            capability_id="android",
            status="scored",
            score=0.5,
            oracle="androidworld",
            oracle_version="abc123",
            provenance={"task": "ContactsAddContact", "params": "sha256:01"},
            started_at=CAPTURED_AT,
            duration_ms=812,
        ),
        OracleScore(
            capability_id="second",
            status="not_scored",
            started_at=CAPTURED_AT,
            duration_ms=60_000,
            reason="timeout",
            detail="no score within 60 s (oracle_timeout)",
        ),
    ]


class TestTheWAL:
    def test_committed_and_read_back_through_the_manifest(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        scores = _scores()

        wal.commit_oracle_scores(scores)

        trace = RunTrace(wal.run_dir)
        assert trace.manifest.oracle_scores == ORACLE_SCORES_FILE == "oracle_scores.json"
        assert trace.oracle_scores() == scores
        (listing,) = [file for file in trace.committed_files if file.kind == "oracle_scores"]
        assert (listing.path, listing.step) == (ORACLE_SCORES_FILE, None)
        assert json.loads(trace.read_committed(ORACLE_SCORES_FILE)) == [
            score.model_dump(mode="json") for score in scores
        ]

    def test_an_empty_list_commits_nothing(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        wal.commit_oracle_scores([])

        trace = RunTrace(wal.run_dir)
        assert trace.manifest.oracle_scores is None
        assert trace.oracle_scores() == []
        assert not (wal.run_dir / ORACLE_SCORES_FILE).exists()
        assert all(file.kind != "oracle_scores" for file in trace.committed_files)

    def test_no_record_reads_as_an_empty_list(self, tmp_path: Path) -> None:
        assert RunTrace(new_wal(tmp_path).run_dir).oracle_scores() == []

    def test_a_committed_record_is_immutable(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        wal.commit_oracle_scores(_scores())
        with pytest.raises(ValueError, match="already committed"):
            wal.commit_oracle_scores(_scores()[:1])
        assert RunTrace(wal.run_dir).oracle_scores() == _scores()

    def test_anything_but_scores_is_refused_before_a_byte_is_written(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        not_scores: Any = [{"capability_id": "android", "status": "scored"}]
        with pytest.raises(ValueError):
            wal.commit_oracle_scores(not_scores)
        assert not (wal.run_dir / ORACLE_SCORES_FILE).exists()
        assert RunTrace(wal.run_dir).manifest.oracle_scores is None

    def test_no_extra_file_may_take_its_name(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        with pytest.raises(ValueError, match="a name the WAL owns"):
            wal.commit_trailing(extra_files=[TraceFile(ORACLE_SCORES_FILE, b"[]")])

    def test_written_before_the_commit_point_it_did_not_happen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash before the manifest rename leaves an orphan no reader sees; the commit is retried."""
        wal = new_wal(tmp_path)
        real = fsio.write_durable

        def crash_on_manifest(path: Path, data: bytes) -> set[Path]:
            if path.name == "manifest.json":
                raise OSError(errno.EIO, "power lost before the commit point", str(path))
            return real(path, data)

        monkeypatch.setattr(fsio, "write_durable", crash_on_manifest)
        with pytest.raises(OSError):
            wal.commit_oracle_scores(_scores())
        assert (wal.run_dir / ORACLE_SCORES_FILE).exists()  # on disk, and an orphan
        trace = RunTrace(wal.run_dir)
        assert trace.oracle_scores() == []
        assert all(file.path != ORACLE_SCORES_FILE for file in trace.committed_files)
        with pytest.raises(KeyError):
            trace.read_committed(ORACLE_SCORES_FILE)

        monkeypatch.setattr(fsio, "write_durable", real)
        wal.commit_oracle_scores(_scores())
        assert RunTrace(wal.run_dir).oracle_scores() == _scores()


class TestAnUnreadableRecord:
    @pytest.mark.parametrize(
        "damage",
        [
            b'[{"capability_id": "android", ',
            b"\xff\xfe",
            b"",
            b'[{"capability_id": "android", "status": "scored"}]',
            b'{"capability_id": "android"}',
            None,
        ],
        ids=["invalid JSON", "invalid UTF-8", "empty", "schema mismatch", "not a list", "missing"],
    )
    def test_names_the_file(self, tmp_path: Path, damage: bytes | None) -> None:
        wal = new_wal(tmp_path)
        wal.commit_oracle_scores(_scores())
        path = wal.run_dir / ORACLE_SCORES_FILE
        if damage is None:
            path.unlink()
        else:
            path.write_bytes(damage)

        with pytest.raises(TraceSchemaError) as caught:
            RunTrace(wal.run_dir).oracle_scores()
        assert caught.value.path == path

    def test_a_manifest_leading_outside_the_run_names_the_manifest(self, tmp_path: Path) -> None:
        wal = new_wal(tmp_path)
        (tmp_path / "elsewhere.json").write_text("[]")
        manifest = wal.run_dir / "manifest.json"
        document = json.loads(manifest.read_text())
        document["oracle_scores"] = "../elsewhere.json"
        manifest.write_text(json.dumps(document))

        trace = RunTrace(wal.run_dir)
        for read in (trace.oracle_scores, lambda: trace.committed_files):
            with pytest.raises(TraceSchemaError) as caught:
                read()
            assert caught.value.path == manifest


async def _closed_run(
    tmp_path: Path, scores: Any, *, recording: RecordingPlan | None = None
) -> TraceWriter:
    """One run through ``TraceWriter`` alone: started, scored, closed out."""
    writer = TraceWriter(trace_dir=str(tmp_path / "traces"), recording=recording)
    await writer.on_run_start(agent=None, env=None, task="t", config={})
    await writer.after_read_oracle_scores(
        scores, args=(), kwargs={"timeout_s": 60.0, "answer": None}
    )
    await writer.on_run_end(result=agent_result())
    return writer


class TestTheTraceWriter:
    def test_the_interception_is_declared_only_with_an_environment(self) -> None:
        writer = TraceWriter()
        declared = {(i.role, i.handler_name) for i in TraceWriter.__intercepts__}
        assert ("env", "after_read_oracle_scores") in declared
        writer.intercept_environment(False)
        assert all(i.method != "read_oracle_scores" for i in writer.__intercepts__)
        writer.intercept_environment(True)
        assert writer.__intercepts__ == TraceWriter.__intercepts__

    async def test_the_scores_are_committed_before_the_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        for name in ("commit_oracle_scores", "write_result"):
            real = getattr(WALWriter, name)

            def spy(self: WALWriter, *args: Any, _real: Any = real, _name: str = name) -> Any:
                order.append(_name)
                return _real(self, *args)

            monkeypatch.setattr(WALWriter, name, spy)

        writer = await _closed_run(tmp_path, _scores())

        assert order == ["commit_oracle_scores", "write_result"]
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.oracle_scores() == _scores()
        assert trace.manifest.status.value == "complete"

    async def test_an_empty_list_writes_no_file(self, tmp_path: Path) -> None:
        writer = await _closed_run(tmp_path, [])
        assert writer.run_dir is not None
        assert not (writer.run_dir / ORACLE_SCORES_FILE).exists()
        trace = RunTrace(writer.run_dir)
        assert (trace.manifest.oracle_scores, trace.manifest.status.value) == (None, "complete")

    async def test_the_recording_plan_does_not_apply(self, tmp_path: Path) -> None:
        nothing = RecordingPlan(metrics=False, errors=False, trajectory=False, observations=False)
        writer = await _closed_run(tmp_path, _scores(), recording=nothing)
        assert writer.run_dir is not None
        assert RunTrace(writer.run_dir).oracle_scores() == _scores()

    async def test_a_write_failure_is_a_recording_problem_and_the_outcome_stands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def full_disk(self: WALWriter, scores: Any) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(WALWriter, "commit_oracle_scores", full_disk)

        writer = await _closed_run(tmp_path, _scores())

        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status.value == "incomplete"
        assert trace.manifest.incomplete_reason == (
            f"oracle_scores: OSError: [Errno {errno.ENOSPC}] No space left on device"
        )
        assert trace.oracle_scores() == []
        result = trace.result
        assert result is not None and result.agent_done == AgentDoneStatus.DONE
        health = result.metrics.recording_health
        assert health is not None
        assert [(p.channel, p.phase) for p in health.problems] == [("oracle_scores", "closeout")]

    async def test_what_is_not_a_list_of_scores_is_a_recording_problem(
        self, tmp_path: Path
    ) -> None:
        """Taking the scores never raises into the scoring point."""
        writer = await _closed_run(tmp_path, [{"not": "a score"}])
        assert writer.run_dir is not None
        trace = RunTrace(writer.run_dir)
        assert trace.oracle_scores() == []
        assert (trace.manifest.incomplete_reason or "").startswith("oracle_scores: ")
