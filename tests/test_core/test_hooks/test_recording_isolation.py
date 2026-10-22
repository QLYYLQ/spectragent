"""Recorder faults preserve execution outcomes and sealed segment identities.

These acceptance tests drive the public hook boundaries directly, independently
of any Loop implementation, then inspect the committed trace and live health.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from spectragent.core.archive import ArchiveBatch
from spectragent.core.hooks import trace_writer as hook_module
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.exceptions import StructuredOutputValidationError
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.negotiation import ArchiveNegotiationResult
from spectragent.core.trace import encoding, fsio
from spectragent.core.trace.errors import RecordingScopeError
from spectragent.core.trace.models import TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.usage import recorded_usage
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.agent_result import AgentMetrics
from spectragent.schemas.prompt import PromptRef, TextPrompt
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.step_record import StepRecord
from tests._helpers.archive_fakes import CAPABILITY_ID, STEPS_KEY, FakeStepArchiver
from tests._helpers.trace_builder import (
    agent_result,
    archive_item,
    as_chat,
    required,
    step_record,
    usage,
)

pytestmark = pytest.mark.usefixtures("archive_fakes")


@pytest.fixture()
async def writer_factory(tmp_path):
    writers: list[TraceWriter] = []

    def make(**kwargs: Any) -> TraceWriter:
        writer = TraceWriter(
            trace_dir=str(tmp_path / f"recording-{len(writers)}"), retry_delay=0, **kwargs
        )
        writers.append(writer)
        return writer

    yield make
    for writer in writers:
        await writer.on_run_end(result=agent_result(metrics=AgentMetrics(steps=0)))


async def start(writer: TraceWriter, **kwargs: Any) -> None:
    await writer.on_run_start(task="test execution", llm_model="test-model", **kwargs)


async def call(
    writer: TraceWriter,
    text: str = "decide",
    *,
    returns: Any = None,
    raises: BaseException | None = None,
    messages: Any = None,
) -> Any:
    async def proceed() -> Any:
        if raises is not None:
            raise raises
        return returns

    return await writer.around_ainvoke(
        proceed=proceed,
        args=([UserMessage(content=text)] if messages is None else messages,),
        kwargs={},
    )


async def seal(writer: TraceWriter, step: int, *, observation: Any = None) -> StepRecord:
    record = step_record(step, llm_output=f"action of step {step}")
    await writer.after_record_step(None, args=(record,), kwargs={"observation": observation})
    return record


async def close(writer: TraceWriter, *, steps: int = 1):
    result = agent_result(metrics=AgentMetrics(steps=steps))
    before = (result.agent_done, result.answer, result.error)
    await writer.on_run_end(result=result)
    assert (result.agent_done, result.answer, result.error) == before
    return result


def trace_of(writer: TraceWriter) -> RunTrace:
    assert writer.run_dir is not None
    return RunTrace(writer.run_dir)


def completion(text: str = "click", *, total: int = 15) -> ChatInvokeCompletion:
    return ChatInvokeCompletion(completion=text, usage=usage(total - 5, 5), stop_reason="end_turn")


class ScriptedArchive:
    def __init__(self, *outcomes: ArchiveBatch | BaseException) -> None:
        self.outcomes = list(outcomes)

    async def collect_archive(self) -> ArchiveBatch:
        outcome = self.outcomes.pop(0) if self.outcomes else ArchiveBatch()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def start_archiving(writer: TraceWriter, env: ScriptedArchive | StalledArchive) -> None:
    await start(
        writer,
        env=env,
        archive_negotiation=ArchiveNegotiationResult(
            active_archivers=frozenset({STEPS_KEY}),
            archive_bindings={STEPS_KEY: CAPABILITY_ID},
        ),
    )


async def test_persistent_initialization_failure_preserves_calls_and_run_outcome(
    writer_factory, monkeypatch
):
    def unavailable(*args, **kwargs):
        raise OSError("initialization storage unavailable")

    monkeypatch.setattr(WALWriter, "init_run", unavailable)
    writer = writer_factory(write_retries=1)
    await start(writer)
    returned = completion()
    assert await call(writer, returns=returned) is returned
    record = await seal(writer, 0)
    assert record.actions[0].name == "browser.click"
    assert record.action_results[0].success is True
    result = await close(writer)
    assert writer.health.status == "incomplete"
    assert writer.health.missing_steps == [0]
    assert result.metrics.llm_calls == 1
    assert result.metrics.usage is not None
    assert result.metrics.usage.total_tokens == 15


async def test_recovered_initialization_failure_does_not_leave_current_trace_incomplete(
    writer_factory, monkeypatch
):
    original = WALWriter.init_run
    attempts = 0

    def unavailable_once(wal, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("initialization temporarily unavailable")
        return original(wal, *args, **kwargs)

    monkeypatch.setattr(WALWriter, "init_run", unavailable_once)
    writer = writer_factory()
    await start(writer)
    await call(writer, returns=completion())
    await seal(writer, 0)
    await close(writer)
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.COMPLETE
    assert trace.manifest.missing_steps == []
    assert writer.health.problems
    assert all(problem.recovered for problem in writer.health.problems)


async def test_health_snapshot_cannot_erase_a_recording_fault(writer_factory, monkeypatch):
    def refuse_observation(*args, **kwargs):
        raise ValueError("capture failed")

    monkeypatch.setattr(hook_module, "encode_observation", refuse_observation)
    writer = writer_factory()
    await start(writer)
    await seal(writer, 0)
    health = writer.health
    health.problems[0].recovered = True
    health.problems.clear()
    health.expected_steps = 0

    result = await close(writer)

    assert writer.health.status == "incomplete"
    assert writer.health.missing_steps == [0]
    assert writer.health.problems[0].recovered is False
    assert trace_of(writer).manifest.status is TraceStatus.INCOMPLETE
    assert result.metrics.recording_health is not None
    assert result.metrics.recording_health.missing_steps == [0]


@pytest.mark.parametrize("fault", ["encoding", "commit"])
async def test_failed_first_segment_is_backfilled_after_second_without_mixing_calls(
    writer_factory, monkeypatch, fault
):
    attempts: list[int] = []
    failed = False
    if fault == "commit":
        original = WALWriter.commit_step

        def commit(wal, step, *args, **kwargs):
            nonlocal failed
            attempts.append(step)
            if step == 0 and not failed:
                failed = True
                raise OSError("first segment write failed")
            return original(wal, step, *args, **kwargs)

        monkeypatch.setattr(WALWriter, "commit_step", commit)
    else:
        original_encode = hook_module.encode_llm_calls

        def encode(step, *args, **kwargs):
            nonlocal failed
            attempts.append(step)
            if step == 0 and not failed:
                failed = True
                raise ValueError("first segment encoding failed")
            return original_encode(step, *args, **kwargs)

        monkeypatch.setattr(hook_module, "encode_llm_calls", encode)

    writer = writer_factory()
    await start(writer)
    await call(writer, "first prompt", returns=completion("first reply"))
    await seal(writer, 0)
    await call(writer, "second prompt", returns=completion("second reply", total=25))
    await seal(writer, 1)
    result = await close(writer, steps=2)
    trace = trace_of(writer)
    assert attempts == [0, 1, 0]
    assert trace.manifest.status is TraceStatus.COMPLETE
    assert (trace.manifest.complete_through, trace.manifest.missing_steps) == (1, [])
    assert trace.trailing_calls == []
    for step, text in enumerate(("first", "second")):
        (recorded,) = trace.llm_messages(step)
        assert (recorded.step, recorded.call) == (step, 0)
        assert as_chat(recorded).messages[-1].text == f"{text} prompt"
        assert as_chat(recorded).completion is not None
        assert required(as_chat(recorded).completion).completion == f"{text} reply"
        step_value = trace.step(step)
        assert step_value is not None
        assert step_value.actions[0].params["index"] == step
    assert result.metrics.usage is not None
    assert result.metrics.usage.total_tokens == recorded_usage(trace).tokens == 40


async def test_observation_capture_failure_loses_only_its_own_segment(writer_factory, monkeypatch):
    original = hook_module.encode_observation

    def encode(step, observation):
        if step == 0:
            raise RuntimeError("observation encoding failed")
        return original(step, observation)

    monkeypatch.setattr(hook_module, "encode_observation", encode)
    writer = writer_factory()
    await start(writer)
    await call(writer, "lost prompt", returns=completion())
    await seal(writer, 0)
    await call(writer, "saved prompt", returns=completion())
    await seal(writer, 1)
    result = await close(writer, steps=2)
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.INCOMPLETE
    assert trace.manifest.missing_steps == [0]
    assert trace.step(0) is None
    assert trace.llm_messages(0) == []
    assert trace.trailing_calls == []
    assert [as_chat(item).messages[-1].text for item in trace.llm_messages(1)] == ["saved prompt"]
    assert result.metrics.llm_calls == 2
    assert recorded_usage(trace).calls == 1
    assert not recorded_usage(trace).complete


async def test_archive_collection_failure_preserves_action_result_and_future_capture(
    writer_factory,
):
    writer = writer_factory(archive=[FakeStepArchiver()])
    await start_archiving(writer, ScriptedArchive(OSError("capture failed"), ArchiveBatch()))
    await call(writer, "lost", returns=completion())
    record = await seal(writer, 0)
    assert record.action_results[0].success is True
    await call(writer, "next", returns=completion())
    await seal(writer, 1)
    await close(writer, steps=2)
    trace = trace_of(writer)
    assert trace.manifest.missing_steps == [0]
    assert [as_chat(item).messages[-1].text for item in trace.llm_messages(1)] == ["next"]


async def test_call_capture_failure_does_not_replace_success_or_original_error(
    writer_factory, monkeypatch
):
    def cannot_capture(*args, **kwargs):
        raise RuntimeError("call snapshot failed")

    monkeypatch.setattr(hook_module, "PendingLLMCall", cannot_capture)
    writer = writer_factory()
    await start(writer)
    returned = completion()
    assert await call(writer, returns=returned) is returned
    original = OSError("actual model transport failed")
    with pytest.raises(OSError) as caught:
        await call(writer, raises=original)
    assert caught.value is original
    await seal(writer, 0)
    await close(writer)
    assert writer.health.status == "incomplete"
    assert all(problem.phase == "capture" for problem in writer.health.problems)


async def test_missing_requested_schema_caps_online_and_persisted_complete_prefix(writer_factory):
    class UnencodableSchema(BaseModel):
        action: str

        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            raise ValueError("requested schema could not be encoded")

    writer = writer_factory()
    await start(writer)
    returned = completion()

    async def proceed():
        return returned

    actual = await writer.around_ainvoke(
        proceed=proceed,
        args=([UserMessage(content="first")],),
        kwargs={"output_format": UnencodableSchema},
    )
    assert actual is returned
    await seal(writer, 0)
    await call(writer, "second", returns=completion())
    await seal(writer, 1)
    result = await close(writer, steps=2)
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.INCOMPLETE
    assert trace.manifest.missing_steps == []
    assert trace.manifest.complete_through == -1
    assert trace.result is not None and trace.result.metrics.recording_health is not None
    assert result.metrics.recording_health is not None
    assert trace.result.metrics.recording_health.complete_through == -1
    assert result.metrics.recording_health.complete_through == -1
    assert [len(trace.llm_messages(step)) for step in (0, 1)] == [1, 1]
    assert recorded_usage(trace).tokens == 30


async def test_finalize_failure_preserves_the_successful_execution(writer_factory, monkeypatch):
    def cannot_close(*args, **kwargs):
        raise OSError("manifest close failed")

    monkeypatch.setattr(WALWriter, "finalize", cannot_close)
    writer = writer_factory()
    await start(writer)
    await call(writer, returns=completion())
    await seal(writer, 0)
    result = await close(writer)
    assert result.metrics.recording_health is not None
    assert result.metrics.recording_health.status == "incomplete"
    assert trace_of(writer).manifest.status is TraceStatus.WRITING
    assert any(problem.channel == "manifest" for problem in writer.health.problems)


async def test_usage_keeps_zero_missing_failed_and_auxiliary_calls_distinct(writer_factory):
    writer = writer_factory()
    await start(writer)
    zero = ChatInvokeCompletion(completion="zero", usage=usage(0, 0))
    await call(writer, returns=zero)
    await call(writer, returns=ChatInvokeCompletion(completion="unknown", usage=None))
    billed_failure = StructuredOutputValidationError(
        "bad response",
        raw_output="bad",
        usage=usage(9, 4).model_copy(
            update={"prompt_cached_tokens": 3, "prompt_cache_creation_tokens": 2}
        ),
    )
    with pytest.raises(StructuredOutputValidationError) as caught:
        await call(writer, raises=billed_failure)
    assert caught.value is billed_failure
    with writer.call_context.scope("prompt_injector", "context_summary", step=0):
        await call(writer, returns=ChatInvokeCompletion(completion="summary", usage=usage(6, 2)))
    record = await seal(writer, 0)
    result = await close(writer)
    trace = trace_of(writer)
    facts = trace.call_metrics(0)
    assert [(fact.caller, fact.purpose) for fact in facts] == [
        ("agent", "agent_decision"),
        ("agent", "agent_decision"),
        ("agent", "agent_decision"),
        ("prompt_injector", "context_summary"),
    ]
    assert facts[0].usage is not None and facts[0].usage.total_tokens == 0
    assert facts[1].usage is None
    assert facts[2].status == "failed" and facts[2].usage is not None
    assert facts[2].usage.total_tokens == 13
    assert record.usage_coverage is not None
    assert (record.usage_coverage.calls, record.usage_coverage.reported_calls) == (4, 3)
    assert result.metrics.usage is not None
    assert record.token_usage is not None
    assert result.metrics.usage.total_tokens == record.token_usage.total_tokens == 21
    assert (
        result.metrics.usage.total_prompt_cached_tokens
        == record.token_usage.prompt_cached_tokens
        == 3
    )
    assert (
        result.metrics.usage.total_prompt_cache_creation_tokens
        == record.token_usage.prompt_cache_creation_tokens
        == 2
    )
    measured = recorded_usage(trace)
    assert measured.calls == 4 and measured.tokens == 21 and not measured.complete
    assert len(measured.without_usage) == 1
    assert recorded_usage(trace, purpose="context_summary").tokens == 8


@pytest.mark.parametrize("measured", [False, True])
async def test_no_calls_are_distinct_from_disabled_measurement(writer_factory, measured):
    writer = writer_factory(recording=RecordingPlan(metrics=measured))
    await start(writer)
    await seal(writer, 0)
    result = await close(writer)
    online = result.metrics
    offline = recorded_usage(trace_of(writer))
    if measured:
        assert online.llm_calls == offline.calls == 0
        assert online.usage is not None and online.usage.total_tokens == offline.tokens == 0
        assert online.usage_coverage is not None and online.usage_coverage.complete
    else:
        assert online.llm_calls is None and offline.calls is None
        assert online.usage is None and offline.tokens is None


async def test_metrics_only_never_copies_prompt_reply_images_or_observation(
    writer_factory, monkeypatch
):
    writer = writer_factory(
        recording=RecordingPlan(metrics=True, errors=True, trajectory=False, observations=False)
    )
    await start(writer)
    forbidden_prompt = object()
    forbidden_reply = object()
    forbidden_observation = object()
    original_snapshot = encoding.snapshot

    def snapshot(value):
        assert value is not forbidden_prompt
        assert value is not forbidden_reply
        assert value is not forbidden_observation
        return original_snapshot(value)

    def forbidden(*args, **kwargs):
        pytest.fail("unselected trajectory or observation was encoded")

    monkeypatch.setattr(encoding, "snapshot", snapshot)
    monkeypatch.setattr(hook_module, "encode_observation", forbidden)
    monkeypatch.setattr(hook_module, "encode_llm_calls", forbidden)
    monkeypatch.setattr(encoding, "store_prompt_images", forbidden)
    returned = SimpleNamespace(completion=forbidden_reply, usage=usage())
    assert await call(writer, returns=returned, messages=forbidden_prompt) is returned
    await seal(writer, 0, observation=forbidden_observation)
    await close(writer)
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.COMPLETE
    with pytest.raises(RecordingScopeError):
        trace.llm_messages(0)
    assert trace.manifest.steps[0].llm_calls == []
    assert trace.manifest.steps[0].fragments == {}
    assert len(trace.call_metrics(0)) == 1
    assert recorded_usage(trace).tokens == 15
    assert not any(
        file.kind in {"llm_call", "fragment", "artifact", "prompt_image"}
        for file in trace.committed_files
    )


async def test_lightweight_sealed_metrics_are_independent_of_later_record_mutation(writer_factory):
    writer = writer_factory(
        recording=RecordingPlan(metrics=True, errors=True, trajectory=False, observations=False)
    )
    await start(writer)
    await call(writer, returns=completion())
    record = await seal(writer, 0)
    assert record.token_usage is not None and record.usage_coverage is not None
    record.token_usage.total_tokens = 9000
    record.usage_coverage.reported_calls = 0
    record.usage_coverage.missing_call_ids.append("unrelated mutation")
    await close(writer)
    trace = trace_of(writer)
    saved = trace.step(0)
    assert saved is not None and saved.token_usage is not None and saved.usage_coverage is not None
    assert saved.token_usage.total_tokens == recorded_usage(trace).tokens == 15
    assert saved.usage_coverage.reported_calls == 1
    assert saved.usage_coverage.missing_call_ids == []


async def test_metrics_only_result_omits_trajectory_without_changing_live_result(writer_factory):
    writer = writer_factory(
        recording=RecordingPlan(metrics=True, errors=True, trajectory=False, observations=False)
    )
    await start(writer)
    await call(writer, returns=completion())
    await seal(writer, 0)
    result = agent_result(
        metrics=AgentMetrics(steps=1),
        action_history=["private trajectory marker"],
        screenshots=["large screenshot marker"],
        system_prompt=TextPrompt(
            kind="text",
            text="large prompt marker",
            ref=PromptRef(path="prompt.txt", content_hash="digest"),
        ),
        agent_metadata={"raw_input": "large metadata marker"},
        config={"raw_prompt": "large config marker"},
    )
    original = result.model_copy(deep=True)
    await writer.on_run_end(result=result)
    assert result.action_history == original.action_history
    assert result.screenshots == original.screenshots
    assert result.system_prompt == original.system_prompt
    assert result.agent_metadata == original.agent_metadata
    assert result.config == original.config
    trace = trace_of(writer)
    saved = trace.result
    assert saved is not None
    assert (saved.agent_done, saved.answer, saved.error) == (
        original.agent_done,
        original.answer,
        original.error,
    )
    assert saved.action_history == saved.screenshots == []
    assert saved.system_prompt is None
    assert saved.agent_metadata == saved.config == {}
    assert saved.metrics.usage is not None and saved.metrics.usage.total_tokens == 15
    serialized = trace.read_committed("result.json")
    assert b"marker" not in serialized


async def test_call_snapshot_overhead_belongs_to_its_step_and_not_execution(
    writer_factory, monkeypatch
):
    writer = writer_factory()
    await start(writer)
    clock = [0.0]
    monkeypatch.setattr(
        hook_module,
        "time",
        SimpleNamespace(perf_counter=lambda: clock[0], time=lambda: 1000.0 + clock[0]),
    )
    messages = [UserMessage(content="snapshotted prompt")]
    answer = "snapshotted reply"
    original_snapshot = encoding.snapshot

    def snapshot(value):
        if value is messages:
            clock[0] += 1.0
        elif value is answer:
            clock[0] += 2.0
        return original_snapshot(value)

    monkeypatch.setattr(encoding, "snapshot", snapshot)

    async def sink():
        return None

    async def first_execution():
        async def provider():
            clock[0] += 5.0
            return ChatInvokeCompletion(completion=answer, usage=usage())

        await writer.around_ainvoke(proceed=provider, args=(messages,), kwargs={})
        await writer.around_record_step(proceed=sink, args=(step_record(0),), kwargs={})

    await writer.around_execute_step(proceed=first_execution, args=(None, 0), kwargs={})
    clock[0] = 100.0

    async def second_execution():
        clock[0] += 7.0
        await writer.around_record_step(proceed=sink, args=(step_record(1),), kwargs={})

    await writer.around_execute_step(proceed=second_execution, args=(None, 1), kwargs={})
    await close(writer, steps=2)
    trace = trace_of(writer)
    first, second = trace.step(0), trace.step(1)
    assert first is not None and second is not None
    assert first.duration_ms == 5000
    assert second.duration_ms == 7000
    assert trace.recording_timing(0) == 3000
    assert trace.recording_timing(1) == 0


async def test_cancelled_call_retains_prior_usage_and_propagates_same_cancellation(writer_factory):
    writer = writer_factory()
    await start(writer)
    await call(writer, returns=completion(total=30))
    cancelled = asyncio.CancelledError("actual external cancellation")
    with pytest.raises(asyncio.CancelledError) as caught:
        await call(writer, raises=cancelled)
    assert caught.value is cancelled
    result = await close(writer, steps=0)
    trace = trace_of(writer)
    assert [fact.status for fact in trace.trailing_call_metrics] == ["completed", "cancelled"]
    assert result.metrics.usage is not None
    assert result.metrics.usage.total_tokens == recorded_usage(trace).tokens == 30


async def test_tail_archive_failure_does_not_discard_unfinished_step_calls(writer_factory):
    writer = writer_factory(archive=[FakeStepArchiver()])
    await start_archiving(writer, ScriptedArchive(OSError("tail archive unavailable")))
    await call(writer, "unfinished step", returns=completion())
    await close(writer, steps=0)
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.INCOMPLETE
    assert [as_chat(item).messages[-1].text for item in trace.trailing_calls] == ["unfinished step"]
    assert recorded_usage(trace).tokens == 15


class StalledArchive:
    """Each step's collection hands over nothing; the close-out's never finishes (a hung browser)."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = False

    async def collect_archive(self) -> ArchiveBatch:
        self.calls += 1
        if self.calls == 1:
            return ArchiveBatch()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


