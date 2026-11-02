"""Checkpoint restoration contracts — what a branch run restored, and how well.

A branch run (``BranchLoop``, ``docs/architecture_design.md`` §4.1) puts the
environment back where a recorded run stood before step *t* and lets an
agent decide that step again. These models are what crosses the layers on
the way:

- :class:`Retarget` — a capability's answer to "how should this recorded
  action run in the restored environment?" (``RestorationSupport``, §4.9);
- :class:`RestorationEvidence` — the base of what one capability measured
  between a recorded and a restored observation. Each capability that
  implements ``RestorationSupport`` defines its own subclass — the facts it
  measures, the limits its checks apply and the checks themselves — and
  names it on its class; the trace reader reads one back as that type by
  its ``capability_id``, the way it reads an action's ``Effect`` (§4.5);
- :class:`RestoredAction` / :class:`RestorationCheck` — the restorer's
  verdict on one step, carried on that step's ``StepRecord.restoration``
  (§5.3);
- :class:`Checkpoint` — the Loop input naming the source run and *t*
  (and the task variant the branch decides under, when it has one);
- :class:`BranchProvenance` — the per-task record of where a branch came
  from (``TraceConfig.branch``, kept out of the configuration digest), with
  :class:`BranchTaskVariant` when the branch's task replaced the source's.

Nothing here names a capability's measures: the browser's evidence and checks
are in ``core/capabilities/browser/restoration_evidence.py``. Nothing here
carries a price either: token counts are the only cost the framework records.
"""

from collections.abc import Collection, Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, ClassVar, Final, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from spectragent.schemas.action import Action, ActionResult

#: The retarget method of an action that runs as recorded — the framework's own:
#: every action of a capability that does not implement ``RestorationSupport``
#: runs this way. Every other method is a capability's own name for how it
#: re-found a target (the browser's ``geometry_signature`` and ``tab_order_url``).
IDENTITY: Final = "identity"

#: Why a retarget found no single answer.
RetargetFailure = Literal["not_found", "ambiguous"]

#: The framework's own reasons a restored step does not pass, found by re-executing
#: its recorded actions: a retarget that found no target, or more than one; an action
#: whose outcome differs from the recorded one, or that raised. Every other reason is
#: a capability's own check (:attr:`RestorationEvidence.REASONS`).
ActionReason = Literal["retarget_not_found", "retarget_ambiguous", "action_failed"]

#: :data:`ActionReason` as a tuple, in reporting order.
ACTION_REASONS: tuple[ActionReason, ...] = get_args(ActionReason)


class Retarget(BaseModel):
    """How one recorded action should run in the restored environment.

    Exactly one of ``action`` and ``failure`` is set: either there is an
    action to execute, or there is a reason there is none. ``method`` is how
    the answer was found: :data:`IDENTITY` (as recorded) or the capability's
    own name for its way. ``detail`` says what was matched (or how many
    candidates there were) for a reader of the trace.
    """

    model_config = ConfigDict(frozen=True)

    action: Action | None
    method: str = Field(min_length=1)
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


