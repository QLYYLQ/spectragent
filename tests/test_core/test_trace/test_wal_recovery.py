"""Sparse commits and manifest replacement failures preserve truthful indexes."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from spectragent.core.trace import fsio
from spectragent.core.trace.encoding import TraceFile
from spectragent.core.trace.models import TraceConfig, TraceManifest, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.recording import RecordingProblem
from tests._helpers.fragment_samples import PNG_BYTES
from tests._helpers.trace_builder import (
    Reply,
    as_chat,
    blob,
    completed_call,
    data_url,
    extra,
    new_wal,
    prompt,
    step_record,
)


def fail_manifest_directory_sync(monkeypatch) -> None:
    """Fail exactly after the next manifest rename, using the real write path."""
    original_write = fsio.write_durable
    original_sync = fsio.fsync_dir
    manifest_renamed = False
    failed = False

    def write(path: Path, data: bytes) -> set[Path]:
        nonlocal manifest_renamed
        directories = original_write(path, data)
        if path.name == "manifest.json":
            manifest_renamed = True
        return directories

    def sync(path: Path) -> None:
        nonlocal failed
        if manifest_renamed and not failed:
            failed = True
            raise OSError(errno.EIO, "manifest directory sync failed", str(path))
        original_sync(path)

    monkeypatch.setattr(fsio, "write_durable", write)
    monkeypatch.setattr(fsio, "fsync_dir", sync)


def test_out_of_order_steps_repair_coverage_without_changing_identity(tmp_path, monkeypatch):
    wal = new_wal(tmp_path)
    first = completed_call(0, 0, "first")
    original_write = fsio.atomic_write

    def fail_manifest(path: Path, data: bytes) -> None:
        raise OSError("temporarily unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(fsio, "atomic_write", fail_manifest)
        with pytest.raises(OSError, match="temporarily unavailable"):
            wal.commit_step(0, step_record(0), llm_calls=[first])
    assert fsio.atomic_write is original_write
    assert (wal.run_dir / "steps/000.json").exists()
    assert RunTrace(wal.run_dir).step(0) is None

    wal.commit_step(1, step_record(1), llm_calls=[completed_call(1, 0, "second")])
    manifest = RunTrace(wal.run_dir).manifest
    assert (manifest.expected_steps, manifest.missing_steps) == (2, [0])
    assert (manifest.last_step_completed, manifest.complete_through) == (1, -1)

    wal.commit_step(0, step_record(0), llm_calls=[first])
    problem = RecordingProblem(
        step=0,
        channel="trajectory",
        phase="commit",
        error="OSError",
        detail="temporarily unavailable",
        occurred_at=1.0,
        recovered=True,
    )
    wal.finalize(TraceStatus.COMPLETE, expected_steps=2, recording_problems=[problem])
    trace = RunTrace(wal.run_dir)
    assert [entry.step for entry in trace.manifest.steps] == [0, 1]
    assert (trace.manifest.last_step_completed, trace.manifest.complete_through) == (1, 1)
    assert trace.manifest.missing_steps == []
    assert trace.manifest.status is TraceStatus.COMPLETE
    assert trace.manifest.recording_problems == [problem]
    assert as_chat(trace.llm_messages(0)[0]).messages[-1].text == "first"
    assert as_chat(trace.llm_messages(1)[0]).messages[-1].text == "second"
    wal.finalize(TraceStatus.COMPLETE)
    assert RunTrace(wal.run_dir).manifest.recording_problems == [problem]
    with pytest.raises(ValueError, match="already committed"):
        wal.commit_step(0, step_record(0))


@pytest.mark.parametrize("status", [TraceStatus.COMPLETE, TraceStatus.INCOMPLETE])
def test_finalization_names_missing_last_segments(tmp_path, status):
    wal = new_wal(tmp_path)
    wal.commit_step(0, step_record(0))
    wal.finalize(status, expected_steps=3)
    manifest = RunTrace(wal.run_dir).manifest
    assert (manifest.expected_steps, manifest.missing_steps) == (3, [1, 2])
    assert manifest.status is TraceStatus.INCOMPLETE
    assert manifest.complete_through == 0
    assert manifest.incomplete_reason == "steps not recorded: 1, 2"


def test_partial_channel_failure_caps_prefix_and_recovery_restores_it(tmp_path):
    wal = new_wal(tmp_path)
    for step in (0, 1, 2):
        wal.commit_step(step, step_record(step))
    problem = RecordingProblem(
        step=1,
        channel="trajectory",
        phase="encode_schema",
        error="ValueError",
        detail="requested output schema was not captured",
        occurred_at=1.0,
    )
    wal.finalize(TraceStatus.COMPLETE, expected_steps=3, recording_problems=[problem])
    incomplete = RunTrace(wal.run_dir).manifest
    assert incomplete.status is TraceStatus.INCOMPLETE
    assert incomplete.missing_steps == []
    assert incomplete.complete_through == 0
    assert [entry.step for entry in incomplete.steps] == [0, 1, 2]

    wal.commit_step(3, step_record(3))
    assert wal.manifest is not None and wal.manifest.complete_through == 0
    recovered = problem.model_copy(update={"recovered": True})
    wal.finalize(TraceStatus.COMPLETE, expected_steps=4, recording_problems=[recovered])
    complete = RunTrace(wal.run_dir).manifest
    assert complete.status is TraceStatus.COMPLETE
    assert complete.complete_through == 3
    assert complete.missing_steps == []
    assert complete.recording_problems == [recovered]


def test_missing_trailing_channel_does_not_shrink_committed_step_prefix(tmp_path):
    wal = new_wal(tmp_path)
    wal.commit_step(0, step_record(0))
    problem = RecordingProblem(
        channel="archive",
        phase="closeout",
        error="OSError",
        detail="tail could not be captured",
        occurred_at=1.0,
    )
    wal.finalize(TraceStatus.INCOMPLETE, recording_problems=[problem])
    manifest = RunTrace(wal.run_dir).manifest
    assert manifest.status is TraceStatus.INCOMPLETE
    assert manifest.complete_through == 0
    assert manifest.missing_steps == []


def test_uncertain_step_commit_refreshes_index_and_dedup_before_the_next_write(
    tmp_path, monkeypatch
):
    wal = new_wal(tmp_path)
    shared = blob(b"shared response body")
    call = completed_call(0, 0, "first", output_format=Reply, images=[data_url(PNG_BYTES)])
    fail_manifest_directory_sync(monkeypatch)
    with pytest.raises(OSError, match="manifest directory sync failed"):
        wal.commit_step(0, step_record(0), llm_calls=[call], extra_files=[shared])

    assert wal.manifest == RunTrace(wal.run_dir).manifest
    assert wal.manifest is not None
    assert [entry.step for entry in wal.manifest.steps] == [0]
    with pytest.raises(ValueError, match="already committed"):
        wal.commit_step(0, step_record(0))

    writes: list[str] = []
    original_write = fsio.write_durable

    def write(path: Path, data: bytes) -> set[Path]:
        writes.append(path.relative_to(wal.run_dir).as_posix())
        return original_write(path, data)

    monkeypatch.setattr(fsio, "write_durable", write)
    wal.commit_step(
        1,
        step_record(1),
        llm_calls=[completed_call(1, 0, "next", output_format=Reply, images=[data_url(PNG_BYTES)])],
        extra_files=[shared],
    )
    wal.finalize(TraceStatus.COMPLETE)
    trace = RunTrace(wal.run_dir)
    assert [entry.step for entry in trace.manifest.steps] == [0, 1]
    assert shared.rel_path not in writes
    assert not any(path.startswith("messages/images/") for path in writes)
    assert not any(path.startswith("messages/output_formats/") for path in writes)
    assert as_chat(trace.llm_messages(0)[0]).messages == prompt("first", [data_url(PNG_BYTES)])
    assert trace.read_extra(shared.rel_path) == b"shared response body"
    with pytest.raises(ValueError, match="immutable"):
        wal.commit_step(2, step_record(2), extra_files=[extra(shared.rel_path, b"overwrite")])


def test_uncertain_initial_manifest_is_reconciled_before_any_step(tmp_path, monkeypatch):
    wal = WALWriter(tmp_path, "run-test")
    fail_manifest_directory_sync(monkeypatch)
    with pytest.raises(OSError, match="manifest directory sync failed"):
        wal.init_run(TraceConfig(task="task"))
    assert wal.manifest == RunTrace(wal.run_dir).manifest
    wal.commit_step(0, step_record(0))
    assert RunTrace(wal.run_dir).manifest.complete_through == 0


def test_unreadable_uncertain_manifest_blocks_writes_until_it_can_be_checked(tmp_path, monkeypatch):
    wal = new_wal(tmp_path)
    fail_manifest_directory_sync(monkeypatch)
    original_read = Path.read_bytes
    block_reads = True

    def read(path: Path) -> bytes:
        if path.name == "manifest.json" and block_reads:
            raise PermissionError("manifest temporarily unreadable")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(OSError, match="manifest directory sync failed"):
        wal.commit_step(0, step_record(0))
    with pytest.raises(PermissionError, match="temporarily unreadable"):
        wal.commit_step(1, step_record(1))
    assert not (wal.run_dir / "steps/001.json").exists()

    block_reads = False
    manifest = wal.reconcile()
    assert manifest is not None
    assert [entry.step for entry in manifest.steps] == [0]
    wal.commit_step(1, step_record(1))
    assert [entry.step for entry in RunTrace(wal.run_dir).manifest.steps] == [0, 1]


def test_unrecognized_manifest_is_never_overwritten_after_uncertain_commit(tmp_path, monkeypatch):
    wal = new_wal(tmp_path)
    original_atomic_write = fsio.atomic_write

    def replace_with_another_manifest(path: Path, data: bytes) -> None:
        manifest = TraceManifest.model_validate_json(data)
        manifest.run_id = "another-writer"
        original_atomic_write(path, manifest.model_dump_json().encode())
        raise OSError("manifest replacement interrupted")

    monkeypatch.setattr(fsio, "atomic_write", replace_with_another_manifest)
    with pytest.raises(OSError, match="replacement interrupted"):
        wal.commit_step(0, step_record(0))
    before = (wal.run_dir / "manifest.json").read_bytes()
    with pytest.raises(RuntimeError, match="unverified commit"):
        wal.commit_step(1, step_record(1))
    assert (wal.run_dir / "manifest.json").read_bytes() == before


def test_uncertain_trailing_commit_does_not_reuse_its_position(tmp_path, monkeypatch):
    wal = new_wal(tmp_path)
    fail_manifest_directory_sync(monkeypatch)
    with pytest.raises(OSError, match="manifest directory sync failed"):
        wal.commit_trailing(segment_id="first", extra_files=[extra("env/first.txt", b"first")])
    wal.commit_trailing(segment_id="second", extra_files=[extra("env/second.txt", b"second")])
    trace = RunTrace(wal.run_dir)
    assert [entry.extra_index for entry in trace.manifest.trailing] == [
        "steps/trailing_00.extra.json",
        "steps/trailing_01.extra.json",
    ]
    assert [trace.read_extra(item.path) for item in trace.trailing_extra_files] == [
        b"first",
        b"second",
    ]
    assert [entry.segment_id for entry in trace.manifest.trailing] == ["first", "second"]
    with pytest.raises(ValueError, match="already committed"):
        wal.commit_trailing(segment_id="first", extra_files=[extra("env/third.txt", b"third")])
    assert not (wal.run_dir / "env/third.txt").exists()


def test_uncertain_close_does_not_erase_previously_committed_steps(tmp_path, monkeypatch):
    wal = new_wal(tmp_path)
    wal.commit_step(0, step_record(0))
    fail_manifest_directory_sync(monkeypatch)
    with pytest.raises(OSError, match="manifest directory sync failed"):
        wal.finalize(TraceStatus.COMPLETE)
    assert wal.manifest == RunTrace(wal.run_dir).manifest
    wal.finalize(TraceStatus.COMPLETE)
    assert [entry.step for entry in RunTrace(wal.run_dir).manifest.steps] == [0]


def test_metrics_only_commit_indexes_facts_without_prompt_files(tmp_path):
    wal = new_wal(tmp_path)
    metric = TraceFile("metrics/000_call_00.json", b'{"call": 0}')
    wal.commit_step(0, step_record(0), call_metrics=iter([metric]))
    entry = RunTrace(wal.run_dir).manifest.steps[0]
    assert entry.call_metrics == [metric.rel_path]
    assert entry.llm_calls == []
    assert not list((wal.run_dir / "messages").iterdir())


def test_implicit_and_explicit_metrics_share_one_committed_file(tmp_path):
    wal = new_wal(tmp_path)
    call = completed_call(0, 0, "first")
    assert call.metrics is not None
    wal.commit_step(0, step_record(0), llm_calls=[call], call_metrics=[call.metrics])
    assert RunTrace(wal.run_dir).manifest.steps[0].call_metrics == [call.metrics.rel_path]


def test_metrics_cannot_replace_a_file_or_escape_their_channel(tmp_path):
    wal = new_wal(tmp_path)
    with pytest.raises(ValueError, match="per-step file under metrics"):
        wal.commit_step(0, step_record(0), call_metrics=[TraceFile("env/call.json", b"{}")])
    assert RunTrace(wal.run_dir).manifest.steps == []
