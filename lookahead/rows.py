"""The per-step row every command works from: what ``extract`` writes, one JSON object a line."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from spectragent.core.capabilities.browser.archive.frames import (
    FrameKind,
    FrameOutcome,
    FrameRecord,
    FrameSuperseder,
)
from spectragent.schemas.restoration import BranchTaskVariant
from spectragent.schemas.step_record import ActionSpan

#: What a run is, decided from its recorded configuration.
RunKind = Literal["record", "replay", "branch"]

#: The row format ``extract`` writes.
STEP_ROW_FORMAT = "lookahead-step-1"


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Settle(_Row):
    """How the capture of the observation waited for the page (the browser's settle record)."""

    outcome: str
    waited_ms: int
    waiting_for: list[str] = Field(default_factory=list)
    detail: str | None = None


class ObservationRef(_Row):
    """Where an observation is: the run's step, and what the trace says about its screenshot."""

    step: int
    origin: str
    screenshot: bool
    url: str | None = None
    captured_at: float | None = None
    image_width: int | None = None
    image_height: int | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None


class ActionRow(_Row):
    """One action the step executed and what its result recorded.

    ``effect`` is the action's recorded effect as JSON (``None``: none recorded),
    ``effect_text`` its one-line description, and ``facts`` the effect's most
    used facts flattened: the pointer's ``point`` and ``hit`` element, ``url_before``
    and ``url_after`` of a navigation, ``scroll_before`` / ``scroll_after``,
    ``focus_after`` and the typed ``text`` (from the parameters; effects never
    echo typed text).
    """

    index: int
    name: str
    params: dict[str, Any] = Field(default_factory=dict)
    success: bool | None = None
    error: str | None = None
    effect: dict[str, Any] | None = None
    effect_text: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)


class FrameRow(_Row):
    """One ``browser.frames`` record (its ``frames.json`` entry), with the step whose commit took it.

    ``batch_step`` is ``None`` for a trailing commit.
    """

    batch_step: int | None
    kind: FrameKind
    cycle: int
    action_index: int | None
    action: str | None
    tab: str
    requested_at: float
    captured_at: float | None
    outcome: FrameOutcome
    superseded_by: FrameSuperseder | None
    name: str | None
    sha256: str | None
    width: int | None
    height: int | None
    detail: str | None

    #: The reader's own record, handed back to the frames reader to read the image.
    _record: FrameRecord | None = PrivateAttr(default=None)

    @property
    def captured(self) -> bool:
        return self.outcome == "captured" and self.name is not None


class NextObservation(_Row):
    """The observation after the step's actions: the next committed step's, if it has one."""

    step: int | None
    exists: bool
    origin: str | None = None
    screenshot: bool = False
    url: str | None = None


class RestorationSummary(_Row):
    """A restoration check in brief: the verdict, why, and the measured distances."""

    verdict: str
    reason: str | None = None
    reasons: list[str] = Field(default_factory=list)
    screenshot_distance: int | None = None
    url_match: bool | None = None
    document_misses: int | None = None
    actions: int = 0
    failed_actions: int = 0


class BranchRestoration(_Row):
    """Every restoration check of one branch run, counted."""

    checkpoint: RestorationSummary | None = None
    restored_steps: int = 0
    verdicts: dict[str, int] = Field(default_factory=dict)
    first_problem_step: int | None = None


class BranchInfo(_Row):
    """Where a branch run came from, from its recorded provenance.

    ``source_run`` is the source run's key under the same viewer root, when the
    source is among the runs read (matched by its recorded run id); ``None``
    when it is not.
    """

    source_run_id: str
    source_trace: str
    source_run: str | None = None
    source_task: str | None = None
    checkpoint_step: int
    branch_index: int
    branch_group_id: str
    restoration: BranchRestoration


class RawDom(_Row):
    """Whether ``browser.dom_raw`` holds the raw DOM of the observation and of the next one."""

    current: bool
    next: bool


class StepRow(_Row):
    """One step that has an observation: O_t, the actions A_t, and where O_{t+1} is."""

    format: Literal["lookahead-step-1"] = STEP_ROW_FORMAT
    run: str
    run_id: str
    kind: RunKind
    site: str | None = None
    task: str
    task_variant: BranchTaskVariant | None = None
    step: int
    origin: str
    timestamp: float | None = None
    error_phase: str = ""
    error: str | None = None
    llm_output: str = ""
    observation: ObservationRef
    observation_cycle: int | None = None
    settle: Settle | None = None
    capture_state: dict[str, Any] | None = None
    actions: list[ActionRow] = Field(default_factory=list)
    action_spans: list[ActionSpan] | None = None
    frames: list[FrameRow] = Field(default_factory=list)
    next: NextObservation
    restoration: RestorationSummary | None = None
    branch: BranchInfo | None = None
    raw_dom: RawDom

    def frame(self, kind: str) -> FrameRow | None:
        """The frame of *kind* that follows this observation first.

        ``after_observation``: the delayed frame of this observation;
        ``before_action``: the frame taken right before the step's first action.
        """
        matching = [frame for frame in self.frames if frame.kind == kind]
        if not matching:
            return None
        return min(matching, key=lambda frame: frame.action_index or 0)

    def action_names(self) -> list[str]:
        return [action.name for action in self.actions]
