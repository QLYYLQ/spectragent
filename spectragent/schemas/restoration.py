"""Checkpoint restoration contracts — what a branch run restored, and how well.

A branch run (``BranchLoop``, ``docs/architecture_design.md`` §4.1) puts the
environment back where a recorded run stood before step *t* and lets an
agent decide that step again. These models are what crosses the layers on
the way:

- :class:`Retarget` — a capability's answer to "how should this recorded
  action run in the restored environment?" (``RestorationSupport``, §4.9);
- :class:`RestorationEvidence` — a capability's measurement of how far a
  restored observation is from the recorded one. It measures, never judges:
  thresholds and ``strict | warn`` belong to the restorer;
- :class:`RestoredAction` / :class:`RestorationCheck` — the restorer's
  verdict on one step, carried on that step's ``StepRecord.restoration``
  (§5.3);
- :class:`Checkpoint` — the Loop input naming the source run and *t*;
- :class:`BranchProvenance` — the per-task record of where a branch came
  from (``TraceConfig.branch``, kept out of the configuration digest).

Nothing here carries a price: token counts are the only cost the framework
records.
"""

from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from spectragent.schemas.action import Action, ActionResult

#: Why a restored step did not pass. The order of the literal is also the
#: order the reasons are reported in (``RestorationCheck.reasons``), so the
#: first one (``RestorationCheck.reason``) is the most fundamental: a page
#: the replay could not serve explains every mismatch after it.
RestorationReason = Literal[
    "document_miss",
    "url_mismatch",
    "retarget_not_found",
    "retarget_ambiguous",
    "action_failed",
    "signature_mismatch",
    "media_mismatch",
    "screenshot_distance",
]

#: :data:`RestorationReason` as a tuple, in precedence order.
REASON_ORDER: tuple[RestorationReason, ...] = get_args(RestorationReason)

#: How a recorded action was mapped onto the restored environment.
#: ``identity`` — run as recorded (coordinate and navigation actions, and
#: every action of a capability that does not implement ``RestorationSupport``);
#: ``geometry_signature`` — an element index re-found by position and
#: element signature; ``tab_order_url`` — a tab id re-found by its position
#: and URL.
RetargetMethod = Literal["identity", "geometry_signature", "tab_order_url"]

#: Why a retarget found no single answer.
RetargetFailure = Literal["not_found", "ambiguous"]


class Retarget(BaseModel):
    """How one recorded action should run in the restored environment.

    Exactly one of ``action`` and ``failure`` is set: either there is an
    action to execute, or there is a reason there is none. ``detail`` says
    what was matched (or how many candidates there were) for a reader of
    the trace.
    """

    model_config = ConfigDict(frozen=True)

    action: Action | None
    method: RetargetMethod
    failure: RetargetFailure | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _action_xor_failure(self) -> "Retarget":
        if (self.action is None) == (self.failure is None):
            raise ValueError(
                "Retarget carries exactly one of 'action' and 'failure' "
                f"(got action={self.action!r}, failure={self.failure!r})"
            )
        return self


#: How a media element of one observation corresponds to the other's
#: (``docs/architecture_design.md`` §4.14 "恢复：重定位与校验"). ``matched``:
#: exactly one counterpart, in the same place and order; ``missing``: a
#: recorded element with no counterpart; ``extra``: a restored element no
#: recorded one corresponds to; ``ambiguous``: candidates the position and
#: order cannot single out.
MediaCorrespondence = Literal["matched", "missing", "extra", "ambiguous"]

#: Which of the two readings around the screenshots a comparison used.
MediaReadingPoint = Literal["before", "after"]

#: What aligning one media element came to. ``aligned``: moved to the recorded
#: position and play state, the seek (and the play) completed in time;
#: ``already_aligned``: it was already there, nothing was done; ``failed``: the
#: seek or the play did not complete in time, and it was put back where it
#: was (``rolled_back``); ``not_restorable``: nothing can be aligned (a live
#: stream, an ambiguous or missing counterpart); ``unknown``: the recorded
#: position or play state is not known, so there was no target.
MediaAlignmentOutcome = Literal["aligned", "already_aligned", "failed", "not_restorable", "unknown"]