async def test_a_close_out_collection_that_outlasts_the_drain_budget_still_closes_the_trace(
    writer_factory, caplog
):
    """The last collection overran; the drain and the result still get their own time.

    Its budget is longer than the drain's on purpose: on one shared clock the
    collection would have used the drain's time too, the worker would have
    been stopped before it wrote ``result.json``, and the manifest would have
    stayed ``writing``.
    """
    writer = writer_factory(
        archive=[FakeStepArchiver()], close_timeout=0.2, closeout_collection_timeout=0.3
    )
    env = StalledArchive()
    await start_archiving(writer, env)
    await call(writer, returns=completion())
    await seal(writer, 0)
    caplog.set_level("WARNING", logger=hook_module.__name__)
    result = await close(writer)

    assert env.cancelled, "the overrunning collection is cancelled, not left running"
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.INCOMPLETE
    assert trace.result is not None
    assert trace.step(0) is not None
    assert trace.manifest.complete_through == 0
    (problem,) = writer.health.problems
    assert (problem.step, problem.channel, problem.phase, problem.error) == (
        None,
        "archive",
        "closeout",
        "TimeoutError",
    )
    overran = "the close-out archive collection did not finish within 0.3 s"
    assert problem.detail.startswith(overran)
    assert overran in (trace.manifest.incomplete_reason or "")
    assert result.metrics.recording_health is not None
    assert result.metrics.recording_health.status == "incomplete"
    assert [
        record.getMessage() for record in caplog.records if record.name == hook_module.__name__
    ] == [f"Recording closeout failed at step None (archive): TimeoutError: {problem.detail}"]


