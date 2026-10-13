"""``require_eval`` — the ``trace_branches`` loader's evaluation filter.

Its own module, not the loader's: conscribe writes a ``.pyi`` stub next to the
loader (it carries an injected receptor), and a stub types a same-module
sibling as ``Any`` — so a type defined beside the loader would be ``Any`` to
every type checker that imports it. Here it is a real class to everyone.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass as pydantic_dataclass


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class RequireEval:
    """Keep a source only when an evaluator graded it ``label`` (``require_eval``).

    At least one verdict in the source's ``evals/`` (``RunTrace.evals()``)
    from ``evaluator_id``, measured (``verdict_source == "graded"`` — an
    ``evaluation_error`` or ``not_evaluated`` verdict says nothing about the
    run) and with ``predicted_label == label``; with ``slug``, only the
    verdict file of that ``verdict_slug`` counts. A pydantic dataclass, not
    a ``BaseModel``: conscribe imports it into the generated config like any
    other type (af-add-a-receptor §7).
    """

    evaluator_id: str = Field(description="The evaluator whose verdicts count.")
    label: int = Field(default=1, description="The predicted label a verdict must carry.")
    slug: str | None = Field(
        default=None,
        description="Only the verdict file of this verdict_slug counts. None (the default): any.",
    )

    def satisfied_by(self, verdicts: Mapping[str, Any]) -> bool:
        """Whether one of *verdicts* (``verdict_slug`` → ``EvalResult``) meets this."""
        return any(
            (self.slug is None or slug == self.slug)
            and verdict.evaluator_id == self.evaluator_id
            and verdict.verdict_source == "graded"
            and verdict.predicted_label == self.label
            for slug, verdict in verdicts.items()
        )


__all__ = ["RequireEval"]
