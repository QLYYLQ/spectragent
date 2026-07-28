"""Tests for TraceWriter hook — M11.

The commit point is ``loop.record_step``, so these tests drive the writer
the way the runtime does: buffer an LLM call, then surrender a real
``StepRecord``.
"""

import json
from pathlib import Path

import pytest

from spectragent.core.hooks._registrar import HookRegistrar
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.trace.models import RunStatus, TraceManifest
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult
from spectragent.schemas.observation import build_observation_model
from spectragent.schemas.observation.fragments import ScreenshotObs, TaskObs
from spectragent.schemas.step_record import StepRecord, TokenUsage

_PNG = b"\x89PNG\r\n\x1a\n-fake-bytes"


class _Completion:
    completion = "click(button)"
    usage = None


def _step(idx: int = 0, **kw) -> StepRecord:
    defaults = {
        "step": idx,
        "llm_output": "click(button)",
        "actions": [Action(name="browser.click", params={"index": 1})],
        "action_results": [ActionResult(success=True, extracted_content="ok")],
        "timestamp": 1234.5,
        "token_usage": TokenUsage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        ),
    }
    defaults.update(kw)
    return StepRecord(**defaults)


class TestTraceWriterRegistration:
    def test_registered_in_conscribe(self):
        cls = HookRegistrar.get("trace_writer")
        assert cls is TraceWriter

    def test_registry_key(self):
        assert TraceWriter.__registry_key__ == "trace_writer"

    def test_blocking(self):
        tw = TraceWriter()
        assert tw.blocking is True

    def test_intercepts_record_step_and_ainvoke(self):
        by_method = {i.method: i.role for i in TraceWriter.__intercepts__}
        assert by_method["ainvoke"] == "llm"
        # The step is committed when the Loop surrenders it, not when the
        # LLM answers — the actions do not exist yet at ainvoke time.
        assert by_method["record_step"] == "loop"
        assert "observe" not in by_method


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

    @pytest.fixture()
    def fake_loop(self):
        class _L:
            loop_id = "react"

        return _L()

    @pytest.fixture()
    def fake_llm(self):
        class _M:
            model = "gpt-4o"

        return _M()

    async def test_on_run_start_creates_directory(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test task", config={}
        )
        assert writer._wal is not None
        assert writer._wal.run_dir.exists()
        assert (writer._wal.run_dir / "manifest.json").exists()
        assert (writer._wal.run_dir / "config.json").exists()

    async def test_config_records_full_identity(
        self, writer, fake_agent, fake_env, fake_loop, fake_llm
    ):
        """A trace nothing can join back to its dataset is an orphan."""
        await writer.on_run_start(
            agent=fake_agent,
            env=fake_env,
            loop=fake_loop,
            llm=fake_llm,
            task="find the price",
            task_id="task-42",
            benchmark="online_mind2web",
            subset="Hard",
            run_id="fixed-run-id",
            config={"k": "v"},
        )
        cfg = RunTrace(writer.run_dir).config
        assert cfg.agent_id == "test_agent"
        assert cfg.loop_id == "react"
        assert cfg.env_type == "browser"
        assert cfg.llm_model == "gpt-4o"
        assert cfg.benchmark == "online_mind2web"
        assert cfg.subset == "Hard"
        assert cfg.task_id == "task-42"
        assert cfg.task == "find the price"
        assert writer.run_dir.name == "fixed-run-id"

    async def test_record_step_commits_full_record(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        messages = [{"role": "user", "content": "what do you see?"}]
        await writer.after_ainvoke(_Completion(), args=(messages,), kwargs={})
        await writer.after_record_step(
            None, args=(_step(0),), kwargs={"observation": None}
        )

        run_dir = writer._wal.run_dir
        assert (run_dir / "steps" / "000.json").exists()
        assert (run_dir / "messages" / "000_input.json").exists()
        assert (run_dir / "messages" / "000_output.json").exists()

        rec = json.loads((run_dir / "steps" / "000.json").read_text())
        assert set(rec) >= {
            "step",
            "llm_output",
            "actions",
            "action_results",
            "timestamp",
            "token_usage",
        }
        assert rec["actions"][0]["name"] == "browser.click"
        assert rec["action_results"][0]["success"] is True
        assert rec["token_usage"]["total_tokens"] == 15

        manifest = TraceManifest.model_validate_json(
            (run_dir / "manifest.json").read_text()
        )
        assert manifest.last_step_completed == 0

    async def test_multi_step_commit(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        for i in range(3):
            await writer.after_ainvoke(
                _Completion(), args=([{"role": "user", "content": f"step {i}"}],), kwargs={}
            )
            await writer.after_record_step(None, args=(_step(i),), kwargs={})

        manifest = TraceManifest.model_validate_json(
            (writer._wal.run_dir / "manifest.json").read_text()
        )
        assert manifest.last_step_completed == 2
        assert writer._steps_written == 3

    async def test_multiple_llm_calls_in_one_step_all_persisted(
        self, writer, fake_agent, fake_env
    ):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        await writer.after_ainvoke(_Completion(), args=([{"m": 1}],), kwargs={})
        await writer.after_ainvoke(_Completion(), args=([{"m": 2}],), kwargs={})
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        run_dir = writer._wal.run_dir
        assert (run_dir / "messages" / "000_input.json").exists()
        assert (run_dir / "messages" / "000_input__1.json").exists()

    async def test_screenshot_written_as_real_png(self, writer, fake_agent, fake_env):
        """Binary artifacts land as files, not base64 buried in JSON."""
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        Model = build_observation_model(frozenset({"browser.screenshot", "loop.task"}))
        obs = Model(
            screenshot=ScreenshotObs(
                image_bytes=_PNG, viewport_width=1280, viewport_height=720
            ),
            task=TaskObs(user_request="find the price"),
        )
        await writer.after_ainvoke(_Completion(), args=([],), kwargs={})
        await writer.after_record_step(None, args=(_step(0),), kwargs={"observation": obs})

        run_dir = writer._wal.run_dir
        png = run_dir / "observations" / "000_screenshot.png"
        assert png.exists()
        assert png.read_bytes() == _PNG

        sidecar = json.loads(
            (run_dir / "observations" / "000_screenshot.json").read_text()
        )
        assert sidecar["viewport_width"] == 1280
        assert "image_bytes" not in sidecar
        assert sidecar["artifact_file"] == "000_screenshot.png"

        trace = RunTrace(run_dir)
        assert trace.screenshots == [png]

    async def test_loop_fragments_reach_disk(self, writer, fake_agent, fake_env):
        """The observation recorded is the one the *agent* saw."""
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        Model = build_observation_model(frozenset({"loop.task"}))
        obs = Model(task=TaskObs(user_request="find the price"))
        await writer.after_ainvoke(_Completion(), args=([],), kwargs={})
        await writer.after_record_step(None, args=(_step(0),), kwargs={"observation": obs})

        written = {
            p.name.split("_", 1)[1].rsplit(".", 1)[0]
            for p in (writer._wal.run_dir / "observations").iterdir()
        }
        assert "task" in written
        assert writer.run_dir is not None
        task_obs = RunTrace(writer.run_dir).get_observation(0, "task")
        assert task_obs is not None
        assert task_obs["user_request"] == "find the price"

    async def test_on_run_end_writes_result(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )

        result = {"task_id": "t1", "answer": "42"}
        await writer.on_run_end(result=result, env=fake_env)

        run_dir = writer._wal.run_dir
        assert (run_dir / "result.json").exists()
        parsed = json.loads((run_dir / "result.json").read_text())
        assert parsed["answer"] == "42"

        manifest = TraceManifest.model_validate_json(
            (run_dir / "manifest.json").read_text()
        )
        assert manifest.status == RunStatus.COMPLETED

    async def test_timeout_result_finalizes_as_error(self, writer, fake_agent, fake_env):
        """status must not stay 'running' just because the agent timed out."""
        from datetime import UTC, datetime

        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        result = AgentResult(
            task_id="t1",
            task="test",
            timestamp=datetime.now(UTC),
            env_status=EnvironmentStatus.SUCCESS,
            agent_done=AgentDoneStatus.TIMEOUT,
            error="Execution timed out after 1s",
            metrics=AgentMetrics(end_to_end_ms=1000, steps=0),
        )
        await writer.on_run_end(result=result, env=fake_env)

        trace = RunTrace(writer.run_dir)
        assert trace.manifest.status == RunStatus.ERROR
        assert trace.result is not None

    async def test_on_run_error_finalizes(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        await writer.on_run_error(
            error=RuntimeError("boom"), partial_steps=[], env=fake_env
        )

        manifest = TraceManifest.model_validate_json(
            (writer._wal.run_dir / "manifest.json").read_text()
        )
        assert manifest.status == RunStatus.ERROR


class TestUnfinishedMessagesSurviveRunEnd:
    """The buffer is drained on commit *and* on death.

    ``after_record_step`` is the commit point, but a run that times out
    or crashes between the completion and the step never reaches it.
    """

    @pytest.fixture()
    def writer(self, tmp_path) -> TraceWriter:
        tw = TraceWriter()
        tw.trace_dir = str(tmp_path / "traces")
        return tw

    async def _start(self, writer) -> Path:
        await writer.on_run_start(agent=None, env=None, task="test", config={})
        assert writer.run_dir is not None
        return writer.run_dir

    async def test_run_end_flushes_buffered_call(self, writer):
        run_dir = await self._start(writer)
        await writer.after_ainvoke(
            _Completion(), args=([{"role": "user", "content": "step 0 prompt"}],), kwargs={}
        )
        await writer.on_run_end(result={"agent_done": "timeout"})

        trace = RunTrace(run_dir)
        prompt, completion = trace.get_messages(0)
        assert prompt == [{"role": "user", "content": "step 0 prompt"}]
        assert completion is not None
        assert [p.name for p in trace.unfinished_message_files] == [
            "000_input.json",
            "000_output.json",
        ]

    async def test_flush_does_not_fabricate_a_completed_step(self, writer):
        run_dir = await self._start(writer)
        await writer.after_ainvoke(_Completion(), args=([{"m": 1}],), kwargs={})
        await writer.on_run_end(result={"agent_done": "timeout"})

        trace = RunTrace(run_dir)
        assert trace.step_count == 0
        assert trace.manifest.steps == []
        assert not (run_dir / "steps" / "000.json").exists()

    async def test_flush_index_follows_the_last_committed_step(self, writer):
        run_dir = await self._start(writer)
        await writer.after_ainvoke(_Completion(), args=([{"m": 0}],), kwargs={})
        await writer.after_record_step(None, args=(_step(0),), kwargs={})
        # Step 1's LLM call comes back; the run dies before record_step.
        await writer.after_ainvoke(_Completion(), args=([{"m": 1}],), kwargs={})
        await writer.on_run_end(result={"agent_done": "timeout"})

        trace = RunTrace(run_dir)
        assert trace.step_count == 1
        assert trace.get_messages(1)[0] == [{"m": 1}]
        assert [p.name for p in trace.unfinished_message_files] == [
            "001_input.json",
            "001_output.json",
        ]
        # The committed step's messages are untouched.
        assert trace.get_messages(0)[0] == [{"m": 0}]

    async def test_every_buffered_call_is_flushed(self, writer):
        run_dir = await self._start(writer)
        await writer.after_ainvoke(_Completion(), args=([{"m": 1}],), kwargs={})
        await writer.after_ainvoke(_Completion(), args=([{"m": 2}],), kwargs={})
        await writer.on_run_end(result={"agent_done": "error"})

        assert (run_dir / "messages" / "000_input.json").exists()
        assert (run_dir / "messages" / "000_input__1.json").exists()
        assert len(RunTrace(run_dir).unfinished_message_files) == 4

    async def test_on_run_error_alone_flushes(self, writer):
        """A driver that only dispatches on_run_error must not lose it."""
        run_dir = await self._start(writer)
        await writer.after_ainvoke(_Completion(), args=([{"m": 1}],), kwargs={})
        await writer.on_run_error(error=RuntimeError("boom"), partial_steps=[])

        assert RunTrace(run_dir).get_messages(0)[0] == [{"m": 1}]

    async def test_clean_run_records_nothing_unfinished(self, writer):
        run_dir = await self._start(writer)
        await writer.after_ainvoke(_Completion(), args=([{"m": 0}],), kwargs={})
        await writer.after_record_step(None, args=(_step(0),), kwargs={})
        await writer.on_run_end(result={"answer": "42"})

        trace = RunTrace(run_dir)
        assert trace.unfinished_message_files == []
        assert trace.manifest.status == RunStatus.COMPLETED
        assert trace.step_count == 1
