"""Loop-level error types.

``ParseError`` is raised by ``Agent.parse_react_output`` when the LLM
output is structurally unparseable. ReactLoop catches it and treats it
as a framework-level control signal — *not* as an action dispatch —
feeding the error detail back into the next observation so the LLM can
self-correct. Repeated consecutive parse failures terminate the run.

``RunAbort`` is the opposite signal: it is the only exception a Loop is
forbidden to absorb. Everything else a Loop catches is "this attempt
failed, here is a terminal ``AgentResult`` saying so"; ``RunAbort``
means the decision to stop was already made by somebody outside the
Loop, and the Loop's job is to get out of the way.

``RunEnded`` is never raised. It is how a Loop whose recording of the
step its run ended on was cut off by a cancellation hands the outcome it
had already reached to ``AgentRuntime.execute`` without swallowing the
cancellation (:func:`carry_outcome`, :func:`carried_outcome`).

``UnrepresentableActionError`` is ``ParseError``'s dual: raised by the
optional ``Agent.format_react_output`` slot when an action, or one of its
parameters, has no written form in the agent's reply format. The training
export (arch §4.15) treats it as the untranslatable reason
``unrepresentable``; it never reaches a Loop.

See docs/execution_dag.md M09 "ReactLoop" for the control flow.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spectragent.schemas.agent_result import AgentResult


class ParseError(Exception):
    """LLM output could not be parsed into actions.

    Raised by ``Agent.parse_react_output``. The Loop catches this and:
    1. Logs a warning.
    2. Feeds ``detail`` back into the next prompt as error feedback.
    3. Terminates after ``max_consecutive_parse_errors`` consecutive
       failures.

    Attributes:
        detail: Human-readable explanation of what went wrong.
        raw_output: Optional truncated copy of the unparseable LLM text.
    """

    def __init__(self, detail: str, *, raw_output: str | None = None) -> None:
        self.detail = detail
        self.raw_output = raw_output
        super().__init__(detail)


class UnrepresentableActionError(Exception):
    """An action the agent's reply format cannot write — the dual of :class:`ParseError`.

    Raised by ``Agent.format_react_output`` (an optional ReAct slot, arch
    §4.1) instead of silently dropping what it cannot write: a verb the
    format has no word for (or that this run did not negotiate), or a
    parameter set to a value the format cannot carry — UI-TARS has no way to
    say ``type(clear=False)`` or ``scroll(amount=300)``. A formatter that
    dropped such a parameter would teach a student a reply that executes
    differently from the action it was trained on.

    Attributes:
        action_name: The action as it was handed in (fully qualified).
        params: The parameters that cannot be written, in the action's order;
            empty when the action itself has no written form.
        detail: Human-readable explanation.
    """

    def __init__(self, action_name: str, params: Sequence[str], detail: str) -> None:
        self.action_name = action_name
        self.params = tuple(params)
        self.detail = detail
        what = f"{action_name}({', '.join(self.params)})" if self.params else action_name
        super().__init__(f"cannot write {what}: {detail}")


class RunAbort(Exception):
    """Something outside the Loop has decided this run must stop now.

    A Loop turns failures into results: a dead browser, a 429, an action
    that raised — each of those produces a terminal ``AgentResult`` and
    the caller reads ``agent_done`` / ``env_status`` to find out what
    happened. That is the right shape for a failure, and the wrong shape
    for a *veto*. A safety guard woven onto ``act`` (or ``observe``, or
    ``ainvoke``) that detects a risk-control page is not reporting that
    the click failed — it is refusing to let the run continue at all, and
    it needs that refusal to reach the process that started the run.

    Catching ``Exception`` around those calls made the two
    indistinguishable, so the guard's veto came back as an ordinary
    ``agent_done="error"`` result and the caller went on to the next task.
    ``RunAbort`` is the escape hatch: every ``except Exception`` in a Loop
    must let it through.

    It derives from ``Exception``, **not** ``BaseException``, on purpose.
    ``AgentRuntime.execute`` catches ``Exception`` to close the trace out
    — ``on_run_end`` with a terminal result, then ``on_run_error`` with
    the partial steps — and *then* re-raises a ``RunAbort`` whatever
    ``raise_on_error`` says, even when that close-out failed (the failure
    becomes a note on it), and even when the run's own timeout expired
    while the Loop was still recording the vetoed step (:class:`RunEnded`;
    the timeout becomes a note on it). An abort must not skip the
    close-out: the evidence the guard fired on is the most valuable thing
    in the trace. Deriving from ``BaseException`` would bypass the WAL
    commit entirely. Only a cancellation from outside the run outranks it.

    Raise it from a hook handler that is ``blocking=True``. A
    non-blocking handler runs after the call it fired on has already
    returned, and ``core/aop/proxy.py`` logs-and-drops its exceptions
    because there is nothing left to propagate to.

    Attributes:
        reason: Human-readable explanation, carried into the trace.
        source: Who vetoed — a hook id, a guard name. Free-form; it exists
            so a trace says *which* guard stopped the run, not just that
            one did.
    """

    def __init__(self, reason: str, *, source: str = "") -> None:
        self.reason = reason
        self.source = source
        super().__init__(f"{source}: {reason}" if source else reason)


class RunEnded(Exception):
    """The outcome a run had already reached when a cancellation cut its recording off.

    Never raised. A Loop hands over the step its run ends on *before* the
    outcome leaves it — the step a guard vetoed, or the one an
    infrastructure failure, a failed action or the last tolerated parse
    error ended the run on — because that step's evidence is what explains
    the ending. The hand-over can take a while: a blocking recorder pulls
    the environment's evidence then. A cancellation that lands in it is not
    the Loop's to swallow — it may be Ctrl-C, or a caller's own
    ``asyncio.timeout`` — so the Loop re-raises it unchanged, with the
    outcome put in its ``__context__`` chain (:func:`carry_outcome`).

    ``AgentRuntime.execute`` looks for it only when the cancellation is the
    run's *own* timeout (:func:`carried_outcome`). The run then ends the way
    the Loop had decided: a veto is closed out and re-raised whatever
    ``raise_on_error`` says, a terminal result is closed out and returned.
    An ordinary timeout result there is how such a veto got lost — returned,
    recorded by a sweep as one more timed-out task, and followed by the next
    task against the site the guard had just refused — and how a dead
    browser got charged to a slow model. A cancellation from outside still
    wins: the outcome only rides along in its traceback.

    Attributes:
        outcome: The ``RunAbort`` the Loop was re-raising, or the terminal
            ``AgentResult`` it was about to return.
    """

    def __init__(self, outcome: RunAbort | AgentResult) -> None:
        self.outcome = outcome
        if isinstance(outcome, BaseException):
            what = f"{type(outcome).__name__}: {outcome}"
        else:
            what = str(getattr(outcome, "error", None) or "a terminal result")
        super().__init__(f"the run had already ended: {what}")


def carry_outcome(cancelled: BaseException, outcome: RunAbort | AgentResult) -> None:
    """Let *cancelled* carry the outcome its run had reached; the cancellation stays as it is.

    For a Loop whose recording of the step its run ended on was cut off:
    call it in the ``except asyncio.CancelledError`` handler, then re-raise
    the same object. A :class:`RunEnded` holding *outcome* goes to the head
    of the cancellation's ``__context__`` chain and whatever context it had
    moves behind it, so nothing in the chain is lost. A veto already in the
    chain — the cancellation landed while an ``except RunAbort`` block was
    awaiting — is not carried twice.
    """
    if carried_outcome(cancelled) is outcome:
        return
    carrier = RunEnded(outcome)
    carrier.__context__ = cancelled.__context__
    cancelled.__context__ = carrier


def carried_outcome(cancelled: BaseException) -> RunAbort | AgentResult | None:
    """The outcome *cancelled* carries, when its run had already reached one.

    The first :class:`RunEnded`'s outcome or the first ``RunAbort`` along
    the ``__context__`` chain, whichever comes first. A bare ``RunAbort``
    counts: a cancellation that lands while a veto is being handled carries
    it as context without any Loop's help, and that veto is in flight all
    the same. Only ``__context__`` — "was being handled when this was
    raised" — is followed, never ``__cause__``: an error raised *from* a
    ``RunAbort`` is a different failure naming it, not a veto in flight.
    """
    seen: set[int] = set()
    link = cancelled.__context__
    while link is not None and id(link) not in seen:
        if isinstance(link, RunEnded):
            return link.outcome
        if isinstance(link, RunAbort):
            return link
        seen.add(id(link))
        link = link.__context__
    return None
