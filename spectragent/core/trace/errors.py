"""Trace reading failures.

:class:`RunTrace` hands back typed models, so it needs a way to say
"these bytes are not that model". A raw pydantic ``ValidationError`` is
the wrong way: on a trace written before the current schema it names
whichever renamed field pydantic tripped over first — ``agent_success``,
for the 2.0 -> 2.1 rename — and never mentions that the file is simply
too old to read.
"""

from __future__ import annotations

from pathlib import Path


class TraceSchemaError(RuntimeError):
    """A trace file exists — or is committed — but cannot be read as what it should be.

    Raised, naming the file (:attr:`path`), for every way a committed
    trace can be unreadable (``docs/architecture_design.md`` §5.5):

    - corrupt JSON, bytes that are not UTF-8, an empty file — or JSON
      nested past what the interpreter can parse, far deeper than the
      writer ever emits;
    - a schema mismatch, including an ``AgentResult.schema_version`` this
      build does not read;
    - a file the manifest lists that is missing on disk — and a missing
      ``manifest.json`` or ``config.json``;
    - bytes that disagree with their index: an empty artifact, an extra
      file or output schema whose size or sha256 does not match;
    - a path the manifest or an extra index lists that leaves the run
      directory — absolute, with a ``..`` segment, or through a symlink —
      or a ``manifest.json`` / ``config.json`` / ``result.json`` that is a
      link out of it. :attr:`path` is then the file that listed it; the
      file it points at is never opened;
    - a fragment key no registered class claims, or a payload filed under
      the wrong key;
    - a ``format_version`` this reader cannot serve.

    Deliberately distinct from both neighbours it used to be confused
    with. "The file is absent" is reported as ``None`` — that is a run
    which died before it recorded anything, not a broken trace. "The
    grader failed" is an ``EvaluationError`` — that says nothing about
    what is on disk. This one means the trace itself is unreadable, and
    re-running the evaluation pass will never fix it.
    """

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message)
        #: The file that could not be read, when there is one.
        self.path = path
