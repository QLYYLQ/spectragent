"""Tests for WALWriter — the run directory's write side.

The commit protocol itself (``init_run``'s order before the first manifest,
ordering, batched directory ``fsync``, failure injection, dedup, path rules,
the 1000-file budget) is pinned in ``test_wal_batch_commit.py``; the
durable-write primitives in ``test_fsio.py``. This file covers the API: what
each call puts on disk.
"""

import json

import pytest

from spectragent.core.trace.models import (
    TRACE_FORMAT_VERSION,
    RunStatus,
    TraceConfig,
    TraceManifest,
)
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.action import Action
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import (
    agent_result,
    completed_call,
    extra,
    failure,
    screenshot,
    step_record,
)


def _manifest(wal: WALWriter) -> TraceManifest:
    return TraceManifest.model_validate_json((wal.run_dir / "manifest.json").read_text())


class TestWALWriter:
    @pytest.fixture()
    def wal(self, tmp_path) -> WALWriter:
        return WALWriter(tmp_path, "run-001")

    @pytest.fixture()
    def config(self) -> TraceConfig:
        return TraceConfig(
            agent_id="test_agent",
            loop_id="react",
            env_type="browser",
            task="test task",
            task_id="task-001",
        )

    def test_init_run_creates_directories(self, wal, config):
        wal.init_run(config)
        run_dir = wal.run_dir
        assert (run_dir / "steps").is_dir()
        assert (run_dir / "observations").is_dir()
        assert (run_dir / "messages").is_dir()
        assert (run_dir / "evals").is_dir()

    def test_init_run_writes_config(self, wal, config):
        wal.init_run(config)
        parsed = json.loads((wal.run_dir / "config.json").read_text())
        assert parsed["agent_id"] == "test_agent"
        assert parsed["loop_id"] == "react"

    def test_init_run_writes_a_format_2_manifest(self, wal, config):
        wal.init_run(config)
        manifest = _manifest(wal)
        assert manifest.format_version == TRACE_FORMAT_VERSION == 2
        assert manifest.run_id == "run-001"
        assert manifest.last_step_completed == -1
        assert manifest.status == RunStatus.RUNNING
        assert manifest.steps == []
        assert manifest.trailing == []
        assert manifest.output_formats == []

    def test_the_step_record_is_persisted_whole(self, wal, config):
        """Every field, not just the ones a hand-built dict remembered.

        The reader re-hydrates this file as a ``StepRecord``, so a writer
        that dropped defaults would make the two disagree.
        """
        wal.init_run(config)
        wal.commit_step(
            0, StepRecord(step=0, actions=[Action(name="browser.click", params={"index": 3})])
        )
        on_disk = json.loads((wal.run_dir / "steps/000.json").read_text())
        assert set(on_disk) == set(StepRecord.model_fields)
        assert on_disk["actions"][0]["params"] == {"index": 3}

    def test_commit_step_indexes_everything_the_step_consists_of(self, wal, config):
        wal.init_run(config)
        wal.commit_step(
            0,
            step_record(0),
            fragments=[screenshot(0), failure(0, "browser.browser_meta")],
            llm_calls=[completed_call(0, 0, "go"), completed_call(0, 1, "go again")],
            extra_files=[extra("env/steps/000/evidence.txt", b"seen")],
        )

        manifest = _manifest(wal)
        assert manifest.last_step_completed == 0
        (entry,) = manifest.steps
        assert entry.step_file == "steps/000.json"
        assert entry.fragments["browser.screenshot"].file == (
            "observations/000_browser.screenshot.json"
        )
        assert entry.fragments["browser.screenshot"].artifact == (
            "observations/000_browser.screenshot.png"
        )
        assert entry.fragments["browser.screenshot"].failed is False
        assert entry.fragments["browser.browser_meta"].failed is True
        assert entry.fragments["browser.browser_meta"].artifact is None
        assert entry.llm_calls == ["messages/000_call_00.json", "messages/000_call_01.json"]
        assert entry.extra_index == "steps/000.extra.json"
        for rel in (
            "observations/000_browser.screenshot.json",
            "observations/000_browser.screenshot.png",
            "observations/000_browser.browser_meta.json",
            "messages/000_call_00.json",
            "messages/000_call_01.json",
            "env/steps/000/evidence.txt",
            "steps/000.extra.json",
        ):
            assert (wal.run_dir / rel).is_file(), rel

    def test_the_fragment_json_is_the_fragments_own(self, wal, config):
        """No ``artifact_file`` pointer in it: the index alone names the binary."""
        wal.init_run(config)
        wal.commit_step(0, step_record(0), fragments=[screenshot(0)])

        sidecar = json.loads((wal.run_dir / "observations/000_browser.screenshot.json").read_text())
        assert "artifact_file" not in sidecar
        assert "image_bytes" not in sidecar
        assert sidecar["fragment_id"] == "browser.screenshot"

    def test_a_step_without_extra_files_has_no_index(self, wal, config):
        wal.init_run(config)
        wal.commit_step(0, step_record(0))
        assert _manifest(wal).steps[0].extra_index is None
        assert not (wal.run_dir / "steps/000.extra.json").exists()

    def test_multi_step_commit(self, wal, config):
        wal.init_run(config)
        for i in range(3):
            wal.commit_step(i, step_record(i))

        manifest = _manifest(wal)
        assert manifest.last_step_completed == 2
        assert [entry.step for entry in manifest.steps] == [0, 1, 2]

    def test_a_step_commits_once(self, wal, config):
        wal.init_run(config)
        wal.commit_step(0, step_record(0))
        with pytest.raises(ValueError, match="already committed"):
            wal.commit_step(0, step_record(0))

    def test_the_record_must_be_the_steps_own(self, wal, config):
        wal.init_run(config)
        with pytest.raises(ValueError, match="record of step 1"):
            wal.commit_step(0, step_record(1))
        assert _manifest(wal).steps == []

    def test_commit_trailing_claims_no_step(self, wal, config):
        wal.init_run(config)
        wal.commit_step(0, step_record(0))
        wal.commit_trailing(llm_calls=[completed_call(1, 0, "unfinished")])

        manifest = _manifest(wal)
        assert manifest.last_step_completed == 0
        assert [entry.step for entry in manifest.steps] == [0]
        assert manifest.trailing[0].llm_calls == ["messages/001_call_00.json"]
        assert not (wal.run_dir / "steps/001.json").exists()

    def test_write_result(self, wal, config):
        wal.init_run(config)
        wal.write_result(agent_result())
        on_disk = json.loads((wal.run_dir / "result.json").read_text())
        assert on_disk["answer"] == "42"
        assert on_disk["schema_version"] == "2.1"

    def test_finalize_completed(self, wal, config):
        wal.init_run(config)
        wal.finalize(RunStatus.COMPLETED)
        assert _manifest(wal).status == RunStatus.COMPLETED

    def test_finalize_error(self, wal, config):
        wal.init_run(config)
        wal.finalize(RunStatus.ERROR)
        assert _manifest(wal).status == RunStatus.ERROR

    def test_finalize_keeps_what_was_committed(self, wal, config):
        wal.init_run(config)
        wal.commit_step(0, step_record(0))
        wal.finalize(RunStatus.COMPLETED)
        manifest = _manifest(wal)
        assert [entry.step for entry in manifest.steps] == [0]
        assert manifest.format_version == 2

    def test_commit_without_init_raises(self, wal):
        with pytest.raises(RuntimeError, match="not initialized"):
            wal.commit_step(0, step_record(0))

    def test_commit_trailing_without_init_raises(self, wal):
        with pytest.raises(RuntimeError, match="not initialized"):
            wal.commit_trailing(llm_calls=[completed_call(0, 0, "x")])

    def test_finalize_without_init_raises(self, wal):
        with pytest.raises(RuntimeError, match="not initialized"):
            wal.finalize(RunStatus.COMPLETED)
