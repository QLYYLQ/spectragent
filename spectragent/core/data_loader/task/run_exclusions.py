"""The ``exclude_runs`` file of the ``trace_branches`` loader (``docs/architecture_design.md`` §4.10).

A run-exclusions file names recorded runs that are not to be used as branch
sources, each with why: a recording whose page never rendered, whose content
requests failed while it was recorded, and so on. Something else decides
that, usually an offline health check over the recordings; the loader only
applies it. The file is JSON::

    {"format": "run-exclusions-1",
     "runs": [
       {"run_id": "<a recorded run's run id>",
        "reason": "why it is not to be used",
        "note": "optional: who or what decided it"}]}

Reading is strict about the file's shape: it must be JSON, ``format`` must
be ``run-exclusions-1``, every entry must have exactly these fields with
these types, and ``run_id`` and ``reason`` must not be empty. Anything else
is a :class:`BenchmarkDataError` naming the file and the problem. A run id
listed more than once is excluded once, with every reason given.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from spectragent.core.data_loader._common import BenchmarkDataError

#: The one format this reader accepts.
RUN_EXCLUSIONS_FORMAT = "run-exclusions-1"


class RunExclusion(BaseModel):
    """One recorded run not to be used, by its run id, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: StrictStr = Field(min_length=1)
    reason: StrictStr = Field(min_length=1)
    note: StrictStr | None = None


class RunExclusionsFile(BaseModel):
    """The whole file: its format tag and its entries, in file order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["run-exclusions-1"]
    runs: list[RunExclusion]


def read_run_exclusions(path: Path) -> dict[str, str]:
    """Why each run *path* lists is excluded, by run id (several reasons joined by ``; ``).

    Raises:
        BenchmarkDataError: The file is missing, is not JSON, or does not
            have the ``run-exclusions-1`` shape — naming the file and the
            first problem found.
    """
    where = f"exclude_runs file {str(path)!r}"
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
            f"{where} must be a JSON object with 'format' and 'runs', got {type(document).__name__}"
        )
    if document.get("format") != RUN_EXCLUSIONS_FORMAT:
        raise BenchmarkDataError(
            f"{where} has format {document.get('format')!r}; the format this reader "
            f"knows is {RUN_EXCLUSIONS_FORMAT!r}"
        )
    try:
        parsed = RunExclusionsFile.model_validate(document)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '(top level)'}: {error['msg']}"
            for error in exc.errors()
        )
        raise BenchmarkDataError(f"{where} is malformed: {problems}") from exc
    reasons: dict[str, list[str]] = {}
    for entry in parsed.runs:
        given = reasons.setdefault(entry.run_id, [])
        if entry.reason not in given:
            given.append(entry.reason)
    return {run_id: "; ".join(given) for run_id, given in reasons.items()}


__all__ = [
    "RUN_EXCLUSIONS_FORMAT",
    "RunExclusion",
    "RunExclusionsFile",
    "read_run_exclusions",
]