async def test_an_error_without_a_message_is_logged_and_recorded_by_its_class(
    writer_factory, caplog
):
    """The collection's own ``TimeoutError()`` — not the close-out budget — has no message."""
    writer = writer_factory(archive=[FakeStepArchiver()])
    await start_archiving(writer, ScriptedArchive(ArchiveBatch(), TimeoutError()))
    caplog.set_level("WARNING", logger=hook_module.__name__)
    await seal(writer, 0)
    await close(writer)

    (problem,) = writer.health.problems
    assert (problem.channel, problem.phase, problem.error, problem.detail) == (
        "archive",
        "closeout",
        "TimeoutError",
        "TimeoutError",
    )
    assert [
        record.getMessage() for record in caplog.records if record.name == hook_module.__name__
    ] == ["Recording closeout failed at step None (archive): TimeoutError: TimeoutError"]
    trace = trace_of(writer)
    assert trace.manifest.incomplete_reason == "archive: TimeoutError: TimeoutError"
    assert trace.result is not None


async def test_unsaved_trailing_call_is_not_recovered_by_a_different_saved_archive_segment(
    writer_factory, monkeypatch
):
    original = WALWriter.commit_trailing

    def commit(wal, *, llm_calls=(), **kwargs):
        if llm_calls:
            raise OSError("unfinished invocation segment cannot be written")
        return original(wal, llm_calls=llm_calls, **kwargs)

    monkeypatch.setattr(WALWriter, "commit_trailing", commit)
    writer = writer_factory(archive=[FakeStepArchiver()], write_retries=0)
    await start_archiving(
        writer,
        ScriptedArchive(
            ArchiveBatch(files=(archive_item(STEPS_KEY, "tail.txt", b"saved tail evidence"),))
        ),
    )
    await call(writer, "unfinished invocation", returns=completion())
    result = await close(writer, steps=0)
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.INCOMPLETE
    assert trace.trailing_calls == []
    assert trace.trailing_call_metrics == []
    (saved,) = trace.manifest.trailing
    (problem,) = trace.manifest.recording_problems
    assert problem.step is None
    assert problem.segment_id is not None
    assert problem.segment_id != saved.segment_id
    assert problem.recovered is False
    assert problem.call_ids == [f"{trace.run_id}:0:0"]
    assert len(trace.trailing_extra_files) == 1
    assert trace.read_extra(trace.trailing_extra_files[0].path) == b"saved tail evidence"
    assert result.metrics.llm_calls == 1
    assert recorded_usage(trace).calls == 0
    assert recorded_usage(trace).recording_complete is False


