"""What a run's call records say it used (``core/trace/usage.py``; arch §4.2, §4.16 "token 上限").

Every trace here is committed by the real WAL from real call records
(``trace_builder.py``), so the usage read back is the usage a provider's
reply or exception carried into ``TraceWriter``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage
from spectragent.core.trace.encoding import EncodedLLMCall, PendingLLMCall, encode_llm_call
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import TraceStatus
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.usage import (
    CallWithoutUsage,
    RunUsage,
    recorded_calls,
    recorded_usage,
)
from tests._helpers.trace_builder import (
    build_reference_trace,
    cancelled_call,
    new_wal,
    prompt,
    step_record,
)


def _call(step: int, index: int, usage: ChatInvokeUsage | None) -> EncodedLLMCall:
    pending = PendingLLMCall(prompt("go"))
    pending.complete(ChatInvokeCompletion(completion="done()", usage=usage))
    return encode_llm_call(step, index, pending)


def _failed(step: int, index: int, exc: Exception) -> EncodedLLMCall:
    pending = PendingLLMCall(prompt("go"))
    pending.fail(exc)
    return encode_llm_call(step, index, pending)


def _usage(prompt_tokens: int, completion: int, total: int) -> ChatInvokeUsage:
    return ChatInvokeUsage(
        prompt_tokens=prompt_tokens,
        prompt_cached_tokens=None,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=completion,
        total_tokens=total,
    )


class _Billed(Exception):
    """A failed call that was billed all the same: the exception carries the usage."""

    def __init__(self, usage: ChatInvokeUsage) -> None:
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
            _call(0, 1, _usage(7, 3, 0)),  # no total: prompt plus completion, 10
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
        tokens=12 + 10 + 4,
        calls=6,
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
    (record,) = trace.manifest.steps[0].llm_calls
    (wal.run_dir / record).write_text("{not json")
    with pytest.raises(TraceSchemaError, match="call"):
        recorded_usage(RunTrace(wal.run_dir))
