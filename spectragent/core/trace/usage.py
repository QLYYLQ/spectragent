"""The model calls a run recorded and the tokens they reported, read back from its trace.

``docs/architecture_design.md`` §4.2 ("sweep 的 token 上限") and §4.16 ("token 上限").

**Which calls.** :func:`recorded_calls` is every call a run recorded: each
committed step's, in manifest order and start order within the step, then the
trailing calls of a step the Loop never surrendered. It is what the run sent
its model (the flywheel's privacy measure searches these requests) and what
the run was billed for.

**How many tokens.** Each call record carries the usage its provider
reported: a completed call in ``completion.usage``, a failed one in
``failure.usage`` (a failed call is billed all the same, §5.4).
:func:`recorded_usage` adds them up by the rule a step's ``TokenUsage`` uses
(``TokenUsage.from_llm_usage``: the provider's ``total_tokens``, or prompt plus
completion when it reported no total). A call whose provider reported no usage
(a cancelled call, a call that failed before any reply, a provider that never
reports usage) adds no tokens and is listed (:class:`CallWithoutUsage`), never
folded into the total as a silent zero.

Only ``RunTrace``'s accessors are read, so nothing here depends on a metrics
hook: ``result.json``'s ``metrics.usage`` exists only when one is configured.
``spectragent-bench``'s token limit, the flywheel chain's and the flywheel
report's ``token_use`` all count through :func:`recorded_usage`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from spectragent.core.trace.models import RecordedLLMCall
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
    tokens: int = 0
    #: Every recorded call, the calls without usage included.
    calls: int = 0
    #: The calls whose provider reported no usage, in the order they were recorded.
    without_usage: tuple[CallWithoutUsage, ...] = ()


def recorded_calls(trace: RunTrace) -> list[RecordedLLMCall]:
    """Every model call *trace* recorded: each committed step's, then the trailing ones.

    Each prompt holds its inline images again, as ``RunTrace.llm_messages``
    hands them back.

    Raises:
        TraceSchemaError: A call record, or an image it refers to, does not
            read back.
    """
    return _committed_calls(trace) + list(trace.trailing_calls)


def recorded_usage(trace: RunTrace) -> RunUsage:
    """The tokens *trace*'s calls reported, and every call that reported none (module docstring).

    Raises:
        TraceSchemaError: A call record, or an image it refers to, does not
            read back.
    """
    tokens = 0
    without: list[CallWithoutUsage] = []
    committed = _committed_calls(trace)
    trailing = list(trace.trailing_calls)
    for calls, is_trailing in ((committed, False), (trailing, True)):
        for call in calls:
            usage = reported_usage(call)
            if usage is None:
                without.append(
                    CallWithoutUsage(
                        step=call.step, call=call.call, status=call.status, trailing=is_trailing
                    )
                )
            else:
                tokens += usage.total_tokens
    return RunUsage(
        tokens=tokens, calls=len(committed) + len(trailing), without_usage=tuple(without)
    )


def reported_usage(call: RecordedLLMCall) -> TokenUsage | None:
    """What *call*'s provider reported, by the step record's rule; ``None`` when it reported nothing.

    A completed call's ``completion.usage``, a failed or cancelled call's
    ``failure.usage``.
    """
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
