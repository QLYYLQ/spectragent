"""Trace reading failures.

:class:`RunTrace` hands back typed models, so it needs a way to say
"these bytes are not that model". A raw pydantic ``ValidationError`` is
the wrong way: on a trace written before the current schema it names
whichever renamed field pydantic tripped over first — ``agent_success``,
for the 2.0 -> 2.1 rename — and never mentions that the file is simply
too old to read.
"""

from __future__ import annotations


class TraceSchemaError(RuntimeError):
    """A trace file exists but does not parse as the current schema.

    Deliberately distinct from both neighbours it used to be confused
    with. "The file is absent" is reported as ``None`` — that is a run
    which died before it recorded anything, not a broken trace. "The
    grader failed" is an ``EvaluationError`` — that says nothing about
    what is on disk. This one means the trace itself is unreadable, and
    re-running the evaluation pass will never fix it.
    """
