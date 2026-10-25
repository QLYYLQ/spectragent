"""The canonical training sample: one exportable step of a recorded run (arch §4.15).

The training export reads a ``RunTrace`` and, for every step a student
could learn from, writes one :class:`CanonicalStepSample` — what the teacher
saw (references to the recorded fragments, never their content), what it
did (its executed actions, projected onto each action's declared params),
what it thought (when asked for), how the actions translate into the
student's action space, and the labels and provenance a dataset reader
needs. It is the format-neutral half of a sample: an exporter renders the
student's context from it and writes its own record format next to it.

Domain-neutral on purpose, like ``schemas/observation/``: a desktop or SWE
run produces the same shape. Nothing here names a page, an element or a
price — a translator's domain-specific provenance is an opaque
:attr:`TranslationRecord.payload` — and nothing imports
``spectragent.core`` (``test_shared_module_is_domain_neutral.py`` holds
both).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.schemas.action import Action

UntranslatableReason = Literal[
    "no_counterpart",
    "target_offscreen",
    "exceeds_action_budget",
    "occluded",
    "occlusion_unknown",
    "unrepresentable",
]
"""Why a teacher step has no sample in the student's action space.

``no_counterpart``: the student has no action that does this (or the
teacher's target is gone from the recorded state). ``target_offscreen``:
the target cannot be placed on the recorded screenshot.
``exceeds_action_budget``: more actions than the student's Loop runs per
step. ``occluded`` / ``occlusion_unknown``: the target point is covered,
or the run recorded no evidence to tell. ``unrepresentable``: the student's
reply format cannot write a param (``UnrepresentableActionError``).
"""

StepStatus = Literal["ok", "action_failed"]
"""``ok``: every executed action succeeded; ``action_failed``: at least one did not."""


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FragmentRef(_Record):
    """One environment-sourced fragment the source run recorded at the step.

    Attributes:
        key: The fragment's fully-qualified key (``browser.screenshot``).
        file: The fragment's file, as the trace manifest lists it (relative
            to the run directory).
        artifact: The fragment's artifact file as listed, or ``None``.
        artifact_sha256: sha256 of the artifact's bytes, or ``None``.
        failed: The capture failed; ``file`` holds the recorded failure.
    """

    key: str
    file: str
    artifact: str | None = None
    artifact_sha256: str | None = None
    failed: bool = False


class ImageRef(_Record):
    """The step's image: the fragment that reports screenshot geometry.

    Attributes:
        fragment: The fragment's fully-qualified key.
        sha256: sha256 of the image bytes — the name an exporter stores it under.
        media_type: Read from the bytes (``image/png``, ``image/jpeg``).
        image_width / image_height: The image's pixel size.
        viewport_width / viewport_height: The area it depicts, in the units
            the capability's point actions take.
    """

    fragment: str
    sha256: str
    media_type: str
    image_width: int
    image_height: int
    viewport_width: int
    viewport_height: int


class ThoughtRecord(_Record):
    """The teacher's thought, as its agent's ``parse_react_thought`` read it.

    Attributes:
        text: The thought.
        source_field: Where in the reply it came from (``Thought``,
            ``thinking``, ``memory``, ``reasoning`` …).
    """

    text: str
    source_field: str


class TranslationRecord(_Record):
    """A translator's provenance for one translated action.

    Attributes:
        translator_id: The translator's registry key.
        action_index: Position in the translated ``actions``.
        payload: The translator's own record, opaque to the sample.
    """

    translator_id: str
    action_index: int
    payload: dict[str, Any] = Field(default_factory=dict)


class Untranslatable(_Record):
    """Why the step has no target in the student's action space."""

    reason: UntranslatableReason
    detail: str = ""


class VerdictLabel(_Record):
    """One graded verdict of the run (a branch run: of its source run).

    Only the verdict itself: a reference answer never enters a sample.
    """

    verdict_slug: str
    evaluator_id: str
    predicted_label: int


class SampleLabels(_Record):
    """What a trainer may weight or filter by.

    Attributes:
        step_status: Whether the step's executed actions all succeeded.
        skipped_actions: How many of the target's last actions the teacher's
            Loop did not execute, because the action before them failed (so
            ``step_status`` is ``action_failed``). 0: every target action ran.
        loss_weight: The target turn's weight (earlier turns weigh 0).
        verdicts: Every graded verdict, in slug order.
    """

    step_status: StepStatus
    skipped_actions: int = Field(default=0, ge=0)
    loss_weight: float = 1.0
    verdicts: list[VerdictLabel] = Field(default_factory=list)


class SampleRedaction(_Record):
    """One replacement the source run's task-setup redaction made, copied.

    So a reader knows which recorded inputs were replaced, and by which
    rule version.
    """

    policy: str
    location: str
    field: str | None = None
    rule: str


class BranchSampleMeta(_Record):
    """Where a branch run's sample came from (a neutral record: no verdict, no preference).

    Attributes:
        branch_group_id: ``<source_run_id>/s<t>`` (``…/v<variant_id>`` for a
            task variant) — every branch given the same inputs at one
            checkpoint, and its source task, share it.
        source_run_id: The run the branch resumed.
        checkpoint_step: ``t``, the step the branch decided again.
        branch_index: Which branch of the group.
        terminal_step: The branch's terminal observation step, or ``None``
            when it recorded none.
        terminal_image_sha256: That observation's image, or ``None``.
        duplicate_of: The ``sample_id`` of an earlier sample of the group
            whose checkpoint-step target is identical — flagged, not removed.
    """

    branch_group_id: str
    source_run_id: str
    checkpoint_step: int
    branch_index: int
    terminal_step: int | None = None
    terminal_image_sha256: str | None = None
    duplicate_of: str | None = None


