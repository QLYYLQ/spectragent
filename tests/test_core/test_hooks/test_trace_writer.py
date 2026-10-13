"""Tests for the TraceWriter hook.

Driven the way the runtime drives it: each LLM call goes through
``around_ainvoke`` (the only place a call is recorded), and the step commits
when the Loop surrenders a real ``StepRecord`` (``after_record_step``).
Everything is read back through ``RunTrace``, typed.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.hooks._registrar import HookRegistrar
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.exceptions import (
    ModelOutputTruncatedError,
    StructuredOutputValidationError,
)
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage
from spectragent.core.step_recorder import RecordingFailedError
from spectragent.core.trace.models import TraceManifest, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult
from spectragent.schemas.observation import build_observation_model
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.fragments import TaskObs
from spectragent.schemas.step_record import StepRecord, TokenUsage

_PNG = b"\x89PNG\r\n\x1a\n-fake-bytes"


def _usage(total: int = 15) -> ChatInvokeUsage:
    return ChatInvokeUsage(
        prompt_tokens=total - 5,
        prompt_cached_tokens=0,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=5,
        total_tokens=total,
    )


def _completion(text: str = "click(button)") -> ChatInvokeCompletion:
    return ChatInvokeCompletion(completion=text, usage=_usage(), stop_reason="end_turn")


def _prompt(text: str) -> list[UserMessage]:
    return [UserMessage(content=text)]


class Reply(BaseModel):
    action: str


def _step(idx: int = 0, **kw) -> StepRecord:
    defaults = {
        "step": idx,
        "llm_output": "click(button)",
        "actions": [Action(name="browser.click", params={"index": 1})],
        "action_results": [ActionResult(success=True, extracted_content="ok")],
        "timestamp": 1234.5,
        "token_usage": TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    }
    defaults.update(kw)
    return StepRecord(**defaults)


def _result(agent_done: AgentDoneStatus = AgentDoneStatus.DONE, **kw) -> AgentResult:
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


async def _call(
    writer: TraceWriter,
    text: str = "what do you see?",
    *,
    returns: Any = None,
    raises: BaseException | None = None,
    output_format: type[BaseModel] | None = None,
) -> Any:
    """One LLM call through the advice, the way the AOP proxy issues it."""

    async def proceed() -> Any:
        if raises is not None:
            raise raises
        return returns if returns is not None else _completion()

    kwargs = {"output_format": output_format} if output_format is not None else {}
    return await writer.around_ainvoke(proceed=proceed, args=(_prompt(text),), kwargs=kwargs)


async def _start(writer: TraceWriter, **kwargs: Any) -> Path:
    await writer.on_run_start(agent=None, env=None, task="test", config={}, **kwargs)
    assert writer.run_dir is not None
    return writer.run_dir


@pytest.fixture()
def writer(tmp_path) -> TraceWriter:
    return TraceWriter(trace_dir=str(tmp_path / "traces"))


class TestTraceWriterRegistration:
    def test_registered_in_conscribe(self):
        assert HookRegistrar.get("trace_writer") is TraceWriter

    def test_registry_key(self):
        assert TraceWriter.__registry_key__ == "trace_writer"

    def test_blocking(self):
        assert TraceWriter().blocking is True

    def test_around_is_the_only_llm_intercept(self):
        """No ``after_ainvoke``: it never sees a call that raised, nor a cancelled one."""
        handlers = {(i.role, i.handler_name) for i in TraceWriter.__intercepts__}
        assert handlers == {("llm", "around_ainvoke"), ("loop", "after_record_step")}
        assert not hasattr(TraceWriter, "after_ainvoke")


class TestTheConstructorSurface:
    """A hand-written ``__init__`` (not a ``@dataclass``) carrying the ``archive`` receptor.

    ``archive`` is a ``__wiring__`` receptor wired from the archiver registry,
    and conscribe reads ``inspect.signature(__init__)`` before ``@dataclass``
    would have synthesised one — so every knob, the receptor included, is a
    named ``Annotated`` parameter, and the receptor's class annotation lives
    on a plain mixin. The YAML semantics are pinned in
    ``tests/test_core/test_composer/test_archive_yaml_surface.py``.
    """

    def test_keywords_and_defaults(self):
        from tests._helpers.archive_fakes import FakeStepArchiver

        default = TraceWriter()
        assert (default.trace_dir, default.record_secrets, default.archive) == (
            "traces",
            False,
            [],
        )
        archiver = FakeStepArchiver()
        writer = TraceWriter(trace_dir="/tmp/t", record_secrets=True, archive=[archiver])
        assert (writer.trace_dir, writer.record_secrets) == ("/tmp/t", True)
        assert writer.archive == [archiver]
        assert writer.archive is not TraceWriter(archive=writer.archive).archive

    def test_not_a_dataclass(self):
        import dataclasses

        assert "__init__" in vars(TraceWriter)
        assert "__dataclass_fields__" not in vars(TraceWriter)
        assert not [f.name for f in dataclasses.fields(TraceWriter)]  # inherits HookBase's: none

    def test_the_config_carries_the_knobs_and_the_archive_receptor(self):
        from conscribe.config.extractor import extract_config_schema

        schema = extract_config_schema(TraceWriter)
        assert schema is not None
        assert set(schema.model_fields) == {"trace_dir", "record_secrets", "archive"}
        assert TraceWriter.__wiring__ == {"archive": "archiver"}

    def test_the_generated_config_types_the_archive(self):
        from pydantic import ValidationError

        from spectragent.generated._composed import TraceWriterHookConfig

        assert set(TraceWriterHookConfig.model_fields) == {
            "trace_dir",
            "record_secrets",
            "archive",
            "hook_id",
        }
        config = TraceWriterHookConfig.model_validate(
            {"archive": [{"archiver_id": "browser.network", "drain_timeout_s": 0.5}]}
        )
        assert config.archive is not None
        assert config.archive[0].archiver_id == "browser.network"
        with pytest.raises(ValidationError, match="archiver_id"):
            TraceWriterHookConfig.model_validate({"archive": [{"archiver_id": "browser.nework"}]})

    def test_two_writers_are_two_hooks(self):
        """Compared by identity: ``HookBase`` is ``@dataclass(eq=False)``.

        Otherwise a hand-written subclass inherits a field-less ``__eq__``
        under which every two instances are equal — and is unhashable.
        """
        first, second = TraceWriter(), TraceWriter()
        assert first != second
        assert first == first
        assert len({first, second}) == 2

    def test_repr_names_the_knobs(self):
        from tests._helpers.archive_fakes import FakeStepArchiver

        writer = TraceWriter(trace_dir="t", archive=[FakeStepArchiver()])
        assert repr(writer) == (
            "TraceWriter(trace_dir='t', record_secrets=False, archive=['contract_archive.steps'])"
        )


class TestTraceWriterLifecycle:
    async def test_on_run_start_creates_directory(self, writer):
        run_dir = await _start(writer)
        assert (run_dir / "manifest.json").exists()
        assert (run_dir / "config.json").exists()
        assert RunTrace(run_dir).manifest.format_version == 4

    async def test_config_records_full_identity(self, writer):
        """A trace nothing can join back to its dataset is an orphan.

        Identity arrives as *values*. ``AgentRuntime`` resolves it off the
        composed instances (``_component_id``) and passes it in; the writer
        does not re-derive it by ``getattr``.
        """
        await writer.on_run_start(
            agent=object(),
            env=object(),
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
        assert writer.run_dir is not None
        cfg = RunTrace(writer.run_dir).config
        assert (cfg.agent_id, cfg.loop_id, cfg.env_type, cfg.llm_model) == (
            "test_agent",
            "react",
            "browser",
            "gpt-4o",
        )
        assert (cfg.benchmark, cfg.subset, cfg.task_id, cfg.task) == (
            "online_mind2web",
            "Hard",
            "task-42",
            "find the price",
        )
        assert writer.run_dir.name == "fixed-run-id"

    async def test_record_step_commits_the_record_and_its_call(self, writer):
        run_dir = await _start(writer)
        await _call(writer, "what do you see?")
        await writer.after_record_step(None, args=(_step(0),), kwargs={"observation": None})

        trace = RunTrace(run_dir)
        assert trace.manifest.last_step_completed == 0
        record = trace.step(0)
        assert record is not None
        assert record.actions[0].name == "browser.click"
        assert record.token_usage is not None
        assert record.token_usage.total_tokens == 15
        (call,) = trace.llm_messages(0)
        assert (call.step, call.call, call.status) == (0, 0, "completed")
        assert call.messages == _prompt("what do you see?")
        assert call.completion is not None
        assert call.completion.completion == "click(button)"
        assert call.completion.usage == _usage()
        assert (run_dir / "messages/000_call_00.json").is_file()

    async def test_multi_step_commit(self, writer):
        run_dir = await _start(writer)
        for i in range(3):
            await _call(writer, f"step {i}")
            await writer.after_record_step(None, args=(_step(i),), kwargs={})

        trace = RunTrace(run_dir)
        assert trace.manifest.last_step_completed == 2
        assert writer._steps_written == 3
        assert [trace.llm_messages(i)[0].messages[0].text for i in range(3)] == [
            "step 0",
            "step 1",
            "step 2",
        ]

    async def test_a_screenshot_is_a_real_file_and_reads_back_with_its_bytes(self, writer):
        """Binary artifacts land as files, not base64 buried in JSON."""
        run_dir = await _start(writer)
        Model = build_observation_model(frozenset({"browser.screenshot", "loop.task"}))
        obs = Model(
            screenshot=ScreenshotObs(image_bytes=_PNG, viewport_width=1280, viewport_height=720),
            task=TaskObs(user_request="find the price"),
        )
        await _call(writer)
        await writer.after_record_step(None, args=(_step(0),), kwargs={"observation": obs})

        png = run_dir / "observations" / "000_browser.screenshot.png"
        assert png.read_bytes() == _PNG
        sidecar = json.loads((run_dir / "observations" / "000_browser.screenshot.json").read_text())
        assert sidecar["viewport_width"] == 1280
        assert "image_bytes" not in sidecar
        assert "artifact_file" not in sidecar

        trace = RunTrace(run_dir)
        assert trace.screenshots == [png]
        shot = trace.observation_fragment(0, "browser.screenshot")
        assert isinstance(shot, ScreenshotObs)
        assert shot.image_bytes == _PNG

    async def test_loop_fragments_reach_disk_named_by_their_full_key(self, writer):
        """The observation recorded is the one the *agent* saw."""
        run_dir = await _start(writer)
        Model = build_observation_model(frozenset({"loop.task"}))
        fragment = TaskObs(user_request="find the price")
        await _call(writer)
        await writer.after_record_step(
            None, args=(_step(0),), kwargs={"observation": Model(task=fragment)}
        )

        trace = RunTrace(run_dir)
        assert list(trace.manifest.steps[0].fragments) == ["loop.task"]
        task = trace.observation_fragment(0, "loop.task")
        assert isinstance(task, TaskObs)
        assert task.user_request == "find the price"
        # Without it, per-capability skew is unrecoverable after the run.
        assert task.captured_at == fragment.captured_at

    async def test_a_failed_fragment_reaches_the_trace_as_a_failure(self, writer):
        """An optional fragment nobody could capture is recorded, not faked.

        The index marks it failed, so it reads back as the failure — never
        as an empty ``ScreenshotObs`` its JSON would also validate as.
        """
        run_dir = await _start(writer)
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
        await _call(writer)
        await writer.after_record_step(None, args=(_step(0),), kwargs={"observation": obs})

        # No .png: nothing was captured, so no artifact is invented.
        assert not (run_dir / "observations" / "000_browser.screenshot.png").exists()
        trace = RunTrace(run_dir)
        assert trace.manifest.steps[0].fragments["browser.screenshot"].failed is True
        failure = trace.observation_fragment(0, "browser.screenshot")
        assert isinstance(failure, FragmentFailure)
        assert (failure.error, failure.detail, failure.capability) == (
            "TimeoutError",
            "CDP call timed out",
            "browser",
        )
        assert trace.screenshot(0) is None

    async def test_an_empty_capture_costs_its_own_frame_not_the_run(self, writer):
        """A frame of zero bytes is no frame — and not a reason to lose the others.

        It used to be committed as a 0-byte ``.png`` the reader rightly calls
        damage, so ``RunTrace.screenshots`` raised for the whole run and an
        image judge could grade none of it. The resolver now raises on an
        empty capture; a fragment that still carries ``b""`` writes no
        artifact at all.
        """
        run_dir = await _start(writer)
        Model = build_observation_model(frozenset({"browser.screenshot"}))
        for step, data in enumerate([_PNG, b"", _PNG]):
            await _call(writer, f"step {step}")
            obs = Model(screenshot=ScreenshotObs(image_bytes=data, viewport_width=1280))
            await writer.after_record_step(None, args=(_step(step),), kwargs={"observation": obs})

        trace = RunTrace(run_dir)
        observations = run_dir / "observations"
        assert trace.screenshots == [
            observations / "000_browser.screenshot.png",
            observations / "002_browser.screenshot.png",
        ]
        assert trace.screenshot(1) is None
        empty = trace.observation_fragment(1, "browser.screenshot")
        assert isinstance(empty, ScreenshotObs)
        assert empty.image_bytes is None
        assert empty.viewport_width == 1280
        assert not (observations / "001_browser.screenshot.png").exists()

    async def test_an_observation_that_is_not_a_fragment_model_is_not_recorded(self, writer):
        """The old ``NNN_observation.json`` fallback is gone; nothing could read it back."""
        run_dir = await _start(writer)
        await writer.after_record_step(
            None, args=(_step(0),), kwargs={"observation": {"dom": "<div/>"}}
        )

        assert RunTrace(run_dir).manifest.steps[0].fragments == {}
        assert list((run_dir / "observations").iterdir()) == []

    async def test_on_run_end_writes_result(self, writer):
        run_dir = await _start(writer)
        await writer.on_run_end(result=_result(), env=None)

        trace = RunTrace(run_dir)
        assert trace.result is not None
        assert trace.result.answer == "42"
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert trace.manifest.incomplete_reason is None

    async def test_a_timed_out_run_closes_a_complete_trace(self, writer):
        """Two ledgers: the agent timed out (result.json); the trace has all of it.

        Not 'writing' just because the agent timed out — and not 'incomplete'
        either: the manifest no longer says how the agent ended.
        """
        run_dir = await _start(writer)
        await writer.on_run_end(
            result=_result(AgentDoneStatus.TIMEOUT, error="Execution timed out after 1s")
        )

        trace = RunTrace(run_dir)
        assert trace.manifest.status == TraceStatus.COMPLETE
        assert trace.result is not None
        assert trace.result.agent_done == AgentDoneStatus.TIMEOUT

    async def test_a_run_with_no_result_writes_no_result_file(self, writer):
        """``result is None`` must mean "absent", never "unparseable" — and the trace says so."""
        run_dir = await _start(writer)
        await writer.on_run_end(result=None)

        assert not (run_dir / "result.json").exists()
        trace = RunTrace(run_dir)
        assert trace.result is None
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert "result.json was not written" in (trace.manifest.incomplete_reason or "")

    async def test_on_run_error_alone_closes_the_trace_out(self, writer):
        """A driver that dispatches only on_run_error: closed, with no result, so incomplete."""
        run_dir = await _start(writer)
        await writer.on_run_error(error=RuntimeError("boom"), partial_steps=[])

        manifest = TraceManifest.model_validate_json((run_dir / "manifest.json").read_text())
        assert manifest.status == TraceStatus.INCOMPLETE

    async def test_on_run_error_after_on_run_end_changes_nothing(self, writer):
        """The close-out happens once: the error that follows it neither reopens nor retries it."""
        run_dir = await _start(writer)
        await writer.on_run_end(result=_result(AgentDoneStatus.ERROR, error="it broke"))
        before = (run_dir / "manifest.json").read_bytes()
        await writer.on_run_error(error=RuntimeError("it broke"), partial_steps=[])

        assert (run_dir / "manifest.json").read_bytes() == before
        assert RunTrace(run_dir).manifest.status == TraceStatus.COMPLETE


class TestEveryCallOfAStep:
    """A step can make several calls; every one is recorded, in start order."""

    async def test_a_parse_retry_step_yields_every_call_in_order(self, writer):
        run_dir = await _start(writer)
        refused = StructuredOutputValidationError(
            "schema mismatch", raw_output='{"action": [{}]}', model="gpt-4o", usage=_usage(20)
        )
        with pytest.raises(StructuredOutputValidationError):
            await _call(writer, "first try", raises=refused, output_format=Reply)
        await _call(writer, "second try", returns=_completion("garbage"), output_format=Reply)
        await _call(writer, "third try", returns=_completion("click(3)"), output_format=Reply)
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        calls = RunTrace(run_dir).llm_messages(0)
        assert [(c.call, c.status, c.messages[0].text) for c in calls] == [
            (0, "failed", "first try"),
            (1, "completed", "second try"),
            (2, "completed", "third try"),
        ]

    async def test_a_refused_structured_reply_keeps_its_raw_output_and_cost(self, writer):
        run_dir = await _start(writer)
        refused = StructuredOutputValidationError(
            "schema mismatch", raw_output='{"action": [{}]}', model="gpt-4o", usage=_usage(20)
        )
        with pytest.raises(StructuredOutputValidationError):
            await _call(writer, raises=refused)
        # The Loop turns this into a parse error and surrenders a step.
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        (call,) = RunTrace(run_dir).llm_messages(0)
        assert call.status == "failed"
        assert call.completion is None
        assert call.failure is not None
        assert call.failure.error == "StructuredOutputValidationError"
        assert call.failure.raw_output == '{"action": [{}]}'
        assert call.failure.usage == _usage(20)

    async def test_a_truncation_keeps_what_the_model_had_written(self, writer):
        run_dir = await _start(writer)
        truncated = ModelOutputTruncatedError(
            stop_reason="length",
            ceiling_field="max_tokens",
            ceiling=32,
            usage=_usage(40),
            partial_output='{"action": "cli',
        )
        with pytest.raises(ModelOutputTruncatedError):
            await _call(writer, raises=truncated)
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        (call,) = RunTrace(run_dir).llm_messages(0)
        assert call.failure is not None
        assert call.failure.error == "ModelOutputTruncatedError"
        assert call.failure.partial_output == '{"action": "cli'
        assert call.failure.stop_reason == "length"
        assert call.failure.usage == _usage(40)
        assert call.failure.raw_output is None

    async def test_every_kind_of_call_in_one_step_reads_back_in_start_order(self, writer):
        """A parse retry, a refused reply, a truncation and a cancellation: four records.

        A cancelled call can belong to a committed step: a Loop that bounds
        each call with its own ``asyncio.wait_for`` sees ``TimeoutError``, while
        underneath the call itself was cancelled — and the step goes on.
        """
        run_dir = await _start(writer)
        await _call(writer, "attempt 0", returns=_completion("not an action"), output_format=Reply)
        refused = StructuredOutputValidationError(
            "schema mismatch", raw_output='{"action": 7}', model="gpt-4o", usage=_usage(20)
        )
        with pytest.raises(StructuredOutputValidationError):
            await _call(writer, "attempt 1", raises=refused, output_format=Reply)
        truncated = ModelOutputTruncatedError(
            stop_reason="length", ceiling=32, usage=_usage(40), partial_output='{"action": "cli'
        )
        with pytest.raises(ModelOutputTruncatedError):
            await _call(writer, "attempt 2", raises=truncated, output_format=Reply)
        with pytest.raises(asyncio.CancelledError):
            await _call(writer, "attempt 3", raises=asyncio.CancelledError(), output_format=Reply)
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        trace = RunTrace(run_dir)
        calls = trace.llm_messages(0)
        assert [(c.call, c.status, c.messages[0].text) for c in calls] == [
            (0, "completed", "attempt 0"),
            (1, "failed", "attempt 1"),
            (2, "failed", "attempt 2"),
            (3, "cancelled", "attempt 3"),
        ]
        retry, refused_call, truncated_call, cancelled = calls
        assert retry.completion is not None
        assert retry.completion.completion == "not an action"
        assert refused_call.failure is not None
        assert refused_call.failure.raw_output == '{"action": 7}'
        assert refused_call.failure.usage == _usage(20)
        assert truncated_call.failure is not None
        assert truncated_call.failure.partial_output == '{"action": "cli'
        assert truncated_call.failure.usage == _usage(40)
        assert cancelled.completion is None
        assert cancelled.failure is not None
        assert cancelled.failure.error == "CancelledError"
        refs = {call.output_format.sha256 for call in calls if call.output_format is not None}
        assert refs == set(trace.manifest.output_formats)
        assert len(refs) == 1
        assert trace.trailing_calls == []

    async def test_a_transport_failure_is_recorded_without_inventing_a_reply(self, writer):
        run_dir = await _start(writer)
        with pytest.raises(TimeoutError):
            await _call(writer, raises=TimeoutError("gateway gone"))
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        (call,) = RunTrace(run_dir).llm_messages(0)
        assert call.failure is not None
        assert (call.failure.error, call.failure.detail) == ("TimeoutError", "gateway gone")
        assert call.failure.raw_output is None
        assert call.failure.usage is None
        raw = json.loads((run_dir / "messages/000_call_00.json").read_text())
        assert raw["failure"] == {"error": "TimeoutError", "detail": "gateway gone"}

    async def test_the_exception_still_reaches_the_caller(self, writer):
        """The hook observes; it does not swallow."""
        await _start(writer)
        with pytest.raises(RuntimeError, match="boom"):
            await _call(writer, raises=RuntimeError("boom"))

    async def test_the_reply_is_returned_unchanged(self, writer):
        await _start(writer)
        completion = _completion("click(7)")
        assert await _call(writer, returns=completion) is completion

    @pytest.mark.parametrize(
        "raises",
        [
            StructuredOutputValidationError("schema mismatch", raw_output="{}", usage=_usage()),
            ModelOutputTruncatedError(stop_reason="length", ceiling=32, partial_output="{"),
            asyncio.CancelledError(),
        ],
        ids=["refused", "truncated", "cancelled"],
    )
    async def test_a_call_that_raised_still_counts_as_model_wait(self, writer, raises):
        """``llm_latency_ms`` is the step's wait on the model, whatever came back.

        The accumulator sits in ``around``'s ``finally``: a step whose only
        call was refused or cut off — a parse-retry step, say —
        waited all the same, and must not read as unmeasured.
        """
        run_dir = await _start(writer)

        async def proceed() -> Any:
            await asyncio.sleep(0.06)
            raise raises

        with pytest.raises(type(raises)):
            await writer.around_ainvoke(proceed=proceed, args=(_prompt("slow"),), kwargs={})
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        record = RunTrace(run_dir).step(0)
        assert record is not None
        assert record.llm_latency_ms is not None
        assert record.llm_latency_ms >= 50

    async def test_a_refused_call_and_a_retry_both_count(self, writer):
        run_dir = await _start(writer)

        async def refused() -> Any:
            await asyncio.sleep(0.06)
            raise StructuredOutputValidationError("schema mismatch", raw_output="{}")

        async def answered() -> Any:
            await asyncio.sleep(0.06)
            return _completion()

        with pytest.raises(StructuredOutputValidationError):
            await writer.around_ainvoke(proceed=refused, args=(_prompt("a"),), kwargs={})
        await writer.around_ainvoke(proceed=answered, args=(_prompt("b"),), kwargs={})
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        record = RunTrace(run_dir).step(0)
        assert record is not None and record.llm_latency_ms is not None
        assert record.llm_latency_ms >= 100

    async def test_the_prompt_is_the_one_sent(self, writer):
        """A message the memory mutates after the call does not rewrite the record."""
        run_dir = await _start(writer)
        message = UserMessage(content="as sent")

        async def proceed() -> Any:
            return _completion()

        await writer.around_ainvoke(proceed=proceed, args=([message],), kwargs={})
        message.content = "rewritten afterwards"
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        (call,) = RunTrace(run_dir).llm_messages(0)
        assert call.messages[0].text == "as sent"


class TestTheModelsReasoning:
    """A reply's reasoning goes into its call record (``docs/architecture_design.md`` §5.3)."""

    async def test_it_is_written_to_the_call_record_and_read_back(self, writer):
        run_dir = await _start(writer)
        completion = ChatInvokeCompletion(
            completion="click(3)",
            thinking="The third element is the search button.",
            redacted_thinking="EuYBCkQYAiJA-opaque",
            usage=_usage(),
            stop_reason="end_turn",
        )
        await _call(writer, returns=completion)
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        raw = json.loads((run_dir / "messages/000_call_00.json").read_text())
        assert raw["completion"]["thinking"] == "The third element is the search button."
        assert raw["completion"]["redacted_thinking"] == "EuYBCkQYAiJA-opaque"
        (call,) = RunTrace(run_dir).llm_messages(0)
        assert call.completion is not None
        assert call.completion.completion == "click(3)"
        assert call.completion.thinking == "The third element is the search button."
        assert call.completion.redacted_thinking == "EuYBCkQYAiJA-opaque"

    async def test_a_reply_without_reasoning_records_none(self, writer):
        run_dir = await _start(writer)
        await _call(writer, returns=_completion("click(3)"))
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        (call,) = RunTrace(run_dir).llm_messages(0)
        assert call.completion is not None
        assert call.completion.thinking is None
        assert call.completion.redacted_thinking is None


