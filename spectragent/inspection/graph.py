"""Small committed-step summaries for the task graph, without loading images or calls."""

from __future__ import annotations

from typing import Any

from spectragent.core.trace.reader import RunTrace
from spectragent.inspection.marks import action_text


def run_graph(trace: RunTrace) -> dict[str, Any]:
    """Present real step indices and origins; lineage comes from the run's provenance.

    Reading a graph must not decode prompt images, load archives, or open another
    run. In particular, a restoration prefix is never represented as new decisions.
    """
    steps = []
    for record in trace.step_records:
        actions = []
        for index, action in enumerate(record.actions):
            result = record.action_results[index] if index < len(record.action_results) else None
            skipped = (
                record.skipped_actions > 0 and index >= len(record.actions) - record.skipped_actions
            )
            actions.append(
                {
                    "index": index,
                    "name": action.name,
                    "text": action_text(action, result),
                    "status": "skipped"
                    if skipped
                    else "succeeded"
                    if result is not None and result.success
                    else "failed"
                    if result is not None
                    else "no_result",
                }
            )
        steps.append(
            {
                "step": record.step,
                "origin": record.origin,
                "actions": actions,
                "error": record.error,
                "error_phase": record.error_phase,
                "restoration_verdict": record.restoration.verdict if record.restoration else None,
            }
        )
    return {
        "steps": steps,
        "recording_trajectory": trace.config.recording.trajectory,
        "restored_steps": sum(step["origin"] == "restored" for step in steps),
    }
