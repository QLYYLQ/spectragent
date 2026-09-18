"""Tests for TraceWriter hook — M11.

The commit point is ``loop.record_step``, so these tests drive the writer
the way the runtime does: buffer an LLM call, then surrender a real
``StepRecord``.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.hooks._registrar import HookRegistrar
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.trace.models import RunStatus, TraceManifest
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult
from spectragent.schemas.observation import build_observation_model
from spectragent.schemas.observation.fragments import TaskObs
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


def _result(
    agent_done: AgentDoneStatus = AgentDoneStatus.DONE, **kw
) -> AgentResult:
    """A terminal result of the shape ``AgentRuntime`` actually dispatches."""
    defaults = {
        "task_id": "t1",
        "task": "test",
        "timestamp": datetime.now(UTC),
        "env_status": EnvironmentStatus.SUCCESS,
        "agent_done": agent_done,
        "agent_reported_success": agent_done is AgentDoneStatus.DONE or None,
        "answer": "42",
        "metrics": AgentMetrics(end_to_end_ms=1000, steps=0),
    }
    defaults.update(kw)
    return AgentResult(**defaults)


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
        tw = TraceWriter(trace_dir=str(tmp_path / "traces"))
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
        """A trace nothing can join back to its dataset is an orphan.

        Identity arrives as *values*. ``AgentRuntime`` resolves it off
        the composed instances (``_component_id``) and passes it in; the
        writer no longer re-derives it by ``getattr``, which is why the
        objects below are handed over *and* the ids stated.
        """
        await writer.on_run_start(
            agent=fake_agent,
            env=fake_env,
            loop=fake_loop,
            llm=fake_llm,
            agent_id="test_agent",
            loop_id="react",
            env_type="browser",
            llm_model="gpt-4o",
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
        png = run_dir / "observations" / "000_browser.screenshot.png"
        assert png.exists()
        assert png.read_bytes() == _PNG

        sidecar = json.loads(
            (run_dir / "observations" / "000_browser.screenshot.json").read_text()
        )
        assert sidecar["viewport_width"] == 1280
        assert "image_bytes" not in sidecar
        assert sidecar["artifact_file"] == "000_browser.screenshot.png"

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
        # The fully-qualified key, so the file says who produced it.
        assert "loop.task" in written
        assert writer.run_dir is not None
        task_obs = RunTrace(writer.run_dir).get_observation(0, "loop.task")
        assert task_obs is not None
        assert task_obs["user_request"] == "find the price"
        assert task_obs["fragment_id"] == "loop.task"

    async def test_a_failed_fragment_reaches_the_trace_as_a_failure(
        self, writer, fake_agent, fake_env
    ):
        """An optional fragment nobody could capture is recorded, not faked.

        Same rule as a failed LLM call (``_failed_call``): the file says
        ``{error, detail}`` under the fragment's own key, so an evaluator
        reading the trace can tell "the device was offline" from "the
        screen was blank" — and cannot mistake either for real data.
        """
        from spectragent.schemas.observation.failure import FragmentFailure

        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        Model = build_observation_model(
            frozenset({"browser.screenshot", "loop.task"}),
            failable=frozenset({"browser.screenshot"}),
        )
        obs = Model(
            screenshot=FragmentFailure(
                fragment_id="browser.screenshot",
                capability="browser",
                error="TimeoutError",
                detail="CDP call timed out",
            ),
            task=TaskObs(user_request="find the price"),
        )
        await writer.after_ainvoke(_Completion(), args=([],), kwargs={})
        await writer.after_record_step(
            None, args=(_step(0),), kwargs={"observation": obs}
        )

        run_dir = writer._wal.run_dir
        # No .png: nothing was captured, so no artifact is invented.
        assert not (run_dir / "observations" / "000_browser.screenshot.png").exists()
        record = json.loads(
            (run_dir / "observations" / "000_browser.screenshot.json").read_text()
        )
        assert record["error"] == "TimeoutError"
        assert record["detail"] == "CDP call timed out"
        assert record["capability"] == "browser"
        assert "image_bytes" not in record

    async def test_capture_time_reaches_the_trace(
        self, writer, fake_agent, fake_env
    ):
        """Without it, per-capability skew is unrecoverable after the run."""
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        Model = build_observation_model(frozenset({"loop.task"}))
        fragment = TaskObs(user_request="find the price")
        obs = Model(task=fragment)
        await writer.after_ainvoke(_Completion(), args=([],), kwargs={})
        await writer.after_record_step(
            None, args=(_step(0),), kwargs={"observation": obs}
        )

        assert writer.run_dir is not None
        written = RunTrace(writer.run_dir).get_observation(0, "loop.task")
        assert written["captured_at"] == fragment.captured_at

    async def test_on_run_end_writes_result(self, writer, fake_agent, fake_env):
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )

        await writer.on_run_end(result=_result(), env=fake_env)

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

    async def test_the_hook_writes_what_the_reader_reads_back(
        self, writer, fake_agent, fake_env
    ):
        """Round-trip through the production writer, not a hand-built file.

        ``RunTrace`` promises typed models. That promise is only worth
        anything if the hook that produces the files on a real run is
        what these assertions read.
        """
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        await writer.after_ainvoke(_Completion(), args=([{"m": 0}],), kwargs={})
        await writer.after_record_step(None, args=(_step(0),), kwargs={})
        await writer.on_run_end(result=_result(), env=fake_env)

        trace = RunTrace(writer.run_dir)

        record = trace.step(0)
        assert isinstance(record, StepRecord)
        assert record.step == 0
        assert record.actions[0].name == "browser.click"
        assert record.token_usage is not None
        assert record.token_usage.total_tokens == 15

        result = trace.result
        assert isinstance(result, AgentResult)
        assert result.answer == "42"
        assert result.agent_done is AgentDoneStatus.DONE
        assert result.metrics.end_to_end_ms == 1000

    async def test_a_run_with_no_result_writes_no_result_file(
        self, writer, fake_agent, fake_env
    ):
        """``result is None`` must mean "absent", never "unparseable".

        A ``null`` result.json would read back as a schema failure and
        make a run that simply never finished look like a corrupt trace.
        """
        await writer.on_run_start(
            agent=fake_agent, env=fake_env, task="test", config={}
        )
        await writer.on_run_end(result=None, env=fake_env)

        assert not (writer.run_dir / "result.json").exists()
        assert RunTrace(writer.run_dir).result is None

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
        tw = TraceWriter(trace_dir=str(tmp_path / "traces"))
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
        await writer.on_run_end(result=_result(AgentDoneStatus.TIMEOUT))

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
        await writer.on_run_end(result=_result(AgentDoneStatus.TIMEOUT))

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
        await writer.on_run_end(result=_result(AgentDoneStatus.TIMEOUT))

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
        await writer.on_run_end(result=_result(AgentDoneStatus.ERROR))

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
        await writer.on_run_end(result=_result())

        trace = RunTrace(run_dir)
        assert trace.unfinished_message_files == []
        assert trace.manifest.status == RunStatus.COMPLETED
        assert trace.step_count == 1


class TestACallThatRaisedIsStillRecorded:
    """``after`` advice does not fire when the target raises.

    That is usually the right answer: a call that never came back has
    nothing to record. But one LLM error is *recoverable* — a structured
    reply that does not fit the schema — and the Loop continues past it,
    feeding the model its own text back. Recorded nowhere, the trace would
    carry a step whose messages file is simply absent, and it would be
    absent for exactly the steps a researcher most wants to read.

    ``around_ainvoke`` sees what ``after`` cannot.
    """

    @pytest.fixture()
    def writer(self, tmp_path) -> TraceWriter:
        return TraceWriter(trace_dir=str(tmp_path / "traces"))

    async def _start(self, writer) -> Path:
        await writer.on_run_start(agent=None, env=None, task="test", config={})
        assert writer.run_dir is not None
        return writer.run_dir

    @staticmethod
    def _raiser(exc: Exception):
        async def proceed():
            raise exc

        return proceed

    async def test_the_exchange_reaches_the_step_it_belongs_to(self, writer):
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        run_dir = await self._start(writer)
        messages = [{"role": "user", "content": "what do you see?"}]
        exc = StructuredOutputValidationError(
            "schema mismatch",
            raw_output='{"action": [{}]}',
            model="gpt-4o",
        )
        with pytest.raises(StructuredOutputValidationError):
            await writer.around_ainvoke(
                proceed=self._raiser(exc), args=(messages,), kwargs={}
            )
        # The Loop turns this into a parse error and surrenders a step.
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        sent, got = RunTrace(run_dir).get_messages(0)
        assert sent == messages
        assert got is not None
        assert got["error"] == "StructuredOutputValidationError"
        assert got["raw_output"] == '{"action": [{}]}'

    async def test_a_transport_failure_is_recorded_without_inventing_a_reply(
        self, writer
    ):
        """No ``raw_output``: the provider had no text to give."""
        run_dir = await self._start(writer)
        with pytest.raises(TimeoutError):
            await writer.around_ainvoke(
                proceed=self._raiser(TimeoutError("gateway gone")),
                args=([{"m": 1}],),
                kwargs={},
            )
        await writer.on_run_end(result=_result(AgentDoneStatus.ERROR))

        got = RunTrace(run_dir).get_messages(0)[1]
        assert got is not None
        assert got["error"] == "TimeoutError"
        assert "raw_output" not in got

    async def test_a_successful_call_is_buffered_once_not_twice(self, writer):
        """``around`` wraps the ``after`` advice; only one of them records."""
        run_dir = await self._start(writer)

        async def proceed():
            await writer.after_ainvoke(_Completion(), args=([{"m": 1}],), kwargs={})
            return _Completion()

        await writer.around_ainvoke(proceed=proceed, args=([{"m": 1}],), kwargs={})
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        assert (run_dir / "messages" / "000_input.json").exists()
        assert not (run_dir / "messages" / "000_input__1.json").exists()

    async def test_the_exception_still_reaches_the_caller(self, writer):
        """The hook observes; it does not swallow."""
        await self._start(writer)
        with pytest.raises(RuntimeError, match="boom"):
            await writer.around_ainvoke(
                proceed=self._raiser(RuntimeError("boom")), args=([],), kwargs={}
            )
