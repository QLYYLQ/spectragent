"""The ``task_variants`` file of the ``trace_branches`` loader (``docs/architecture_design.md`` §4.10).

A task variant pairs a recorded checkpoint with a different task that starts
from the same screen ("same page, different need"); something else, usually
a language model, writes them. The file is JSON::

    {"format": "task-variants-1",
     "variants": [
       {"source_run_id": "<a source run's run id>",
        "step": 7,
        "variant_id": "v1",
        "task_text": "a different task that starts from this same screen",
        "note": "optional: how it was generated"}]}

Reading is strict about the file's shape: it must be JSON, ``format`` must
be ``task-variants-1``, every entry must have exactly these fields with these
types, and a ``variant_id`` must be letters, digits, ``.``, ``_`` or ``-``
(it becomes part of a task id). Anything else is a :class:`BenchmarkDataError`
naming the file and the problem, raised when the loader first scans.

What an entry *says* is judged per entry by the loader, which leaves such an
entry out with its reason (``TraceBranchesTaskLoader.skipped``): a
``(source_run_id, step, variant_id)`` listed more than once (every copy, since
nothing says which is meant), an empty ``task_text``, a run id no source
has, a step that is not a branchable candidate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from spectragent.core.data_loader._common import BenchmarkDataError

#: The one format this reader accepts.
TASK_VARIANTS_FORMAT = "task-variants-1"
#: What a variant id may contain: it is written into the task id ``…/v<variant_id>/b<i>``.
VARIANT_ID_PATTERN = r"^[A-Za-z0-9._-]+$"


class TaskVariantEntry(BaseModel):
    """One variant: a task for the checkpoint at ``step`` of the source run ``source_run_id``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_run_id: StrictStr = Field(min_length=1)
    step: StrictInt
    variant_id: StrictStr = Field(pattern=VARIANT_ID_PATTERN)
    task_text: StrictStr
    note: StrictStr | None = None

    @property
    def key(self) -> str:
        """``<source_run_id>/s<step>/v<variant_id>``: the variant's task ids without ``/b<i>``."""
        return f"{self.source_run_id}/s{self.step}/v{self.variant_id}"


class TaskVariantsFile(BaseModel):
    """The whole file: its format tag and its variants, in file order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["task-variants-1"]
    variants: list[TaskVariantEntry]


def read_task_variants(path: Path) -> list[TaskVariantEntry]:
    """The variants *path* lists, in file order.

    Raises:
        BenchmarkDataError: The file is missing, is not JSON, or does not
            have the ``task-variants-1`` shape — naming the file and the
            first problem found.
    """
    where = f"trace_branches: task_variants file {str(path)!r}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkDataError(f"{where} cannot be read: {exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BenchmarkDataError(f"{where} is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise BenchmarkDataError(
            f"{where} must be a JSON object with 'format' and 'variants', "
            f"got {type(document).__name__}"
        )
    if document.get("format") != TASK_VARIANTS_FORMAT:
        raise BenchmarkDataError(
            f"{where} has format {document.get('format')!r}; the format this reader "
            f"knows is {TASK_VARIANTS_FORMAT!r}"
        )
    try:
        parsed = TaskVariantsFile.model_validate(document)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '(top level)'}: {error['msg']}"
            for error in exc.errors()
        )
        raise BenchmarkDataError(f"{where} is malformed: {problems}") from exc
    return list(parsed.variants)


__all__ = [
    "TASK_VARIANTS_FORMAT",
    "VARIANT_ID_PATTERN",
    "TaskVariantEntry",
    "TaskVariantsFile",
    "read_task_variants",
]