async def test_failed_recording_timing_append_is_retried_without_rewriting_the_step(
    writer_factory, monkeypatch
):
    original = WALWriter.commit_trailing
    failed = False

    def append(wal, *, extra_files=(), **kwargs):
        nonlocal failed
        if not failed and any(item.meta.get("kind") == "recording_timing" for item in extra_files):
            failed = True
            raise OSError("timing append failed")
        return original(wal, extra_files=extra_files, **kwargs)

    monkeypatch.setattr(WALWriter, "commit_trailing", append)
    writer = writer_factory()
    await start(writer)
    await call(writer, returns=completion())
    await seal(writer, 0)
    await call(writer, "second", returns=completion())
    await seal(writer, 1)
    await close(writer, steps=2)
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.COMPLETE
    assert len(trace.llm_messages(0)) == 1
    timing = [
        item for item in trace.trailing_extra_files if item.meta.get("kind") == "recording_timing"
    ]
    assert sorted(item.meta["step"] for item in timing) == [0, 1]


async def test_trajectory_without_metrics_or_errors_has_no_lightweight_call_ledger(writer_factory):
    writer = writer_factory(
        recording=RecordingPlan(metrics=False, errors=False, trajectory=True, observations=False)
    )
    await start(writer)
    await call(writer, returns=completion())
    await seal(writer, 0)
    await close(writer)
    trace = trace_of(writer)
    assert len(trace.llm_messages(0)) == 1
    assert trace.call_metrics(0) == []
    assert recorded_usage(trace).measured is False


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_unselected_usage_and_error_details_do_not_leak_through_trajectory(
    writer_factory, status
):
    writer = writer_factory(
        recording=RecordingPlan(metrics=False, errors=False, trajectory=True, observations=False)
    )
    await start(writer)
    error_text = "excluded execution diagnostic marker"
    if status == "completed":
        response = completion("selected model reply")
        assert await call(writer, returns=response) is response
    else:
        error = StructuredOutputValidationError(
            error_text, raw_output="excluded failure marker", usage=usage()
        )
        with pytest.raises(StructuredOutputValidationError) as caught:
            await call(writer, raises=error)
        assert caught.value is error
    record = step_record(0)
    record.error = error_text
    record.error_phase = "act"
    record.action_results[0].success = False
    record.action_results[0].error = error_text
    await writer.after_record_step(None, args=(record,), kwargs={})
    result = agent_result(
        agent_done=AgentDoneStatus.ERROR,
        agent_reported_success=None,
        error=error_text,
        metrics=AgentMetrics(steps=1),
    )
    await writer.on_run_end(result=result)
    assert result.error == record.error == record.action_results[0].error == error_text
    trace = trace_of(writer)
    (saved_call,) = trace.llm_messages(0)
    assert saved_call.status == status
    assert saved_call.failure is None
    if status == "completed":
        assert as_chat(saved_call).completion is not None
        assert required(as_chat(saved_call).completion).completion == "selected model reply"
        assert required(as_chat(saved_call).completion).usage is None
    saved_step = trace.step(0)
    assert saved_step is not None
    assert saved_step.error is None and saved_step.error_phase == ""
    assert saved_step.execution_errors == []
    assert saved_step.action_results[0].error is None
    assert saved_step.token_usage is None
    assert trace.result is not None and trace.result.error is None
    assert trace.result.metrics.usage is None
    assert trace.call_metrics(0) == []
    for file in trace.committed_files:
        assert b"excluded" not in trace.read_committed(file.path)