class TestTheRequestedSchema:
    async def test_written_once_per_run_and_referenced_by_every_call(self, writer):
        run_dir = await _start(writer)
        for step in range(2):
            await _call(writer, f"step {step}", output_format=Reply)
            await _call(writer, f"step {step} again", output_format=Reply)
            await writer.after_record_step(None, args=(_step(step),), kwargs={})

        trace = RunTrace(run_dir)
        (sha,) = trace.manifest.output_formats
        refs = [call.output_format for step in range(2) for call in trace.llm_messages(step)]
        assert len(refs) == 4
        assert all(ref is not None and (ref.name, ref.sha256) == ("Reply", sha) for ref in refs)
        assert trace.output_schema(sha) == Reply.model_json_schema()
        assert list((run_dir / "messages/output_formats").iterdir()) == [
            run_dir / f"messages/output_formats/{sha}.json"
        ]

    async def test_generated_once_per_class(self, writer, monkeypatch):
        await _start(writer)
        generated: list[Any] = []
        real = Reply.model_json_schema

        def counting(*args: Any, **kwargs: Any) -> Any:
            generated.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(Reply, "model_json_schema", counting)
        for _ in range(3):
            await _call(writer, output_format=Reply)
        assert len(generated) == 1

    async def test_an_unstructured_call_references_none(self, writer):
        run_dir = await _start(writer)
        await _call(writer)
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        trace = RunTrace(run_dir)
        assert trace.llm_messages(0)[0].output_format is None
        assert trace.manifest.output_formats == []

    async def test_a_schema_that_cannot_be_generated_does_not_fail_the_call(self, writer):
        run_dir = await _start(writer)
        await _call(writer, output_format=object)  # type: ignore[arg-type]
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        assert RunTrace(run_dir).llm_messages(0)[0].output_format is None


