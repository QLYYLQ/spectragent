"""``require_eval`` — the verdict filter of the ``trace_branches`` loader and of training export.

Shared by branch selection and training export; both consumers use the same
validated settings and verdict-matching semantics, over the run's verdict
store (``RunTrace.verdicts()``, ``docs/architecture_design.md`` §5.3
"判定统一存放").
"""

from collections.abc import Mapping

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from spectragent.schemas.verdict import Verdict


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class RequireEval:
    """Keep a source only when a verifier judged it ``label`` (``require_eval``).

    At least one verdict in the source's store (``RunTrace.verdicts()``) from
    ``verifier_id``, judged (a ``not_judged`` verdict says nothing about the
    run) and labelled ``label``; with ``slug``, only the verdict of that slug
    counts. A pydantic dataclass, not a ``BaseModel``; conscribe imports the
    original type into generated config.
    """

    verifier_id: str = Field(description="The verifier kind whose verdicts count.")
    label: int = Field(default=1, description="The label a verdict must carry: 1 passed, 0 failed.")
    slug: str | None = Field(
        default=None,
        description="Only the verdict of this slug counts. None (the default): any.",
    )

    def satisfied_by(self, verdicts: Mapping[str, Verdict]) -> bool:
        """Whether one of *verdicts* (slug → verdict) meets this."""
        return any(
            (self.slug is None or slug == self.slug)
            and verdict.verifier.verifier_id == self.verifier_id
            and verdict.status == "judged"
            and verdict.label == self.label
            for slug, verdict in verdicts.items()
        )


__all__ = ["RequireEval"]
