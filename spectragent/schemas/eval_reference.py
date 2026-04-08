"""Evaluation reference data schema — the eval-side view of benchmark data."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from spectragent.schemas.task import TaskReference


class EvalReference(BaseModel):
    """Evaluation reference data — returned by Eval DataLoaders.

    This is the "eval view" of benchmark data: it contains only the fields
    needed for evaluation (expected answers, rubrics, scoring criteria).
    The complementary "task view" is ``TaskDefinition``.
    """

    model_config = ConfigDict(extra="allow")

    # Identity
    task_id: str
    benchmark: str

    # Evaluation data
    expected_answer: str | None = None
    reference: TaskReference | None = None  # steps, key_points, scoring, encrypted

    # Classification
    difficulty: str | None = None
    tags: list[str] = Field(default_factory=list)

    # Catch-all
    metadata: dict[str, Any] = Field(default_factory=dict)
