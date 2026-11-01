"""The oracle score record (``docs/architecture_design.md`` §5.3): the status decides the rest.

A scored entry carries its score and the oracle that gave it and no reason; a
not-scored entry carries a reason and no score, so an evaluator reading
``score`` can never count a run nobody scored. The record is built by
capabilities, which do not import the trace layer, so its module imports
nothing from ``spectragent.core`` (§10).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import spectragent.schemas.oracle_score as oracle_score_module
from spectragent.schemas.oracle_score import OracleScore
from tests.test_core.test_archive.test_archive_boundaries import imported_modules

SCORED: dict[str, Any] = {
    "capability_id": "android",
    "status": "scored",
    "score": 1.0,
    "oracle": "androidworld",
    "started_at": 1_760_000_000.0,
    "duration_ms": 812,
}
NOT_SCORED: dict[str, Any] = {
    "capability_id": "android",
    "status": "not_scored",
    "started_at": 1_760_000_000.0,
    "duration_ms": 60_000,
    "reason": "timeout",
    "detail": "no score within 60 s (oracle_timeout)",
}


def test_a_scored_and_a_not_scored_entry_round_trip_through_json() -> None:
    for fields in (SCORED, NOT_SCORED):
        entry = OracleScore(**fields)
        assert OracleScore.model_validate_json(entry.model_dump_json()) == entry


@pytest.mark.parametrize(
    ("base", "change"),
    [
        (SCORED, {"score": None}),
        (SCORED, {"oracle": None}),
        (SCORED, {"reason": "error"}),
        (NOT_SCORED, {"reason": None}),
        (NOT_SCORED, {"score": 0.0}),
        (SCORED, {"score": float("nan")}),
        (SCORED, {"started_at": float("inf")}),
        (SCORED, {"duration_ms": -1}),
        (SCORED, {"status": "skipped"}),
        (NOT_SCORED, {"reason": "crashed"}),
    ],
    ids=[
        "scored without a score",
        "scored without an oracle",
        "scored with a reason",
        "not scored without a reason",
        "not scored with a score",
        "a score that is not a number",
        "a start that is not a time",
        "a negative duration",
        "an unknown status",
        "an unknown reason",
    ],
)
def test_an_inconsistent_entry_is_refused(base: dict[str, Any], change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OracleScore(**{**base, **change})


def test_an_entry_is_not_changed_once_built() -> None:
    entry = OracleScore(**SCORED)
    with pytest.raises(ValidationError):
        entry.score = 0.0  # type: ignore[misc]


def test_the_module_imports_nothing_from_the_core() -> None:
    """A capability builds the record; it must not need the trace layer to do so."""
    path = Path(oracle_score_module.__file__)
    imported = imported_modules(path.read_text(), oracle_score_module.__name__)
    assert [name for name in imported if name.startswith("spectragent.core")] == []
