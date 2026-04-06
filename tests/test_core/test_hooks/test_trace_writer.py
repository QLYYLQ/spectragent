"""Tests for TraceWriter hook — M11."""

import json

import pytest

from spectragent.core.hooks._registrar import HookRegistrar
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.trace.models import RunStatus, TraceManifest


class TestTraceWriterRegistration:
    def test_registered_in_conscribe(self):
        cls = HookRegistrar.get("trace_writer")
        assert cls is TraceWriter

    def test_registry_key(self):
        assert TraceWriter.__registry_key__ == "trace_writer"

    def test_blocking(self):
        tw = TraceWriter()
        assert tw.blocking is True

    def test_intercepts_observe_and_ainvoke(self):
        methods = {i.method for i in TraceWriter.__intercepts__}
        assert "observe" in methods
        assert "ainvoke" in methods


class TestTraceWriterLifecycle:
    @pytest.fixture()
    def writer(self, tmp_path) -> TraceWriter:
        tw = TraceWriter()
        tw.trace_dir = str(tmp_path / "traces")
        return tw

    @pytest.fixture()
    def fake_agent(self):
        class _A:
            agent_id = "test_agent"
            agent_name = "test_agent"

        return _A()

    @pytest.fixture()
    def fake_env(self):
        class _E:
            env_type = "browser"

        return _E()

    @pytest.mark.asyncio
    async def test_on_run_start_creates_directory(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test task", config={}
        )
        assert writer._wal is not None
        assert writer._wal.run_dir.exists()
        assert (writer._wal.run_dir / "manifest.json").exists()
        assert (writer._wal.run_dir / "config.json").exists()

    @pytest.mark.asyncio
    async def test_after_observe_buffers_data(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        observation = {"dom": "<div/>", "url": "http://example.com"}
        await writer.after_observe(observation, args=(), kwargs={})
        assert writer._pending_observation is observation

    @pytest.mark.asyncio
    async def test_after_ainvoke_commits_step(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        # Buffer observation
        await writer.after_observe(
            {"url": "http://example.com"}, args=(), kwargs={}
        )

        # Simulate LLM response
        class _Completion:
            completion = "click(button)"
            usage = None

        messages = [{"role": "user", "content": "what do you see?"}]
        await writer.after_ainvoke(
            _Completion(), args=(messages,), kwargs={}
        )

        # Verify files written
        run_dir = writer._wal.run_dir
        assert (run_dir / "steps" / "000.json").exists()
        assert (run_dir / "messages" / "000_input.json").exists()
        assert (run_dir / "messages" / "000_output.json").exists()

        # Verify manifest updated
        manifest = TraceManifest.model_validate_json(
            (run_dir / "manifest.json").read_text()
        )
        assert manifest.last_step_completed == 0

    @pytest.mark.asyncio
    async def test_multi_step_commit(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )

        class _Completion:
            completion = "action"
            usage = None

        for i in range(3):
            await writer.after_observe({"step": i}, args=(), kwargs={})
            await writer.after_ainvoke(
                _Completion(), args=([{"role": "user", "content": f"step {i}"}],), kwargs={}
            )

        manifest = TraceManifest.model_validate_json(
            (writer._wal.run_dir / "manifest.json").read_text()
        )
        assert manifest.last_step_completed == 2
        assert writer._current_step == 3

    @pytest.mark.asyncio
    async def test_on_run_end_writes_result(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )

        result = {"task_id": "t1", "answer": "42"}
        await writer.on_run_end(result=result)

        run_dir = writer._wal.run_dir
        assert (run_dir / "result.json").exists()
        parsed = json.loads((run_dir / "result.json").read_text())
        assert parsed["answer"] == "42"

        manifest = TraceManifest.model_validate_json(
            (run_dir / "manifest.json").read_text()
        )
        assert manifest.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_on_run_error_finalizes(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        await writer.on_run_error(error=RuntimeError("boom"), partial_steps=[])

        manifest = TraceManifest.model_validate_json(
            (writer._wal.run_dir / "manifest.json").read_text()
        )
        assert manifest.status == RunStatus.ERROR