class RemovedAction(_Record):
    """One recorded action the export left out of a sample (``wait_actions: drop_when_capped``).

    Attributes:
        run_id: The run that recorded it (a branch's prefix step: its source run).
        step: The step it was recorded at.
        action_index: Its position in that step's recorded actions.
        action: The action, as recorded.
    """

    run_id: str
    step: int
    action_index: int
    action: Action


class RewriteModel(_Record):
    """The model that rewrote a thought, by its identity."""

    model_type: str
    provider: str
    model: str


ThoughtRewriteOutcome = Literal["rewritten", "reviewed_unchanged"]
"""``rewritten``: the model changed the thought (synthetic text);
``reviewed_unchanged``: it returned the thought as it was."""


class ThoughtRewrite(_Record):
    """How a thought affected by removed waits was rewritten (arch §4.15 "等待动作").

    Attributes:
        original: The teacher's thought, as its agent read it.
        rewritten: The thought the sample carries; equal to ``original`` when
            the model reviewed it and changed nothing, empty when it removed
            all of it (the sample then carries no thought).
        outcome: Whether the model changed it.
        synthetic: ``True`` exactly when ``outcome`` is ``rewritten``: the
            text is the model's, not the teacher's.
        model: The model's identity.
        prompt_version / prompt_digest: The versioned rewrite prompt and the
            digest of its text.
        cache_key: The rewrite cache's key for this rewrite.
    """

    original: str
    rewritten: str
    outcome: ThoughtRewriteOutcome
    synthetic: bool
    model: RewriteModel
    prompt_version: str
    prompt_digest: str
    cache_key: str

    @model_validator(mode="after")
    def _synthetic_iff_rewritten(self) -> ThoughtRewrite:
        if self.synthetic != (self.outcome == "rewritten"):
            raise ValueError("a thought rewrite is synthetic exactly when it was rewritten")
        if self.outcome == "reviewed_unchanged" and self.rewritten != self.original:
            raise ValueError("a thought reviewed unchanged carries its original text")
        return self


class WaitRemoval(_Record):
    """What ``wait_actions: drop_when_capped`` did to one sample (arch §4.15 "等待动作").

    Attributes:
        removed_actions: The waits removed from this step's own actions.
        removed_steps_before: The actions of the wait-only steps removed
            right before this step in its trajectory (they left the
            trajectory, so their observation and reply are not in this
            sample's context).
        thought: How the sample's thought was rewritten, or ``None`` when the
            step carries no teacher thought.
    """

    removed_actions: list[RemovedAction] = Field(default_factory=list)
    removed_steps_before: list[RemovedAction] = Field(default_factory=list)
    thought: ThoughtRewrite | None = None


class CanonicalStepSample(_Record):
    """One exportable step of one recorded run.

    Identity and source: ``sample_id`` (``<run_id>/s<step>``), ``run_id``,
    ``source_trace`` (the run directory relative to the export's sources
    root, POSIX), ``step``, the task's ``benchmark`` / ``subset`` /
    ``task_id``, the teacher's ``agent_id`` / ``loop_id`` / ``llm_model``,
    and the run's ``config_hash``.

    ``history_steps``: the steps of the same trajectory before this one that
    were exportable — for a branch run, the source run's ``0..t-1`` and then
    the branch's own policy steps before this one (a step number below
    ``branch.checkpoint_step`` names the source run's step).

    ``teacher_actions`` are the step's *executed* actions, fully qualified,
    as recorded, followed by the ones its Loop skipped because the last
    executed one failed (``labels.skipped_actions`` counts them): the reply
    the teacher gave. Their params were validated against each action's
    ``Params`` before any of them ran (``ReactLoop.check_action_params``), so
    they are what the action reads and nothing else. ``actions`` is their
    translation into the student's action space, or ``None`` with
    ``untranslatable`` saying why.

    ``wait_removal``: set only on a sample ``wait_actions: drop_when_capped``
    changed — waits removed from its step, wait-only steps removed right
    before it, its thought rewritten (arch §4.15 "等待动作"). ``teacher_actions``
    are then the reply without the removed waits, ``thought`` is the
    rewritten one and ``history_steps`` leaves the removed steps out.
    """

    sample_id: str
    run_id: str
    source_trace: str
    step: int
    benchmark: str = ""
    subset: str = ""
    task_id: str = ""
    agent_id: str = ""
    loop_id: str = ""
    llm_model: str = ""
    config_hash: str = ""
    task: str = ""
    observations: list[FragmentRef] = Field(default_factory=list)
    image: ImageRef | None = None
    history_steps: list[int] = Field(default_factory=list)
    teacher_actions: list[Action]
    thought: ThoughtRecord | None = None
    actions: list[Action] | None = None
    untranslatable: Untranslatable | None = None
    translation: list[TranslationRecord] = Field(default_factory=list)
    labels: SampleLabels
    redactions: list[SampleRedaction] = Field(default_factory=list)
    branch: BranchSampleMeta | None = None
    wait_removal: WaitRemoval | None = None

    def canonical_json(self) -> str:
        """The sample as JSON with sorted keys and fixed separators — byte-stable.

        Two equal samples write the same bytes whatever order their params
        were inserted in, which is what a reproducible export needs.
        """
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )


__all__ = [
    "BranchSampleMeta",
    "CanonicalStepSample",
    "FragmentRef",
    "ImageRef",
    "RemovedAction",
    "RewriteModel",
    "SampleLabels",
    "SampleRedaction",
    "StepStatus",
    "ThoughtRecord",
    "ThoughtRewrite",
    "ThoughtRewriteOutcome",
    "TranslationRecord",
    "Untranslatable",
    "UntranslatableReason",
    "VerdictLabel",
    "WaitRemoval",
]
