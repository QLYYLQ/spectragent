"""What a run's call records say it used (``core/trace/usage.py``; arch §4.2, §4.16 "token 上限").

Every trace here is committed by the real WAL from real call records
(``trace_builder.py``), so the usage read back is the usage a provider's
reply or exception carried into ``TraceWriter``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spectragent.core.llm.views import ChatInvokeCompletion, ModelInvokeUsage
from spectragent.core.trace.encoding import (
    EncodedLLMCall,
    PendingLLMCall,
    encode_call_metrics,
    encode_llm_call,
)
from spectragent.core.trace.errors import RecordingScopeError, TraceSchemaError
from spectragent.core.trace.models import TraceConfig, TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.usage import (
    CallWithoutUsage,
    RunUsage,
    recorded_calls,
    recorded_usage,
)
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.step_record import StepRecord, TokenUsage
from tests._helpers.trace_builder import (
    build_reference_trace,
    cancelled_call,
    new_wal,
    prompt,
    step_record,
)


def _call(step: int, index: int, usage: ModelInvokeUsage | None) -> EncodedLLMCall:
    pending = PendingLLMCall(prompt("go"))
    pending.complete(ChatInvokeCompletion(completion="done()", usage=usage))
    return encode_llm_call(step, index, pending)


def _failed(step: int, index: int, exc: Exception) -> EncodedLLMCall:
    pending = PendingLLMCall(prompt("go"))
    pending.fail(exc)
    return encode_llm_call(step, index, pending)


def _usage(prompt_tokens: int, completion: int, total: int) -> ModelInvokeUsage:
    return ModelInvokeUsage(
        prompt_tokens=prompt_tokens,
        prompt_cached_tokens=None,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=completion,
        total_tokens=total,
    )


class _Billed(Exception):
    """A failed call that was billed all the same: the exception carries the usage."""

    def __init__(self, usage: ModelInvokeUsage) -> None:
        super().__init__("the reply did not validate")
        self.usage = usage


def test_the_reference_trace_adds_every_reported_call(tmp_path: Path) -> None:
    trace = RunTrace(build_reference_trace(tmp_path).run_dir)
    usage = recorded_usage(trace)
    # Step 0: a refused structured reply (billed: the failure carries 15) and a completed call
    # (15); step 1: one completed call (15); the trailing call was cancelled and reported nothing.
    assert (usage.tokens, usage.calls) == (45, 4)
    assert usage.without_usage == (
        CallWithoutUsage(step=2, call=0, status="cancelled", trailing=True),
    )
    assert usage.without_usage[0].describe() == "step 2 call 0, trailing (cancelled)"
    assert [(call.step, call.call) for call in recorded_calls(trace)] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (2, 0),
    ]


def test_the_step_record_rule_and_the_calls_that_reported_nothing(tmp_path: Path) -> None:
    wal = new_wal(tmp_path)
    wal.commit_step(
        0,
        step_record(0),
        llm_calls=[
            _call(0, 0, _usage(7, 3, 12)),  # the provider's total wins: 12
            _call(0, 1, _usage(7, 3, 0)),  # an explicit provider zero stays zero
            _call(0, 2, None),  # a provider that reports no usage
        ],
    )
    wal.commit_step(
        1,
        step_record(1),
        llm_calls=[
            _failed(1, 0, _Billed(_usage(4, 0, 4))),
            _failed(1, 1, ConnectionError("the socket died")),
        ],
    )
    wal.commit_trailing(llm_calls=[cancelled_call(2, 0, "cut off")])
    wal.finalize(TraceStatus.COMPLETE)

    usage = recorded_usage(RunTrace(wal.run_dir))

    assert usage == RunUsage(
        tokens=12 + 0 + 4,
        calls=6,
        usage=TokenUsage(prompt_tokens=18, completion_tokens=6, total_tokens=16),
        complete=False,
        without_usage=(
            CallWithoutUsage(step=0, call=2, status="completed"),
            CallWithoutUsage(step=1, call=1, status="failed"),
            CallWithoutUsage(step=2, call=0, status="cancelled", trailing=True),
        ),
    )
    assert [call.describe() for call in usage.without_usage] == [
        "step 0 call 2 (completed)",
        "step 1 call 1 (failed)",
        "step 2 call 0, trailing (cancelled)",
    ]


def test_a_run_without_calls_used_nothing(tmp_path: Path) -> None:
    wal = new_wal(tmp_path)
    wal.commit_step(0, step_record(0))
    wal.finalize(TraceStatus.COMPLETE)
    assert recorded_usage(RunTrace(wal.run_dir)) == RunUsage()


def test_a_call_record_that_does_not_read_back_is_named(tmp_path: Path) -> None:
    wal = new_wal(tmp_path)
    wal.commit_step(0, step_record(0), llm_calls=[_call(0, 0, _usage(1, 1, 2))])
    wal.finalize(TraceStatus.COMPLETE)
    trace = RunTrace(wal.run_dir)
    (record,) = trace.manifest.steps[0].call_metrics
    (wal.run_dir / record).write_text("{not json")
    with pytest.raises(TraceSchemaError, match="call"):
        recorded_usage(RunTrace(wal.run_dir))


def test_light_metrics_are_readable_without_trajectory_or_observations(tmp_path: Path) -> None:
    wal = WALWriter(tmp_path, "light")
    wal.init_run(TraceConfig(recording=RecordingPlan(trajectory=False, observations=False)))
    pending = PendingLLMCall(object(), capture_trajectory=False)
    pending.complete(ChatInvokeCompletion(completion="unused", usage=_usage(3, 4, 7)))
    wal.commit_step(0, StepRecord(step=0), call_metrics=[encode_call_metrics(0, 0, pending)])
    wal.finalize(TraceStatus.COMPLETE)
    trace = RunTrace(wal.run_dir)
    assert recorded_usage(trace).tokens == 7
    provider_usage = trace.call_metrics(0)[0].usage
    assert provider_usage is not None and provider_usage.total_tokens == 7
    assert not any(
        file.kind in {"llm_call", "fragment", "prompt_image"} for file in trace.committed_files
    )
    with pytest.raises(RecordingScopeError, match="did not select"):
        trace.llm_messages(0)
    with pytest.raises(RecordingScopeError, match="observations"):
        trace.screenshot(0)


def test_usage_does_not_restore_unreadable_prompts_or_images(tmp_path: Path) -> None:
    fixture = build_reference_trace(tmp_path)
    trace = RunTrace(fixture.run_dir)
    for entry in trace.manifest.steps:
        for path in entry.llm_calls:
            (fixture.run_dir / path).write_text("corrupt trajectory")
    for path in trace.manifest.prompt_images:
        (fixture.run_dir / path).unlink()
    assert recorded_usage(trace).tokens == 45
    with pytest.raises(TraceSchemaError):
        recorded_calls(trace)


def test_unmeasured_usage_is_distinct_from_no_calls_and_reported_zero(tmp_path: Path) -> None:
    unmeasured = WALWriter(tmp_path, "unmeasured")
    unmeasured.init_run(TraceConfig(recording=RecordingPlan(metrics=False)))
    unmeasured.finalize(TraceStatus.COMPLETE)
    usage = recorded_usage(RunTrace(unmeasured.run_dir))
    assert usage.tokens is None and usage.calls is None and usage.usage is None
    assert not usage.measured and not usage.complete
    zero = new_wal(tmp_path, "zero")
    zero.commit_step(0, step_record(0), llm_calls=[_call(0, 0, _usage(0, 0, 0))])
    zero.finalize(TraceStatus.COMPLETE)
    usage = recorded_usage(RunTrace(zero.run_dir))
    assert (usage.tokens, usage.calls) == (0, 1)
    assert usage.measured and usage.complete and not usage.without_usage


def test_saved_steps_after_a_gap_still_contribute_to_known_usage(tmp_path: Path) -> None:
    wal = new_wal(tmp_path)
    wal.commit_step(0, step_record(0), llm_calls=[_call(0, 0, _usage(1, 1, 2))])
    wal.commit_step(2, step_record(2), llm_calls=[_call(2, 0, _usage(3, 4, 7))])
    wal.finalize(TraceStatus.COMPLETE)
    usage = recorded_usage(RunTrace(wal.run_dir))
    assert (usage.tokens, usage.calls) == (9, 2)
    assert usage.missing_steps == (1,)
    assert not usage.recording_complete and not usage.complete


def test_auxiliary_calls_and_cache_counts_remain_attributed(tmp_path: Path) -> None:
    wal = new_wal(tmp_path)
    pending = PendingLLMCall(
        None, capture_trajectory=False, caller="prompt_extension", purpose="guidance"
    )
    provider = _usage(10, 2, 12).model_copy(
        update={"prompt_cached_tokens": 6, "prompt_cache_creation_tokens": 2}
    )
    pending.fail(_Billed(provider))
    wal.commit_step(0, step_record(0), call_metrics=[encode_call_metrics(0, 0, pending)])
    wal.finalize(TraceStatus.COMPLETE)
    usage = recorded_usage(RunTrace(wal.run_dir), caller="prompt_extension", purpose="guidance")
    assert usage.calls == 1 and usage.tokens == 12 and usage.complete
    assert usage.usage is not None
    assert usage.usage.prompt_cached_tokens == 6
    assert usage.usage.prompt_cache_creation_tokens == 2
    assert recorded_usage(RunTrace(wal.run_dir), caller="agent").calls == 0


def test_all_missing_provider_usage_is_an_incomplete_known_subtotal(tmp_path: Path) -> None:
    wal = new_wal(tmp_path)
    wal.commit_step(0, step_record(0), llm_calls=[_call(0, 0, None)])
    wal.finalize(TraceStatus.COMPLETE)
    measured = recorded_usage(RunTrace(wal.run_dir))
    assert measured.measured and measured.recording_complete
    assert (measured.tokens, measured.calls) == (0, 1)
    assert not measured.complete and len(measured.without_usage) == 1
