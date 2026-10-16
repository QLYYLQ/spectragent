"""Pure terminal presentation shared by live and recorded experiment views."""

from typing import Any

from spectragent.inspection.restoration import restoration_gap


def gap_text(gap: dict[str, Any] | None) -> str:
    """A compact difference label that does not equate tolerance with equality."""
    if gap is None:
        return "—"
    label = (
        f"{len(gap['differences'])} differences"
        if gap["differences"]
        else "unmeasured"
        if gap["unmeasured"]
        else "no measured gap"
    )
    return f"{label} · {gap['verdict'] or 'unknown'}"


def terminal_value(value: Any) -> Any:
    """Keep image payloads out of terminal text while preserving all other details."""
    if isinstance(value, str) and value.startswith("data:image/"):
        return f"[inline image, {len(value):,} encoded characters; view in dashboard]"
    if isinstance(value, dict):
        return {key: terminal_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [terminal_value(item) for item in value]
    return value


def step_presentation(record: dict[str, Any]) -> dict[str, Any]:
    """Label returned action pairs and all restoration attempts separately.

    Source replies and actions stay in ``source_history``. The step's top-level
    action/result pairs describe this run, including a returned failure.
    """
    if record.get("origin") != "restored":
        return record
    check = record.get("restoration") or {}
    return {
        "agent_history_vs_replay": restoration_gap(record),
        **{
            key: value
            for key, value in record.items()
            if key not in {"actions", "action_results", "restoration"}
        },
        "actual_actions": record.get("actions", []),
        "actual_action_results": record.get("action_results", []),
        "actual_restoration_actions": check.get("actions", []),
        "restoration_check": {key: value for key, value in check.items() if key != "actions"},
    }


def step_actions(record: dict[str, Any]) -> str:
    """Summarize source material and actual attempts without inferring success."""
    names = ", ".join(action.get("name", "?") for action in record.get("actions", []))
    if record.get("origin") != "restored":
        return names
    source = record.get("source_history") or {}
    names = ", ".join(action.get("name", "?") for action in source.get("source_actions", []))
    actual = []
    for action in (record.get("restoration") or {}).get("actions", []):
        executed = action.get("executed")
        if executed is None:
            actual.append(f"not executed ({action.get('failure') or 'no action'})")
        else:
            success = action.get("live_success")
            result = (
                "failed"
                if action.get("live_error") or success is False
                else "ok"
                if success is True
                else "unknown result"
            )
            actual.append(f"{executed.get('name', '?')} ({result})")
    return f"source: {names or '—'}; actual: {', '.join(actual) or 'no execution recorded'}"
