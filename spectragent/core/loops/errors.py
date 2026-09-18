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

See docs/execution_dag.md M09 "ReactLoop" for the control flow.
"""

from __future__ import annotations


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
    ``AgentRuntime.run`` catches ``Exception`` to close the trace out —
    ``on_run_end`` with a terminal result, then ``on_run_error`` with the
    partial steps — and *then* re-raises when ``raise_on_error`` is set.
    An abort must not skip that: the evidence the guard fired on is the
    most valuable thing in the trace. Deriving from ``BaseException``
    would bypass the WAL commit entirely.

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