async def test_timed_out_finalizer_cannot_mutate_the_next_runs_result(writer_factory, monkeypatch):
    writer = writer_factory(close_timeout=0.01)
    loop = asyncio.get_running_loop()
    blocked = asyncio.Event()
    write_returned = asyncio.Event()
    release = threading.Event()
    first_finalizations: list[str] = []
    original_write = WALWriter.write_result
    original_finalize = WALWriter.finalize

    def write(wal, result):
        if wal.run_dir.name == "first":
            loop.call_soon_threadsafe(blocked.set)
            assert release.wait(3), "test did not release the old recording write"
        try:
            return original_write(wal, result)
        finally:
            if wal.run_dir.name == "first":
                loop.call_soon_threadsafe(write_returned.set)

    def finalize(wal, *args, **kwargs):
        if wal.run_dir.name == "first":
            first_finalizations.append("unexpected post-timeout finalize")
        return original_finalize(wal, *args, **kwargs)

    monkeypatch.setattr(WALWriter, "write_result", write)
    monkeypatch.setattr(WALWriter, "finalize", finalize)
    try:
        await start(writer, run_id="first")
        first_path = writer.run_dir
        for step in (0, 1):
            await call(writer, returns=completion())
            await seal(writer, step)
        first = agent_result(metrics=AgentMetrics(steps=2))
        closing = asyncio.create_task(writer.on_run_end(result=first))
        await asyncio.wait_for(blocked.wait(), timeout=1)
        await asyncio.wait_for(closing, timeout=1)
        assert first.metrics.recording_health is not None
        assert first.metrics.recording_health.status == "incomplete"

        writer.close_timeout = 1.0
        await start(writer, run_id="second")
        await call(writer, returns=completion())
        await seal(writer, 0)
        second = await close(writer)
        second_snapshot = second.model_dump()
        assert second.metrics.recording_health is not None
        assert second.metrics.recording_health.status == "complete"
        release.set()
        await asyncio.wait_for(write_returned.wait(), timeout=1)
        await asyncio.sleep(0.01)
        assert second.model_dump() == second_snapshot
        assert first_finalizations == []
        assert first_path is not None
        assert RunTrace(first_path).manifest.status is TraceStatus.WRITING
        assert trace_of(writer).manifest.status is TraceStatus.COMPLETE
    finally:
        release.set()


