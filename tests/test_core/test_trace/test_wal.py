"""Tests for WALWriter — M10 WAL persistence engine."""

import json

import pytest
from pydantic import BaseModel

from spectragent.core.trace.models import RunStatus, TraceConfig, TraceManifest
from spectragent.core.trace.wal import WALWriter, _atomic_write, _serialize_fragment


# ── _atomic_write ────────────────────────────────────────────


class TestAtomicWrite:
    def test_creates_file(self, tmp_path):
        target = tmp_path / "test.json"
        _atomic_write(target, b'{"ok": true}')
        assert target.exists()
        assert target.read_bytes() == b'{"ok": true}'

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "test.json"
        target.write_bytes(b"old")
        _atomic_write(target, b"new")
        assert target.read_bytes() == b"new"

    def test_no_leftover_tmp_on_success(self, tmp_path):
        target = tmp_path / "test.json"
        _atomic_write(target, b"data")
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_rename_failure_surfaces_the_real_error(self, tmp_path, monkeypatch):
        """The caller must see the disk error, not EBADF from the cleanup.

        Closing the fd manually and then probing it with
        ``os.get_inheritable`` in the error handler raised
        ``OSError(EBADF)`` first, masking the actual failure.
        """
        import os

        target = tmp_path / "test.json"

        def boom(*a, **kw):
            raise OSError("DISK FULL")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="DISK FULL"):
            _atomic_write(target, b"data")

    def test_no_leftover_tmp_on_failure(self, tmp_path, monkeypatch):
        import os

        target = tmp_path / "test.json"

        def boom(*a, **kw):
            raise OSError("DISK FULL")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            _atomic_write(target, b"data")
        assert list(tmp_path.glob("*.tmp")) == []
        assert not target.exists()


# ── _serialize_fragment ──────────────────────────────────────


class TestSerializeFragment:
    def test_pydantic_model(self):
        class M(BaseModel):
            x: int = 1

        result = _serialize_fragment(M())
        parsed = json.loads(result)
        assert parsed["x"] == 1

    def test_bytes_passthrough(self):
        raw = b"\x89PNG\r\n"
        assert _serialize_fragment(raw) is raw

    def test_dict(self):
        data = {"key": "value"}
        result = _serialize_fragment(data)
        assert json.loads(result) == data

    def test_string(self):
        assert _serialize_fragment("hello") == b"hello"


# ── WALWriter ────────────────────────────────────────────────


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
        config_path = wal.run_dir / "config.json"
        assert config_path.exists()
        parsed = json.loads(config_path.read_text())
        assert parsed["agent_id"] == "test_agent"
        assert parsed["loop_id"] == "react"

    def test_init_run_writes_manifest(self, wal, config):
        wal.init_run(config)
        manifest_path = wal.run_dir / "manifest.json"
        assert manifest_path.exists()
        manifest = TraceManifest.model_validate_json(manifest_path.read_text())
        assert manifest.run_id == "run-001"
        assert manifest.last_step_completed == -1
        assert manifest.status == RunStatus.RUNNING

    def test_write_observation_json(self, wal, config):
        wal.init_run(config)
        rel = wal.write_observation(0, "dom", {"element_tree": "<div/>"})
        assert rel == "observations/000_dom.json"
        abs_path = wal.run_dir / rel
        assert abs_path.exists()
        assert json.loads(abs_path.read_text())["element_tree"] == "<div/>"

    def test_write_observation_binary(self, wal, config):
        wal.init_run(config)
        png_data = b"\x89PNG\r\nfake"
        rel = wal.write_observation(0, "screenshot", png_data)
        assert rel == "observations/000_screenshot.png"
        assert (wal.run_dir / rel).read_bytes() == png_data

    def test_write_messages(self, wal, config):
        wal.init_run(config)
        msgs_in = [{"role": "user", "content": "hello"}]
        msgs_out = {"completion": "hi", "usage": {"tokens": 10}}
        in_rel, out_rel = wal.write_messages(0, msgs_in, msgs_out)
        assert in_rel == "messages/000_input.json"
        assert out_rel == "messages/000_output.json"
        assert json.loads((wal.run_dir / in_rel).read_text())[0]["role"] == "user"
        assert json.loads((wal.run_dir / out_rel).read_text())["completion"] == "hi"

    def test_write_step_record(self, wal, config):
        wal.init_run(config)
        record = {"step": 0, "llm_output": "click(button)", "actions": []}
        rel = wal.write_step_record(0, record)
        assert rel == "steps/000.json"
        assert json.loads((wal.run_dir / rel).read_text())["step"] == 0

    def test_commit_step_updates_manifest(self, wal, config):
        wal.init_run(config)
        wal.write_step_record(0, {"step": 0})
        wal.commit_step(
            step=0,
            step_file="steps/000.json",
            observation_files=["observations/000_dom.json"],
            message_input_file="messages/000_input.json",
            message_output_file="messages/000_output.json",
        )

        # Re-read manifest from disk
        manifest = TraceManifest.model_validate_json(
            (wal.run_dir / "manifest.json").read_text()
        )
        assert manifest.last_step_completed == 0
        assert len(manifest.steps) == 1
        assert manifest.steps[0].step == 0
        assert manifest.steps[0].step_file == "steps/000.json"

    def test_multi_step_commit(self, wal, config):
        wal.init_run(config)
        for i in range(3):
            wal.write_step_record(i, {"step": i})
            wal.commit_step(step=i, step_file=f"steps/{i:03d}.json")

        manifest = TraceManifest.model_validate_json(
            (wal.run_dir / "manifest.json").read_text()
        )
        assert manifest.last_step_completed == 2
        assert len(manifest.steps) == 3

    def test_write_result(self, wal, config):
        wal.init_run(config)
        result = {"task_id": "task-001", "answer": "42"}
        rel = wal.write_result(result)
        assert rel == "result.json"
        assert json.loads((wal.run_dir / rel).read_text())["answer"] == "42"

    def test_finalize_completed(self, wal, config):
        wal.init_run(config)
        wal.finalize(RunStatus.COMPLETED)
        manifest = TraceManifest.model_validate_json(
            (wal.run_dir / "manifest.json").read_text()
        )
        assert manifest.status == RunStatus.COMPLETED

    def test_finalize_error(self, wal, config):
        wal.init_run(config)
        wal.finalize(RunStatus.ERROR)
        manifest = TraceManifest.model_validate_json(
            (wal.run_dir / "manifest.json").read_text()
        )
        assert manifest.status == RunStatus.ERROR

    def test_commit_without_init_raises(self, wal):
        with pytest.raises(RuntimeError, match="not initialized"):
            wal.commit_step(step=0, step_file="steps/000.json")

    def test_finalize_without_init_raises(self, wal):
        with pytest.raises(RuntimeError, match="not initialized"):
            wal.finalize(RunStatus.COMPLETED)
