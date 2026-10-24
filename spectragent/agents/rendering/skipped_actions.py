"""Say which actions of a step were not executed, and why (``docs/architecture_design.md`` §4.1).

The ReAct Loop stops a step at its first failed action (the user's ruling of
2026-10-24): the actions after it never run, and the step records them as
skipped (``StepRecord.skipped_actions``). Every Agent tells its model so at
its next step, in its own history format, with the same reason, e.g.::

    Action 2 (send_keys) was not executed: action 1 failed.

How the action itself reads is the Agent's: it passes *write*, and puts the
action's points on the step's frame first (``fact_in_model_frame``), as it
does for every action it shows. A step with nothing skipped yields nothing,
so a prompt that has no skipped action to show reads as it did before.
"""

from __future__ import annotations

from collections.abc import Callable

from spectragent.schemas.action import Action
from spectragent.schemas.step_record import StepRecord


def skip_reason(record: StepRecord) -> str:
    """Why *record*'s skipped actions did not run: ``"action N failed"`` (N from 1)."""
    return f"action {len(record.action_results)} failed"


def not_executed_lines(record: StepRecord, *, write: Callable[[Action], str]) -> list[str]:
    """One sentence per skipped action: ``Action 2 (<write(action)>) was not executed: action 1 failed.``"""
    reason = skip_reason(record)
    first = len(record.action_results) + 1
    return [
        f"Action {position} ({write(action)}) was not executed: {reason}."
        for position, action in enumerate(record.skipped(), start=first)
    ]


__all__ = ["not_executed_lines", "skip_reason"]