#: What alignment did to one element: paused and seeked, seeked and played, or nothing.
MediaAlignmentAction = Literal["pause_and_seek", "seek_and_play", "none"]


class MediaComparison(BaseModel):
    """One media element compared between a recorded and a restored observation.

    ``element`` is the capability's name for it (the browser's
    ``<frame>#<order> <tag>``). ``live``: either side is a live stream, which
    has no position to compare. The times and play states are the reading
    both sides took at the same point (``reading``: before the screenshot
    where both have it, else after); ``None`` where it was not measured,
    which never decides. Measures only: the restorer applies its tolerance.
    """

    model_config = ConfigDict(frozen=True)

    element: str
    correspondence: MediaCorrespondence
    live: bool = False
    reading: MediaReadingPoint | None = None
    recorded_time: float | None = None
    restored_time: float | None = None
    recorded_paused: bool | None = None
    restored_paused: bool | None = None
    detail: str = ""


class MediaAlignment(BaseModel):
    """What aligning one media element to the recorded state did, before a restored capture.

    ``target_*``: the recorded state aimed at; ``from_*``: the element before
    alignment; ``reached_*``, ``ready_state`` and ``seeking``: where it stood
    when alignment finished. ``seek_ms``: how long the seek took to complete
    (``None``: it did not, or none was made). ``rolled_back`` is set only for a
    ``failed`` alignment: whether putting the element back where it was
    completed. ``detail`` says why, in words.
    """

    model_config = ConfigDict(frozen=True)

    element: str
    outcome: MediaAlignmentOutcome
    action: MediaAlignmentAction = "none"
    target_time: float | None = None
    target_paused: bool | None = None
    from_time: float | None = None
    from_paused: bool | None = None
    reached_time: float | None = None
    reached_paused: bool | None = None
    ready_state: int | None = None
    seeking: bool | None = None
    seek_ms: float | None = Field(default=None, ge=0)
    rolled_back: bool | None = None
    detail: str = ""


class MediaEvidence(BaseModel):
    """Media playback state compared between two observations, and what was aligned first.

    ``comparisons`` is ``None`` when no comparison could be made — a side
    carries no media state, or both of its readings are unknown — and
    ``detail`` says why; an empty list means neither side has a media
    element. ``recorded_unreachable_frames`` / ``restored_unreachable_frames``
    name the embedded frames whose own media read did not answer on each
    side: what media they hold is unknown, so the step cannot be restored.
    ``alignment`` is ``None`` when no alignment was requested for the
    restored observation (``media: none``); otherwise one entry per recorded
    element it considered, and ``alignment_detail`` says why it is empty
    when it is.
    """

    model_config = ConfigDict(frozen=True)

    comparisons: list[MediaComparison] | None = None
    detail: str = ""
    recorded_unreachable_frames: list[str] = Field(default_factory=list)
    restored_unreachable_frames: list[str] = Field(default_factory=list)
    alignment: list[MediaAlignment] | None = None
    alignment_detail: str = ""


class RestorationEvidence(BaseModel):
    """What one capability measured between a recorded and a restored observation.

    Every measure is ``None`` when the capability does not measure it or
    could not this time (no screenshot fragment in this run, say); a
    ``None`` never takes part in a verdict. ``signature_missing`` /
    ``signature_extra`` detail a signature comparison: elements of the
    recorded page absent from the restored one, and the reverse (both 0
    when no comparison was made). ``media`` compares media playback state
    and records the media alignment done before the restored capture
    (:class:`MediaEvidence`).
    """

    capability_id: str
    url_match: bool | None = None
    recorded_url: str | None = None
    restored_url: str | None = None
    signature_match: bool | None = None
    signature_missing: int = Field(default=0, ge=0)
    signature_extra: int = Field(default=0, ge=0)
    screenshot_distance: int | None = Field(default=None, ge=0)
    document_misses: int | None = Field(default=None, ge=0)
    media: MediaEvidence | None = None


