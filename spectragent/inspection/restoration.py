"""Presentation of recorded restoration facts, shared by terminal and web views."""

from typing import Any


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

    A nonzero dHash distance may pass verification. Conversely, zero distance
    is not proof of identical pixels. Missing measures never become matches.
    """
    check = record.get("restoration")
    if check is None and record.get("origin") != "restored":
        return None
    differences: list[str] = []
    unknown: list[str] = []
    if check is None:
        unknown.append("Restoration check not recorded")
    else:
        if not check.get("evidence"):
            unknown.append("Environment comparison not recorded")
        for item in check.get("evidence", []):
            prefix = item["capability_id"]
            for field, label in (("url_match", "URL"), ("signature_match", "DOM signature")):
                if item.get(field) is False:
                    differences.append(f"{prefix}: {label} changed")
                elif item.get(field) is None:
                    unknown.append(f"{prefix}: {label} not measured")
            distance = item.get("screenshot_distance")
            if distance is None:
                unknown.append(f"{prefix}: screenshot distance not measured")
            elif distance > 0:
                differences.append(f"{prefix}: screenshot dHash distance {distance}")
            misses = item.get("document_misses")
            if misses is None:
                unknown.append(f"{prefix}: document misses not measured")
            elif misses > 0:
                differences.append(f"{prefix}: {misses} document misses")
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
        else "no_measured_difference",
        "differences": differences,
        "unmeasured": unknown,
        "verdict": check.get("verdict") if check else None,
    }


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
