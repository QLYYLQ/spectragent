"""Offline usage from the lightweight call ledger, independently of trajectory artifacts.

The provider's reported subtotal, missing provider usage, and recording
coverage are separate facts. A plan that did not select metrics returns
``None`` for counts; a fully recorded run with no calls returns zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from spectragent.core.trace.models import RecordedCallMetrics, RecordedLLMCall, TraceStatus
from spectragent.schemas.step_record import TokenUsage

if TYPE_CHECKING:
    from spectragent.core.trace.reader import RunTrace


class CallWithoutUsage(BaseModel):
    """A recorded call whose provider reported no usage: counted as a call, adding no tokens."""

    model_config = ConfigDict(frozen=True)

    #: The step the Loop was on (``RecordedLLMCall.step``); for a trailing call, the step it
    #: never surrendered.
    step: int
    #: Start order within the step, from 0.
    call: int
    status: Literal["completed", "failed", "cancelled"]
    #: A call of a step the Loop never surrendered (``RunTrace.trailing_calls``).
    trailing: bool = False

    def describe(self) -> str:
        """``step S call K (status)``; a trailing call says so."""
        where = f"step {self.step} call {self.call}" + (", trailing" if self.trailing else "")
        return f"{where} ({self.status})"


class RunUsage(BaseModel):
    """The tokens one run's recorded calls reported (:func:`recorded_usage`)."""

    model_config = ConfigDict(frozen=True)

    #: The tokens of every call whose provider reported usage.
    tokens: int | None = 0
    #: Known token subtotal, retaining provider cache and image counts.
    usage: TokenUsage | None = Field(default_factory=TokenUsage)
    #: Every recorded call, the calls without usage included.
    calls: int | None = 0
    #: The calls whose provider reported no usage, in the order they were recorded.
    without_usage: tuple[CallWithoutUsage, ...] = ()
    #: Whether the recording plan selected usage measurement.
    measured: bool = True
    #: All expected recording content committed and the trace closed.
    recording_complete: bool = True
    #: The subtotal covers the whole run and every call reported usage.
    complete: bool = True
    #: Missing surrendered steps; saved steps after these gaps still contribute.
    missing_steps: tuple[int, ...] = ()


def recorded_calls(trace: RunTrace) -> list[RecordedLLMCall]:
    """Every model call *trace* recorded: each committed step's, then the trailing ones.

    Each prompt holds its inline images again, as ``RunTrace.llm_messages``
    hands them back.

    Raises:
        TraceSchemaError: A call record, or an image it refers to, does not
            read back.
    """
    return _committed_calls(trace) + list(trace.trailing_calls)


def recorded_usage(
    trace: RunTrace, *, caller: str | None = None, purpose: str | None = None
) -> RunUsage:
    """Read usage facts only; optional attribution filters keep auxiliary calls distinguishable.

    No prompt, image, or step record is opened. Recording gaps retain the
    known subtotal while ``complete`` reports the absent coverage.
    """
    manifest = trace.manifest
    missing = tuple(manifest.missing_steps)
    recording_complete = manifest.status == TraceStatus.COMPLETE and not missing
    if not trace.config.recording.metrics:
        return RunUsage(
            tokens=None,
            usage=None,
            calls=None,
            measured=False,
            complete=False,
            recording_complete=recording_complete,
            missing_steps=missing,
        )
    count = 0
    reported: list[TokenUsage] = []
    without: list[CallWithoutUsage] = []
    committed = [call for entry in manifest.steps for call in trace.call_metrics(entry.step)]
    trailing = trace.trailing_call_metrics
    for calls, is_trailing in ((committed, False), (trailing, True)):
        for call in calls:
            if caller is not None and call.caller != caller:
                continue
            if purpose is not None and call.purpose != purpose:
                continue
            count += 1
            usage = TokenUsage.from_llm_usage(call.usage)
            if usage is None:
                without.append(
                    CallWithoutUsage(
                        step=call.step, call=call.call, status=call.status, trailing=is_trailing
                    )
                )
            else:
                reported.append(usage)
    subtotal = TokenUsage.combine(reported)
    return RunUsage(
        tokens=subtotal.total_tokens,
        usage=subtotal,
        calls=count,
        without_usage=tuple(without),
        recording_complete=recording_complete,
        complete=recording_complete and not without,
        missing_steps=missing,
    )


def reported_usage(call: RecordedLLMCall | RecordedCallMetrics) -> TokenUsage | None:
    """What *call*'s provider reported, by the step record's rule; ``None`` when it reported nothing.

    A completed call's ``completion.usage``, a failed or cancelled call's
    ``failure.usage``.
    """
    if isinstance(call, RecordedCallMetrics):
        return TokenUsage.from_llm_usage(call.usage)
    if call.completion is not None:
        usage = call.completion.usage
    elif call.failure is not None:
        usage = call.failure.usage
    else:
        usage = None
    return TokenUsage.from_llm_usage(usage)


def _committed_calls(trace: RunTrace) -> list[RecordedLLMCall]:
    return [call for entry in trace.manifest.steps for call in trace.llm_messages(entry.step)]


__all__ = [
    "CallWithoutUsage",
    "RunUsage",
    "recorded_calls",
    "recorded_usage",
    "reported_usage",
]