class TestTrailingCalls:
    """Run end commits the calls of a step the Loop never surrendered.

    ``after_record_step`` is the commit point, but a run that times out,
    is cancelled or crashes between a call and the step never reaches it.
    """

    async def test_run_end_commits_a_completed_call_as_trailing(self, writer):
        run_dir = await _start(writer)
        await _call(writer, "step 0 prompt")
        await writer.on_run_end(result=_result(AgentDoneStatus.TIMEOUT))

        trace = RunTrace(run_dir)
        (call,) = trace.trailing_calls
        assert (call.step, call.call, call.status) == (0, 0, "completed")
        assert call.messages[0].text == "step 0 prompt"
        assert trace.manifest.trailing[0].llm_calls == ["messages/000_call_00.json"]

    async def test_a_cancelled_call_keeps_its_prompt(self, writer):
        run_dir = await _start(writer)
        with pytest.raises(asyncio.CancelledError):
            await _call(writer, "the call the timeout cut off", raises=asyncio.CancelledError())
        await writer.on_run_end(result=_result(AgentDoneStatus.TIMEOUT))

        (call,) = RunTrace(run_dir).trailing_calls
        assert call.status == "cancelled"
        assert call.messages[0].text == "the call the timeout cut off"
        assert call.completion is None
        assert call.failure is not None
        assert call.failure.error == "CancelledError"

    async def test_trailing_calls_do_not_fabricate_a_completed_step(self, writer):
        run_dir = await _start(writer)
        await _call(writer)
        await writer.on_run_end(result=_result(AgentDoneStatus.TIMEOUT))

        trace = RunTrace(run_dir)
        assert trace.step_count == 0
        assert trace.manifest.steps == []
        assert not (run_dir / "steps" / "000.json").exists()

    async def test_the_index_follows_the_last_committed_step(self, writer):
        run_dir = await _start(writer)
        await _call(writer, "step 0")
        await writer.after_record_step(None, args=(_step(0),), kwargs={})
        # Step 1's call comes back; the run dies before record_step.
        await _call(writer, "step 1")
        await writer.on_run_end(result=_result(AgentDoneStatus.TIMEOUT))

        trace = RunTrace(run_dir)
        assert trace.step_count == 1
        (call,) = trace.trailing_calls
        assert (call.step, call.messages[0].text) == (1, "step 1")
        assert trace.llm_messages(1) == []
        # The committed step's call is untouched.
        assert trace.llm_messages(0)[0].messages[0].text == "step 0"

    async def test_every_registered_call_is_committed(self, writer):
        run_dir = await _start(writer)
        await _call(writer, "a")
        with pytest.raises(TimeoutError):
            await _call(writer, "b", raises=TimeoutError("socket"))
        await writer.on_run_end(result=_result(AgentDoneStatus.ERROR))

        calls = RunTrace(run_dir).trailing_calls
        assert [(c.call, c.status) for c in calls] == [(0, "completed"), (1, "failed")]

    async def test_on_run_error_alone_commits_them(self, writer):
        """A driver that only dispatches on_run_error must not lose them."""
        run_dir = await _start(writer)
        await _call(writer, "lonely")
        await writer.on_run_error(error=RuntimeError("boom"), partial_steps=[])

        assert RunTrace(run_dir).trailing_calls[0].messages[0].text == "lonely"

    async def test_end_then_error_commits_them_once(self, writer):
        run_dir = await _start(writer)
        await _call(writer, "once")
        await writer.on_run_end(result=_result(AgentDoneStatus.ERROR))
        await writer.on_run_error(error=RuntimeError("boom"), partial_steps=[])

        trace = RunTrace(run_dir)
        assert len(trace.manifest.trailing) == 1
        assert len(trace.trailing_calls) == 1

    async def test_a_clean_run_records_no_trailing_calls(self, writer):
        run_dir = await _start(writer)
        await _call(writer)
        await writer.after_record_step(None, args=(_step(0),), kwargs={})
        await writer.on_run_end(result=_result())

        trace = RunTrace(run_dir)
        assert trace.trailing_calls == []
        assert trace.manifest.trailing == []
        assert trace.manifest.status == TraceStatus.COMPLETE

    async def test_a_step_committed_after_its_trailing_calls_numbers_after_them(self, writer):
        """A driver that drains and keeps going must not collide with what it drained."""
        run_dir = await _start(writer)
        await _call(writer, "before the drain")
        await writer.on_run_error(error=RuntimeError("transient"), partial_steps=[])
        await _call(writer, "after the drain")
        await writer.after_record_step(None, args=(_step(0),), kwargs={})

        # Only the drained step continues the count; the next starts at 0.
        await _call(writer, "next step")
        await writer.after_record_step(None, args=(_step(1),), kwargs={})

        trace = RunTrace(run_dir)
        assert [(c.call, c.messages[0].text) for c in trace.trailing_calls] == [
            (0, "before the drain")
        ]
        assert [(c.call, c.messages[0].text) for c in trace.llm_messages(0)] == [
            (1, "after the drain")
        ]
        assert [(c.call, c.messages[0].text) for c in trace.llm_messages(1)] == [(0, "next step")]

    async def test_calls_of_a_step_that_failed_to_commit_are_kept_for_run_end(
        self, writer, monkeypatch
    ):
        run_dir = await _start(writer)
        await _call(writer, "owed to the trace")
        assert writer._wal is not None

        def failing_commit(*args: Any, **kwargs: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(writer._wal, "commit_step", failing_commit)
        with pytest.raises(RecordingFailedError, match="recording failed at step 0: OSError"):
            await writer.after_record_step(None, args=(_step(0),), kwargs={})
        monkeypatch.undo()
        await writer.on_run_end(result=_result(AgentDoneStatus.ERROR))

        trace = RunTrace(run_dir)
        (call,) = trace.trailing_calls
        assert call.messages[0].text == "owed to the trace"
        # The hole: the trace is incomplete, complete through no step.
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.complete_through == -1
        assert (trace.manifest.incomplete_reason or "").startswith(
            "recording failed at step 0: OSError: disk full"
        )

    async def test_a_step_the_agent_called_done_on_is_never_raised_over(self, writer, monkeypatch):
        """Its outcome is decided before it is recorded: the hole is the trace's, not the run's."""
        run_dir = await _start(writer)
        await writer.after_record_step(None, args=(_step(0),), kwargs={})
        assert writer._wal is not None

        def failing_commit(*args: Any, **kwargs: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(writer._wal, "commit_step", failing_commit)
        done = _step(1, action_results=[ActionResult(success=True, is_done=True)])
        await writer.after_record_step(None, args=(done,), kwargs={})  # not raised
        with pytest.raises(RecordingFailedError, match="recording failed at step 2"):
            await writer.after_record_step(None, args=(_step(2),), kwargs={})
        monkeypatch.undo()
        await writer.on_run_end(result=_result())

        manifest = RunTrace(run_dir).manifest
        assert (manifest.status, manifest.complete_through) == (TraceStatus.INCOMPLETE, 0)
        # The first hole is the reason; the later failure follows from it.
        assert manifest.incomplete_reason == "recording failed at step 1: OSError: disk full"

    async def test_the_terminal_state_lands_even_if_the_trailing_commit_fails(
        self, writer, monkeypatch
    ):
        run_dir = await _start(writer)
        await _call(writer)
        assert writer._wal is not None

        def failing_trailing(**kwargs: Any) -> None:
            raise OSError("disk hiccup")

        monkeypatch.setattr(writer._wal, "commit_trailing", failing_trailing)
        # Not raised: the run's outcome is decided; the trace says what it lost.
        await writer.on_run_end(result=_result(AgentDoneStatus.TIMEOUT))

        trace = RunTrace(run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert "OSError: disk hiccup" in (trace.manifest.incomplete_reason or "")
        assert trace.result is not None
        assert trace.trailing_calls == []  # dropped, not retried

    async def test_the_terminal_state_lands_even_if_result_json_cannot_be_written(
        self, writer, monkeypatch
    ):
        """Not ``writing`` forever — and not ``complete`` without the result it claims."""
        run_dir = await _start(writer)
        assert writer._wal is not None

        def failing_result(result: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(writer._wal, "write_result", failing_result)
        await writer.on_run_end(result=_result())

        trace = RunTrace(run_dir)
        assert trace.manifest.status == TraceStatus.INCOMPLETE
        assert trace.manifest.incomplete_reason == (
            "result.json could not be written: OSError: disk full"
        )
        assert trace.result is None
