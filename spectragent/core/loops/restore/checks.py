"""Turning measurements into a verdict — the restorer's half of verification.

Capabilities measure (``RestorationSupport.verify_restoration``) and say how
they re-targeted each recorded action; a restorer judges. :func:`judge` is that
judgement, kept apart from any one restorer so every restorer applies the
same rules (``docs/architecture_design.md`` §5.3 ``RestorationCheck``):

- ``document_miss`` — a capability counted replay documents it could not serve;
- ``url_mismatch`` — a capability's restored URL is not the recorded one;
- ``retarget_not_found`` / ``retarget_ambiguous`` — a recorded action found
  no target, or more than one;
- ``action_failed`` — a re-executed action succeeded where the recording
  failed or the reverse, or raised;
- ``signature_mismatch`` — the interactive elements differ;
- ``media_mismatch`` — a media element is missing, extra, ambiguous or a
  live stream; a matched one is further from its recorded position than the
  media tolerance or differs in its paused state; an embedded frame's media
  could not be read; or the media alignment before the capture failed or
  could not be done
  (:func:`media_failed`);
- ``screenshot_distance`` — the screenshots are further apart than the
  threshold.

A measure that is ``None`` (not taken) never takes part. The reasons are
reported in :data:`~spectragent.schemas.restoration.REASON_ORDER`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from spectragent.schemas.restoration import (
    REASON_ORDER,
    MediaEvidence,
    RestorationCheck,
    RestorationEvidence,
    RestorationReason,
    RestoredAction,
)

#: What a failed check means: ``strict`` ends the run, ``warn`` records it.
VerifyPolicy = Literal["strict", "warn"]


def media_failed(media: MediaEvidence, *, time_tolerance_s: float) -> bool:
    """Whether media evidence fails (``docs/architecture_design.md`` §4.14, §5.3).

    Fails on an element present on one side only, an ambiguous
    correspondence, a live stream, a matched element whose two known
    positions are more than *time_tolerance_s* apart or whose two known
    paused states differ, an embedded frame whose own media read did not
    answer on either side (what it holds is unknown, so the step cannot be
    restored), and on an alignment that failed or could not be done.
    Unknown values — a comparison that could not be made, a position or
    state not measured, an alignment with no recorded target — never decide;
    without a comparison the unreachable frames do not decide either.
    """
    if media.comparisons is not None and (
        media.recorded_unreachable_frames or media.restored_unreachable_frames
    ):
        return True
    for item in media.comparisons or []:
        if item.correspondence != "matched" or item.live:
            return True
        if (
            item.recorded_time is not None
            and item.restored_time is not None
            and abs(item.recorded_time - item.restored_time) > time_tolerance_s
        ):
            return True
        if (
            item.recorded_paused is not None
            and item.restored_paused is not None
            and item.recorded_paused != item.restored_paused
        ):
            return True
    return any(record.outcome in ("failed", "not_restorable") for record in media.alignment or [])


def failed_reasons(
    evidence: Sequence[RestorationEvidence],
    actions: Sequence[RestoredAction],
    *,
    screenshot_max_distance: int,
    media_time_tolerance_s: float,
) -> list[RestorationReason]:
    """Every check that did not pass, in ``REASON_ORDER``."""
    failed: set[RestorationReason] = set()
    for item in evidence:
        if item.document_misses is not None and item.document_misses > 0:
            failed.add("document_miss")
        if item.url_match is False:
            failed.add("url_mismatch")
        if item.signature_match is False:
            failed.add("signature_mismatch")
        if item.media is not None and media_failed(
            item.media, time_tolerance_s=media_time_tolerance_s
        ):
            failed.add("media_mismatch")
        if (
            item.screenshot_distance is not None
            and item.screenshot_distance > screenshot_max_distance
        ):
            failed.add("screenshot_distance")
    for action in actions:
        if action.failure == "not_found":
            failed.add("retarget_not_found")
        elif action.failure == "ambiguous":
            failed.add("retarget_ambiguous")
        if action.executed is not None and _action_failed(action):
            failed.add("action_failed")
    return [reason for reason in REASON_ORDER if reason in failed]


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
    screenshot_max_distance: int,
    media_time_tolerance_s: float,
) -> RestorationCheck:
    """The verdict on one restored step.

    ``ok`` when every check passed; otherwise ``diverged`` under ``strict``
    and ``warn`` under ``warn``, with every failed check in ``reasons`` and
    the first of them in ``reason``.
    """
    reasons = failed_reasons(
        evidence,
        actions,
        screenshot_max_distance=screenshot_max_distance,
        media_time_tolerance_s=media_time_tolerance_s,
    )
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
