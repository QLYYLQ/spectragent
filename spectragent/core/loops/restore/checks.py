"""Turning measurements into a verdict — the restorer's half of verification.

Each capability judges its own measurement: its evidence type's checks
(``RestorationEvidence.failed_checks``, defined in the capability's package,
against limits its own configuration set). What every restorer adds is the
same, so :func:`judge` keeps it apart from any one restorer
(``docs/architecture_design.md`` §5.3 ``RestorationCheck``):

- ``retarget_not_found`` / ``retarget_ambiguous`` — a recorded action found
  no target, or more than one;
- ``action_failed`` — a re-executed action succeeded where the recording
  failed or the reverse, or raised;

and the policy, ``strict`` or ``warn``. The reasons are reported in
:func:`~spectragent.schemas.restoration.ordered_reasons` order.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from spectragent.schemas.restoration import (
    RestorationCheck,
    RestorationEvidence,
    RestoredAction,
    ordered_reasons,
)

#: What a failed check means: ``strict`` ends the run, ``warn`` records it.
VerifyPolicy = Literal["strict", "warn"]


def failed_reasons(
    evidence: Sequence[RestorationEvidence], actions: Sequence[RestoredAction]
) -> list[str]:
    """Every check that did not pass: each capability's own, then the actions'."""
    failed: set[str] = set()
    for item in evidence:
        failed.update(item.failed_checks())
    for action in actions:
        if action.failure == "not_found":
            failed.add("retarget_not_found")
        elif action.failure == "ambiguous":
            failed.add("retarget_ambiguous")
        if action.executed is not None and _action_failed(action):
            failed.add("action_failed")
    return ordered_reasons(failed, evidence)


def _action_failed(action: RestoredAction) -> bool:
    if action.live_error is not None:
        return True
    if action.recorded_success is None or action.live_success is None:
        return False
    return action.live_success != action.recorded_success


def judge(
    step: int,
    evidence: Sequence[RestorationEvidence],
    actions: Sequence[RestoredAction],
    *,
    verify: VerifyPolicy,
) -> RestorationCheck:
    """The verdict on one restored step.

    ``ok`` when every check passed; otherwise ``diverged`` under ``strict``
    and ``warn`` under ``warn``, with every failed check in ``reasons`` and
    the first of them in ``reason``.
    """
    reasons = failed_reasons(evidence, actions)
    if not reasons:
        verdict: Literal["ok", "warn", "diverged"] = "ok"
    else:
        verdict = "diverged" if verify == "strict" else "warn"
    return RestorationCheck(
        step=step,
        verdict=verdict,
        reason=reasons[0] if reasons else None,
        reasons=reasons,
        evidence=list(evidence),
        actions=list(actions),
    )
