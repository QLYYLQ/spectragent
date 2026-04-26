"""Loop-level error types.

``ParseError`` is raised by ``Agent.parse_react_output`` when the LLM
output is structurally unparseable. ReactLoop catches it and treats it
as a framework-level control signal — *not* as an action dispatch —
feeding the error detail back into the next observation so the LLM can
self-correct. Repeated consecutive parse failures terminate the run.

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