class RestoredAction(BaseModel):
    """What happened to one recorded action during restoration.

    ``executed`` is the action handed to ``act_fn`` — ``None`` when a
    retarget failed under ``verify: strict`` and nothing was executed.
    ``recorded_success`` is the source result's ``success`` (``None`` when
    the source step has no result for it); ``live_success`` is the restored
    execution's, and ``live_error`` the exception it raised, if it raised
    (``"Type: message"``; a returned failure is compared by ``success``
    alone, since the recording failed the same way or it did not).
    """

    recorded: Action
    executed: Action | None
    method: RetargetMethod
    failure: RetargetFailure | None = None
    detail: str = ""
    recorded_success: bool | None
    result: ActionResult | None = None
    live_success: bool | None = None
    live_error: str | None = None


class RestorationCheck(BaseModel):
    """The restorer's verdict on one restored step.

    ``reasons`` lists every check that did not pass, in
    :data:`REASON_ORDER`; ``reason`` is the first of them. ``verdict`` is
    ``ok`` exactly when nothing failed, and otherwise says which policy
    applied: ``warn`` (recorded, the run went on) or ``diverged`` (``strict``:
    the run ends here). ``actions`` is empty on the checkpoint step itself —
    its action is the one the branch is about to decide.
    """

    step: int = Field(ge=0)
    verdict: Literal["ok", "warn", "diverged"]
    reason: RestorationReason | None = None
    reasons: list[RestorationReason] = Field(default_factory=list)
    evidence: list[RestorationEvidence] = Field(default_factory=list)
    actions: list[RestoredAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "RestorationCheck":
        ordered = [r for r in REASON_ORDER if r in self.reasons]
        if list(self.reasons) != ordered:
            raise ValueError(
                f"reasons must be distinct and in REASON_ORDER; got {self.reasons!r}, "
                f"expected {ordered!r}"
            )
        first = self.reasons[0] if self.reasons else None
        if self.reason != first:
            raise ValueError(
                f"reason must be the first of reasons ({first!r}), got {self.reason!r}"
            )
        if (self.verdict == "ok") != (not self.reasons):
            raise ValueError(
                f"verdict {self.verdict!r} does not fit reasons {self.reasons!r}: "
                "'ok' exactly when nothing failed"
            )
        return self


def _is_absolute(path: str) -> bool:
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


class Checkpoint(BaseModel):
    """Where a branch starts: a recorded run's directory and the step to decide again.

    The Loop input ``checkpoint`` of ``BranchLoop`` (``TaskContext.loop``).
    ``trace_dir`` is absolute: a branch sweep may run from another working
    directory than the one that listed the sources.
    """

    model_config = ConfigDict(frozen=True)

    trace_dir: str
    step: int = Field(ge=0)

    @field_validator("trace_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not _is_absolute(value):
            raise ValueError(f"Checkpoint.trace_dir must be an absolute path, got {value!r}")
        return value


class BranchProvenance(BaseModel):
    """Where a branch run came from — recorded as ``TraceConfig.branch``.

    Per task (the task id itself is ``<source_run_id>/s<t>/b<i>``), so it is
    not part of the configuration digest: every task of one branch sweep
    shares a ``config_hash``. ``source_trace`` is the source run directory
    relative to the sources root (POSIX), so a reader holding the same root
    finds the source trace; the source benchmark / subset / task id join
    the branch back to the source task's reference.
    """

    model_config = ConfigDict(frozen=True)

    source_run_id: str
    source_trace: str
    source_benchmark: str = ""
    source_subset: str = ""
    source_task_id: str = ""
    checkpoint_step: int = Field(ge=0)
    branch_index: int = Field(ge=0)
    branch_group_id: str


__all__ = [
    "REASON_ORDER",
    "BranchProvenance",
    "Checkpoint",
    "MediaAlignment",
    "MediaAlignmentAction",
    "MediaAlignmentOutcome",
    "MediaComparison",
    "MediaCorrespondence",
    "MediaEvidence",
    "MediaReadingPoint",
    "RestorationCheck",
    "RestorationEvidence",
    "RestorationReason",
    "RestoredAction",
    "Retarget",
    "RetargetFailure",
    "RetargetMethod",
]
