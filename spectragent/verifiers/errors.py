"""Why a verifier could not judge a trace — all loud, none silent.

The graders this package replaces degraded quietly in three separate
places: a missing screenshot directory swapped an image judge for a
weaker text-only prompt, a missing config file fell back to defaults, and
a missing prompt file raised a bare ``FileNotFoundError`` naming a path
that no longer existed. A verdict produced by a different prompt than the
one you asked for is worse than no verdict, so every one of those paths
raises here.

A failure to judge is never a failed task: offline, nothing is written and
the trace can be judged again. What the attempt left behind — the prompts it
sent, the reply it got, what its judge model spent — rides on the error
(:attr:`VerificationError.evidence`, :attr:`VerificationError.usage`), so the
report of the pass keeps it.

The missing-prompt case is the exception: it lives on
:class:`spectragent.prompts.PromptNotFoundError`, because agents load
from the same tree and one condition should not have two exception
classes depending on who happened to ask.
"""

from __future__ import annotations

from pydantic import JsonValue

from spectragent.schemas.verdict import JudgeUsage


class VerificationError(RuntimeError):
    """A verifier could not judge a trace.

    Attributes:
        evidence: What the attempt left: prompts, partial replies.
        usage: What its judge model spent before it failed, as reported.
    """

    def __init__(
        self,
        message: str,
        *,
        evidence: dict[str, JsonValue] | None = None,
        usage: JudgeUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.evidence = evidence
        self.usage = usage


class TraceIncompleteError(VerificationError):
    """The trace lacks something the verifier needs to judge it.

    Distinct from "the agent failed": a run that errored out is still
    judged (and fails). This is for a trace that cannot be *read* — no
    ``result.json``, no recorded task text, and so on.
    """


class ReferenceMismatchError(VerificationError):
    """The reference data does not carry what this verifier requires."""


class NoReferenceError(VerificationError):
    """The task the trace ran has no benchmark reference, and the verifier judges against one.

    A task-variant branch (``BranchProvenance.task_variant``,
    ``docs/architecture_design.md`` §4.10) ran a task nobody wrote a
    reference for: its source task's reference describes another task. The
    trace cannot be judged by such a verifier — never a judged wrong answer.
    """