class RestorationEvidence(BaseModel):
    """What one capability measured between a recorded and a restored observation — the base.

    A capability that implements ``RestorationSupport``
    (``core/capabilities/base.py``) defines a subclass — the facts it measures,
    the limits its checks apply (from its own configuration) and the checks —
    and names it on its class as ``RestorationEvidence``. ``capability_id`` is
    the registry key of the capability that measured it: the trace reader looks
    it up in the capability registry and reads the payload back with that
    capability's ``RestorationEvidence`` (``core/trace/effects.py``), as an
    action's ``Effect`` is read back by its ``effect_id``
    (``docs/architecture_design.md`` §4.5, §4.9). This package never imports a
    capability.

    :attr:`REASONS` names the subclass's checks, in the order they are
    reported; :meth:`failed_checks` is the judgement: the checks this
    measurement does not pass, from its own fields alone. A measure the
    capability did not take, or could not take this time, decides nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Every check :meth:`failed_checks` can report, in reporting order.
    REASONS: ClassVar[tuple[str, ...]] = ()

    capability_id: str = Field(min_length=1)

    def failed_checks(self) -> list[str]:
        """The checks of :attr:`REASONS` this measurement does not pass, in that order."""
        raise NotImplementedError(f"{type(self).__name__} must implement failed_checks()")


def ordered_reasons(failed: Collection[str], evidence: Sequence[RestorationEvidence]) -> list[str]:
    """*failed* in reporting order: the restored observation's checks, then its actions'.

    First the capabilities' own checks — in evidence order, each in its evidence
    type's ``REASONS`` order — then :data:`ACTION_REASONS`: an observation that
    already left the recording explains the action failures after it.

    Raises:
        ValueError: A reason that no evidence here declares and that is not the
            framework's.
    """
    declared = [reason for item in evidence for reason in type(item).REASONS]
    unknown = sorted(set(failed) - {*declared, *ACTION_REASONS})
    if unknown:
        raise ValueError(
            f"restoration reasons {unknown} are neither the framework's {list(ACTION_REASONS)} "
            f"nor a check the evidence here declares ({declared})"
        )
    return [reason for reason in dict.fromkeys([*declared, *ACTION_REASONS]) if reason in failed]


class RestoredAction(BaseModel):
    """What happened to one recorded action during restoration.

    ``executed`` is the action handed to ``act_fn`` — ``None`` when a
    retarget failed under ``verify: strict`` and nothing was executed.
    ``method`` / ``failure`` / ``detail`` are the retarget's
    (:class:`Retarget`). ``recorded_success`` is the source result's
    ``success`` (``None`` when the source step has no result for it);
    ``live_success`` is the restored execution's, and ``live_error`` the
    exception it raised, if it raised (``"Type: message"``; a returned failure
    is compared by ``success`` alone, since the recording failed the same way
    or it did not).
    """

    recorded: Action
    executed: Action | None
    method: str = Field(min_length=1)
    failure: RetargetFailure | None = None
    detail: str = ""
    recorded_success: bool | None
    result: ActionResult | None = None
    live_success: bool | None = None
    live_error: str | None = None


class RestorationCheck(BaseModel):
    """The restorer's verdict on one restored step.

    ``evidence`` holds each restoring capability's own measurement, as its
    own type. ``reasons`` lists every check that did not pass, in
    :func:`ordered_reasons` order — the capabilities' own checks, then the
    framework's :data:`ACTION_REASONS` — and ``reason`` is the first of them.
    ``verdict`` is ``ok`` exactly when nothing failed, and otherwise says
    which policy applied: ``warn`` (recorded, the run went on) or
    ``diverged`` (``strict``: the run ends here). ``actions`` is empty on the
    checkpoint step itself — its action is the one the branch is about to
    decide.
    """

    step: int = Field(ge=0)
    verdict: Literal["ok", "warn", "diverged"]
    reason: str | None = None
    reasons: list[str] = Field(default_factory=list)
    evidence: list[SerializeAsAny[RestorationEvidence]] = Field(default_factory=list)
    actions: list[RestoredAction] = Field(default_factory=list)

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence_is_typed(cls, value: Any) -> Any:
        """Evidence arrives as its capability's own model, never as a bare mapping.

        Only the capability registry knows which class a ``capability_id``
        names, and this package does not import it: recorded evidence is
        rebuilt by the trace reader (``core/trace/effects.py``) before the
        check is validated. A mapping reaching here was not, and would
        otherwise be read as this base or refused for its fields.
        """
        if isinstance(value, (list, tuple)) and any(isinstance(item, Mapping) for item in value):
            raise ValueError(
                "restoration evidence must be RestorationEvidence instances; recorded "
                "evidence is read back by the trace reader, which resolves its "
                "capability_id through the capability registry"
            )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "RestorationCheck":
        ordered = ordered_reasons(self.reasons, self.evidence)
        if list(self.reasons) != ordered:
            raise ValueError(
                f"reasons must be distinct and in reporting order; got {self.reasons!r}, "
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

    ``task_variant`` is the id of the task variant the branch decides under
    instead of the source's task (the ``trace_branches`` loader's
    ``task_variants`` file), or ``None`` when it keeps the source's task. The
    variant's text reaches the agent as the run's task, like any task; the
    Loop reads only the id, to refuse ``history_source: source``, which
    replays the source's exact prompts and so its task (§4.1).
    """

    model_config = ConfigDict(frozen=True)

    trace_dir: str
    step: int = Field(ge=0)
    task_variant: str | None = None

    @field_validator("trace_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not _is_absolute(value):
            raise ValueError(f"Checkpoint.trace_dir must be an absolute path, got {value!r}")
        return value


class BranchTaskVariant(BaseModel):
    """The task a branch decided under in place of its source's ("same page, different need").

    One entry of the ``trace_branches`` loader's ``task_variants`` file,
    recorded on :class:`BranchProvenance` so a reader of the branch trace
    alone knows both tasks: ``task_text`` is the run's own task
    (``TraceConfig.task``), ``source_task_text`` the source run's.

    Attributes:
        variant_id: The variant's id in the file, unique per source run and
            step; the task id carries it (``…/s<t>/v<variant_id>/b<i>``).
        task_text: The variant's task — the task this branch ran.
        source_task_text: The source run's task, as its trace recorded it.
        note: The file's free-form note (how the variant was generated), or
            ``None`` when it gave none.
    """

    model_config = ConfigDict(frozen=True)

    variant_id: str
    task_text: str
    source_task_text: str
    note: str | None = None


class BranchProvenance(BaseModel):
    """Where a branch run came from — recorded as ``TraceConfig.branch``.

    Per task (the task id itself is ``<source_run_id>/s<t>/b<i>``, or
    ``<source_run_id>/s<t>/v<variant_id>/b<i>`` for a task variant), so it is
    not part of the configuration digest: every task of one branch sweep
    shares a ``config_hash``. ``source_trace`` is the source run directory
    relative to the sources root (POSIX), so a reader holding the same root
    finds the source trace; the source benchmark / subset / task id join
    the branch back to the source task's reference.

    ``branch_group_id`` names the branches given identical inputs: the task
    id without its ``/b<i>``. ``task_variant`` is ``None`` (not written) when
    the branch kept its source's task.
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
    task_variant: BranchTaskVariant | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


__all__ = [
    "ACTION_REASONS",
    "IDENTITY",
    "ActionReason",
    "BranchProvenance",
    "BranchTaskVariant",
    "Checkpoint",
    "RestorationCheck",
    "RestorationEvidence",
    "RestoredAction",
    "Retarget",
    "RetargetFailure",
    "ordered_reasons",
]
