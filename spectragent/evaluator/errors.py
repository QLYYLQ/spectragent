"""Evaluator failures — all loud, none silent.

The graders this package replaces degraded quietly in three separate
places: a missing screenshot directory swapped an image judge for a
weaker text-only prompt, a missing config file fell back to defaults, and
a missing prompt file raised a bare ``FileNotFoundError`` naming a path
that no longer existed. A verdict produced by a different prompt than the
one you asked for is worse than no verdict, so every one of those paths
raises here.

The missing-prompt case is the exception: it lives on
:class:`spectragent.prompts.PromptNotFoundError`, because agents load
from the same tree and one condition should not have two exception
classes depending on who happened to ask.
"""

from __future__ import annotations


class EvaluationError(RuntimeError):
    """Base class for evaluator failures."""


class TraceIncompleteError(EvaluationError):
    """The trace lacks something the evaluator needs to score it.

    Distinct from "the agent failed": a run that errored out is still
    scorable (and scores 0). This is for a trace that cannot be *read* —
    no ``result.json``, no recorded task text, and so on.
    """


class ReferenceMismatchError(EvaluationError):
    """The reference data does not carry what this evaluator requires."""