def test_process_exit_does_not_wait_for_a_stuck_recording_system_call(tmp_path):
    script = """
import asyncio
import sys
import threading
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.agent_result import AgentMetrics
from spectragent.schemas.step_record import StepRecord
from tests._helpers.trace_builder import agent_result

def stuck_commit(*args, **kwargs):
    threading.Event().wait(60)

async def main():
    WALWriter.commit_step = stuck_commit
    writer = TraceWriter(trace_dir=sys.argv[1], close_timeout=0.02)
    await writer.on_run_start(run_id="blocked-process")
    await writer.after_record_step(None, args=(StepRecord(step=0),), kwargs={})
    result = agent_result(metrics=AgentMetrics(steps=1))
    await writer.on_run_end(result=result)
    assert result.metrics.recording_health.status == "incomplete"
    print("bounded shutdown returned", flush=True)

asyncio.run(main())
print("asyncio.run exited", flush=True)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert "bounded shutdown returned" in completed.stdout
    assert "asyncio.run exited" in completed.stdout


async def test_blocked_background_write_and_full_queue_do_not_block_execution(
    writer_factory, monkeypatch
):
    original = WALWriter.commit_step
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def commit(wal, step, *args, **kwargs):
        if step == 0:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=3), "test never released the blocked persistence worker"
        return original(wal, step, *args, **kwargs)

    monkeypatch.setattr(WALWriter, "commit_step", commit)
    writer = writer_factory(pending_segments=1, write_retries=0)
    await start(writer)
    try:
        await call(writer, "first", returns=completion())
        await seal(writer, 0)
        await asyncio.wait_for(started.wait(), timeout=1)
        returned = completion()
        assert (
            await asyncio.wait_for(call(writer, "second", returns=returned), timeout=0.2)
            is returned
        )
        await asyncio.wait_for(seal(writer, 1), timeout=0.2)
        assert (
            await asyncio.wait_for(call(writer, "third", returns=returned), timeout=0.2) is returned
        )
        await asyncio.wait_for(seal(writer, 2), timeout=0.2)
    finally:
        release.set()
    result = await close(writer, steps=3)
    trace = trace_of(writer)
    assert trace.manifest.missing_steps == [2]
    assert trace.manifest.status is TraceStatus.INCOMPLETE
    assert result.metrics.llm_calls == 3
    assert recorded_usage(trace).calls == 2
    assert any(problem.phase == "buffer" for problem in writer.health.problems)


async def test_restored_prefix_step_does_not_fabricate_new_model_calls(writer_factory):
    writer = writer_factory()
    await start(writer)
    restored = step_record(0).model_copy(update={"origin": "restored"})
    await writer.after_record_step(None, args=(restored,), kwargs={})
    await call(writer, "new policy", returns=completion())
    await seal(writer, 1)
    result = await close(writer)
    trace = trace_of(writer)
    assert trace.call_metrics(0) == []
    assert len(trace.call_metrics(1)) == 1
    assert result.metrics.llm_calls == recorded_usage(trace).calls == 1


async def test_result_write_failure_preserves_the_live_run_result(writer_factory, monkeypatch):
    def cannot_write_result(*args, **kwargs):
        raise OSError("result storage unavailable")

    monkeypatch.setattr(WALWriter, "write_result", cannot_write_result)
    writer = writer_factory()
    await start(writer)
    await call(writer, returns=completion())
    await seal(writer, 0)
    result = await close(writer)
    assert result.metrics.recording_health is not None
    assert result.metrics.recording_health.status == "incomplete"
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.INCOMPLETE
    assert trace.result is None
    assert trace.step(0) is not None


async def test_metrics_only_keeps_actual_execution_and_model_errors(writer_factory):
    writer = writer_factory(
        recording=RecordingPlan(metrics=True, errors=True, trajectory=False, observations=False)
    )
    await start(writer)
    actual_error = OSError("model transport failed")
    with pytest.raises(OSError) as caught:
        await call(writer, raises=actual_error)
    assert caught.value is actual_error
    record = StepRecord(step=0, error_phase="llm", error="model transport failed")
    await writer.after_record_step(None, args=(record,), kwargs={})
    await close(writer)
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.COMPLETE
    with pytest.raises(RecordingScopeError):
        trace.llm_messages(0)
    assert trace.manifest.steps[0].llm_calls == []
    (fact,) = trace.call_metrics(0)
    assert (fact.status, fact.error, fact.detail) == ("failed", "OSError", "model transport failed")
    saved = trace.step(0)
    assert saved is not None
    assert (saved.error_phase, saved.error) == ("llm", "model transport failed")


@pytest.mark.parametrize("commit_kind", ["step", "timing"])
@pytest.mark.parametrize("with_archive", [False, True])
async def test_uncertain_commit_with_delayed_readback_recovers_without_duplicate_writes(
    writer_factory, monkeypatch, commit_kind, with_archive
):
    original_write = fsio.atomic_write
    original_read = Path.read_bytes
    failed = False
    refuse_readback = False

    def atomic_write(path: Path, data: bytes) -> None:
        nonlocal failed, refuse_readback
        original_write(path, data)
        if path.name != "manifest.json" or failed:
            return
        manifest = json.loads(data)
        wanted = [entry["step"] for entry in manifest["steps"]] == [0] and len(
            manifest["trailing"]
        ) == (1 if commit_kind == "timing" else 0)
        if wanted:
            failed = True
            refuse_readback = True
            raise OSError("manifest replaced before synchronization failed")

    def read(path: Path) -> bytes:
        nonlocal refuse_readback
        if path.name == "manifest.json" and refuse_readback:
            refuse_readback = False
            raise PermissionError("first manifest readback unavailable")
        return original_read(path)

    monkeypatch.setattr(fsio, "atomic_write", atomic_write)
    monkeypatch.setattr(Path, "read_bytes", read)
    writer = writer_factory(write_retries=1, archive=[FakeStepArchiver()] if with_archive else [])
    if with_archive:
        await start_archiving(
            writer,
            ScriptedArchive(
                ArchiveBatch(
                    files=(archive_item(CAPABILITY_ID, "run.json", b'{"capture": 0}', scope="run"),)
                ),
                ArchiveBatch(
                    files=(archive_item(CAPABILITY_ID, "run.json", b'{"capture": 1}', scope="run"),)
                ),
            ),
        )
    else:
        await start(writer)
    await call(writer, "first", returns=completion())
    await seal(writer, 0)
    await call(writer, "second", returns=completion())
    await seal(writer, 1)
    await close(writer, steps=2)
    trace = trace_of(writer)
    assert trace.manifest.status is TraceStatus.COMPLETE
    assert [entry.step for entry in trace.manifest.steps] == [0, 1]
    assert [len(trace.llm_messages(step)) for step in (0, 1)] == [1, 1]
    timing = [
        item for item in trace.trailing_extra_files if item.meta.get("kind") == "recording_timing"
    ]
    assert sorted(item.meta["step"] for item in timing) == [0, 1]
    if with_archive:
        latest = trace.archive_run_file(CAPABILITY_ID)
        assert latest is not None
        assert latest.run_version == 1
        assert trace.read_extra(latest.path) == b'{"capture": 1}'
