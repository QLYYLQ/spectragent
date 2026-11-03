"""A verdict, the judgment it holds and who judged (``docs/architecture_design.md`` §5.3 "判定统一存放").

A judgment holds only what a verifier found. In the verdict the status decides
the rest: a judged verdict carries a score, a label or both and no reason; a
not-judged one carries a reason and neither, so a reader of ``label`` or
``score`` can never count a run nobody judged. The slug, the verdict's name in
the store, is derived from who judged. Capabilities build judgments and the
environment builds verdicts; neither imports the trace layer, so the module
imports nothing from ``spectragent.core`` (§10).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import spectragent.schemas.verdict as verdict_module
from spectragent.schemas.verdict import Judgment, Verdict, VerifierIdentity
from tests.test_core.test_archive.test_archive_boundaries import imported_modules

WHO = VerifierIdentity(verifier_id="androidworld", version="55ddaf5")
JUDGED: dict[str, Any] = {
    "verifier": WHO,
    "attachment": "live",
    "status": "judged",
    "score": 1.0,
    "started_at": 1_760_000_000.0,
    "duration_ms": 812,
}
NOT_JUDGED: dict[str, Any] = {
    "verifier": WHO,
    "attachment": "live",
    "status": "not_judged",
    "started_at": 1_760_000_000.0,
    "duration_ms": 60_000,
    "reason": "timeout",
    "detail": "no verdict within 60 s (verify_timeout)",
}


def test_a_judged_and_a_not_judged_verdict_round_trip_through_json() -> None:
    for fields in (JUDGED, NOT_JUDGED, {**JUDGED, "score": None, "label": 0}):
        verdict = Verdict(**fields)
        assert Verdict.model_validate_json(verdict.model_dump_json()) == verdict


@pytest.mark.parametrize(
    ("base", "change"),
    [
        (JUDGED, {"score": None}),
        (JUDGED, {"reason": "error"}),
        (NOT_JUDGED, {"reason": None}),
        (NOT_JUDGED, {"score": 0.0}),
        (NOT_JUDGED, {"label": 0}),
        (JUDGED, {"score": float("nan")}),
        (JUDGED, {"label": 2}),
        (JUDGED, {"started_at": float("inf")}),
        (JUDGED, {"duration_ms": -1}),
        (JUDGED, {"status": "skipped"}),
        (JUDGED, {"attachment": "per_step"}),
        (NOT_JUDGED, {"reason": "crashed"}),
        (JUDGED, {"verifier": {"verifier_id": ""}}),
        (JUDGED, {"graded": True}),
    ],
    ids=[
        "judged without a finding",
        "judged with a reason",
        "not judged without a reason",
        "not judged with a score",
        "not judged with a label",
        "a score that is not a number",
        "a label other than pass or fail",
        "a start that is not a time",
        "a negative duration",
        "an unknown status",
        "an attachment there is not",
        "an unknown reason",
        "a verifier with no kind",
        "a field it does not have",
    ],
)
def test_an_inconsistent_verdict_is_refused(base: dict[str, Any], change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Verdict(**{**base, **change})


def test_a_verdict_is_not_changed_once_built() -> None:
    verdict = Verdict(**JUDGED)
    with pytest.raises(ValidationError):
        verdict.score = 0.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "fields",
    [{}, {"score": float("inf")}, {"label": 1, "started_at": 1_760_000_000.0}],
    ids=["no finding", "a score that is not a number", "its own timing"],
)
def test_a_judgment_holds_a_finding_and_nothing_the_attaching_side_records(
    fields: dict[str, Any],
) -> None:
    """Who judged and when are the attaching side's to record."""
    with pytest.raises(ValidationError):
        Judgment(**fields)


def test_a_judged_verdict_carries_its_judgment_and_what_the_attaching_side_adds() -> None:
    judgment = Judgment(score=0.5, rationale="half", evidence={"family": "android"})
    verdict = Verdict.judged(
        WHO,
        "offline",
        judgment,
        started_at=1.0,
        duration_ms=3,
        evidence={"source_reference": {"task_id": "t"}},
    )
    assert (verdict.status, verdict.score, verdict.label, verdict.rationale) == (
        "judged",
        0.5,
        None,
        "half",
    )
    assert verdict.evidence == {"family": "android", "source_reference": {"task_id": "t"}}


def test_the_slug_names_the_kind_and_the_settings_never_the_version() -> None:
    plain = VerifierIdentity(verifier_id="exact_match")
    assert plain.slug.startswith("exact_match_") and len(plain.slug) == len("exact_match_") + 8
    assert VerifierIdentity(verifier_id="exact_match", config_signature={"a": 1}).slug != plain.slug
    assert VerifierIdentity(verifier_id="exact_match", version="2").slug == plain.slug
    assert (
        VerifierIdentity(verifier_id="x", config_signature={"a": 1, "b": 2}).slug
        == VerifierIdentity(verifier_id="x", config_signature={"b": 2, "a": 1}).slug
    )


def test_the_module_imports_nothing_from_the_core() -> None:
    """Capabilities and the environment build these; neither may need the trace layer."""
    path = Path(verdict_module.__file__)
    imported = imported_modules(path.read_text(), verdict_module.__name__)
    assert [name for name in imported if name.startswith("spectragent.core")] == []
