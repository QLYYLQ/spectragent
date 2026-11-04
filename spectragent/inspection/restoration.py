"""Presentation of recorded restoration facts, shared by terminal and web views.

Each capability's restoration evidence class reads its own measurements
(``core/presentation.py::MeasuresRestorationGap``): what it measured as
different, and what it did not measure. Evidence of a capability whose class
does not is named as not shown here, never read as missing measures; its own
checks still decide the step's verdict (``docs/architecture_design.md`` §4.9).
"""

from collections.abc import Iterable
from typing import Any

from spectragent.core.presentation import MeasuresRestorationGap, registered
from spectragent.core.trace.effects import registered_restoration_evidence
from spectragent.schemas.restoration import RestorationEvidence


def _outcome(result: dict[str, Any]) -> dict[str, Any]:
    """A result without its measured effect: what the status compares."""
    return {key: value for key, value in result.items() if key != "effect"}


def restoration_results(
    record: dict[str, Any], source: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Compare result payloads without pairing across a missing/failed attempt.

    The status compares outcomes only. A result's measured ``effect`` (scroll
    offsets, the element a click hit, how long a wait took) is shown with
    both payloads but not judged here: measurements of a re-executed action
    legitimately differ in timing, and whether a difference matters is the
    reader's call.
    """
    comparisons = []
    source_actions = (source or {}).get("actions", [])
    source_results = (source or {}).get("action_results", [])
    for index, attempt in enumerate((record.get("restoration") or {}).get("actions", [])):
        original = (
            source_results[index]
            if index < len(source_actions)
            and index < len(source_results)
            and source_actions[index] == attempt.get("recorded")
            else None
        )
        actual = attempt.get("result")
        comparisons.append(
            {
                "source": original,
                "actual": actual,
                "status": "unavailable"
                if original is None or actual is None
                else "matches"
                if _outcome(original) == _outcome(actual)
                else "changed",
            }
        )
    return comparisons


def restoration_gap(record: dict[str, Any]) -> dict[str, Any] | None:
    """Separate measured differences from the restorer's tolerance verdict.

    Each evidence's measures are its capability's to read: a measure that
    passed the check may still differ, and missing measures never become
    matches. Evidence whose class does not read its measures is listed in
    ``not_shown``: this view does not know them, and they are neither
    differences nor unmeasured facts. With nothing else to report, the status
    is ``not_shown``: its own checks may have failed, so no "no measured
    difference" is claimed.
    """
    check = record.get("restoration")
    if check is None and record.get("origin") != "restored":
        return None
    differences: list[str] = []
    unknown: list[str] = []
    not_shown: list[str] = []
    if check is None:
        unknown.append("Restoration check not recorded")
    else:
        if not check.get("evidence"):
            unknown.append("Environment comparison not recorded")
        for item in check.get("evidence", []):
            capability = item["capability_id"]
            evidence = registered(_evidence_class, capability)
            if not isinstance(evidence, MeasuresRestorationGap):
                not_shown.append(f"{capability}: not shown in this view")
                continue
            measured, unmeasured = evidence.measured_gap(item)
            differences += measured
            unknown += unmeasured
        for index, action in enumerate(check.get("actions", [])):
            prefix = f"Action {index + 1}"
            if action.get("executed") is None:
                differences.append(f"{prefix}: not executed")
            elif action["executed"] != action.get("recorded"):
                differences.append(f"{prefix}: target or parameters changed")
            if action.get("live_error"):
                differences.append(f"{prefix}: execution raised an exception")
            elif action.get("live_success") is None or action.get("recorded_success") is None:
                unknown.append(f"{prefix}: outcome comparison unavailable")
            elif action["live_success"] != action["recorded_success"]:
                differences.append(f"{prefix}: success changed")
    return {
        "status": "changed"
        if differences
        else "unmeasured"
        if unknown
        else "not_shown"
        if not_shown
        else "no_measured_difference",
        "differences": differences,
        "unmeasured": unknown,
        "not_shown": not_shown,
        "verdict": check.get("verdict") if check else None,
    }


def _evidence_class(capability_id: str) -> type[RestorationEvidence] | None:
    try:
        return registered_restoration_evidence(capability_id)
    except ValueError:
        return None


def restoration_overview(records: list[dict[str, Any]], checkpoint: int) -> dict[str, Any]:
    """Count committed checks through the checkpoint, with coverage visible."""
    checked = [row for row in records if row.get("restoration") and row["step"] <= checkpoint]
    return {
        "expected": checkpoint + 1,
        "checked": len(checked),
        "accepted": sum(row["restoration"]["verdict"] == "ok" for row in checked),
        "warned": sum(row["restoration"]["verdict"] == "warn" for row in checked),
        "diverged": sum(row["restoration"]["verdict"] == "diverged" for row in checked),
        "different_steps": [
            row["step"] for row in checked if (restoration_gap(row) or {}).get("differences")
        ],
    }


def unmeasured_everywhere(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Unmeasured facts shared by every checked step of a run, in recorded order.

    A checked step is one with a restoration gap: a restored step, or one
    carrying a restoration check. A fact no checked step measured is stated
    once for the run (the user's ruling of 2026-10-24): it stays unknown and
    is never drawn as zero, while step rows count only the unmeasured facts
    that vary between steps. Each step's own gap is unchanged.
    """
    gaps = [gap for record in records if (gap := restoration_gap(record)) is not None]
    shared = set(gaps[0]["unmeasured"]) if gaps else set()
    for gap in gaps[1:]:
        shared &= set(gap["unmeasured"])
    facts = [line for line in gaps[0]["unmeasured"] if line in shared] if gaps else []
    return {"facts": facts, "checked_steps": len(gaps)}
