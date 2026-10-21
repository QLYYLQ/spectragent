"""``stability``: did the screen the agent saw stay the same while it thought?

For every observation with a screenshot, two comparisons against the frames
the ``browser.frames`` archiver took after it: the delayed frame of the same
observation (``after_observation``), and the frame taken right before the
step's first action (``before_action``). Each comparison is the restorer's
64-bit dHash distance plus the changed 32-px grid regions.

Verdicts, with ``limit`` the largest distance that still counts as the same
screen (default 5, the restorer's default):

- ``stable_through_think``: the before-action frame is within the limit (and
  the delayed frame too, when there is one);
- ``stable_short``: the delayed frame is within the limit; the before-action
  frame is not, or there is none;
- ``changed_short``: the delayed frame is past the limit;
- ``changed_before_action``: no delayed frame, and the before-action frame is
  past the limit;
- ``no_frames``: neither frame was captured.

A change whose box overlaps a media element that was playing in the
screenshot (its ``media`` reading) is flagged separately (``media_change``).
"""

from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lookahead import imaging
from lookahead.contracts import FRAMES_ARCHIVER_ID
from lookahead.extract import step_rows
from lookahead.queues import (
    Choice,
    ItemField,
    ItemLink,
    LabelSpec,
    QueueImage,
    QueueItem,
    RegionKind,
    RegionLabel,
    ReviewQueue,
    SuggestedRegion,
    Suggestion,
    archive_image,
    observation_image,
    write_queue,
)
from lookahead.report import crosstab, share, table
from lookahead.rows import FrameRow, StepRow
from lookahead.runs import Run, RunSet
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.flywheel.diagnostics import DifferenceMethod

Verdict = Literal[
    "stable_through_think", "stable_short", "changed_short", "changed_before_action", "no_frames"
]
VERDICTS: tuple[str, ...] = (
    "stable_through_think",
    "stable_short",
    "changed_short",
    "changed_before_action",
    "no_frames",
)
#: The previous-action label of a run's first observation.
START = "(start)"
NO_ACTION = "(no action)"
MACHINE_SOURCE = "lookahead-stability"
STABILITY_FORMAT = "lookahead-stability-1"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrameComparison(_Model):
    """The observation compared with one later frame."""

    kind: str
    batch_step: int | None
    name: str | None
    outcome: str
    action_index: int | None = None
    seconds_after: float | None = None
    distance: int | None = None
    compared: bool = False
    boxes: list[list[int]] = Field(default_factory=list)
    changed_pixels: int | None = None
    total_pixels: int | None = None
    reason: str | None = None


class ObservationStability(_Model):
    run: str
    step: int
    origin: str
    kind: str
    site: str | None = None
    url: str | None = None
    settle_outcome: str
    settle_waited_ms: int | None = None
    settle_waiting_for: list[str] = Field(default_factory=list)
    previous_action: str
    delayed: FrameComparison | None = None
    before_action: FrameComparison | None = None
    verdict: Verdict
    media_playing: list[str] = Field(default_factory=list)
    media_boxes: list[list[int]] = Field(default_factory=list)
    media_change: bool = False


class StabilityReport(_Model):
    format: Literal["lookahead-stability-1"] = STABILITY_FORMAT
    runs: int
    runs_with_frames: int
    max_distance: int
    method: DifferenceMethod
    inputs: dict[str, str]
    summary: dict[str, int]
    skipped_runs: dict[str, str]
    observations: list[ObservationStability]


def verdict(delayed: int | None, before: int | None, limit: int) -> Verdict:
    if delayed is None and before is None:
        return "no_frames"
    if delayed is not None and delayed > limit:
        return "changed_short"
    if before is not None and before <= limit:
        return "stable_through_think"
    if delayed is not None:
        return "stable_short"
    return "changed_before_action"


def _compare(
    run: Run,
    frame: FrameRow,
    observation: bytes,
    captured_at: float | None,
    method: DifferenceMethod,
) -> FrameComparison:
    result = FrameComparison(
        kind=frame.kind,
        batch_step=frame.batch_step,
        name=frame.name,
        outcome=frame.outcome,
        action_index=frame.action_index,
        seconds_after=(
            round(frame.captured_at - captured_at, 3)
            if frame.captured_at is not None and captured_at is not None
            else None
        ),
    )
    if not frame.captured:
        result.reason = f"frame not captured ({frame.outcome})"
        return result
    image = run.frame_image(frame)
    result.distance = imaging.distance(observation, image)
    diff = imaging.difference(observation, image, method)
    result.compared = diff.compared
    result.boxes = diff.boxes
    result.changed_pixels = diff.changed_pixels
    result.total_pixels = diff.total_pixels
    result.reason = diff.reason
    return result


def playing_media(shot: ScreenshotObs | None) -> tuple[list[str], list[list[int]]]:
    """Labels and image-pixel boxes of the media elements playing around the screenshot."""
    if shot is None or shot.media is None:
        return [], []
    scale_x = (
        shot.image_width / shot.viewport_width if shot.viewport_width and shot.image_width else 1.0
    )
    scale_y = (
        shot.image_height / shot.viewport_height
        if shot.viewport_height and shot.image_height
        else 1.0
    )
    labels: list[str] = []
    boxes: list[list[int]] = []
    for element in shot.media.elements:
        readings = [reading for reading in (element.before, element.after) if reading is not None]
        if not any(reading.paused is False and not reading.ended for reading in readings):
            continue
        rect = element.rect
        labels.append(element.label)
        boxes.append(
            [
                round(rect.x * scale_x),
                round(rect.y * scale_y),
                round((rect.x + rect.width) * scale_x),
                round((rect.y + rect.height) * scale_y),
            ]
        )
    return labels, boxes


def previous_action(run: Run, step: int) -> str:
    earlier = [other for other in run.steps() if other < step]
    if not earlier:
        return START
    record = run.records[earlier[-1]]
    return record.actions[-1].name if record.actions else NO_ACTION


def observation_stability(
    run: Run, row: StepRow, *, limit: int, method: DifferenceMethod
) -> ObservationStability | None:
    """The stability of one observation, or ``None`` when it has no screenshot image."""
    observation = run.screenshot_bytes(row.step) if row.observation.screenshot else None
    if observation is None:
        return None
    captured_at = row.observation.captured_at
    delayed_frame = row.frame("after_observation")
    before_frame = row.frame("before_action")
    delayed = (
        _compare(run, delayed_frame, observation, captured_at, method) if delayed_frame else None
    )
    before = _compare(run, before_frame, observation, captured_at, method) if before_frame else None
    labels, media_boxes = playing_media(run.screenshot(row.step))
    changed_boxes = [
        box
        for comparison in (delayed, before)
        if comparison is not None
        and comparison.distance is not None
        and comparison.distance > limit
        for box in comparison.boxes
    ]
    return ObservationStability(
        run=row.run,
        step=row.step,
        origin=row.origin,
        kind=row.kind,
        site=row.site,
        url=row.observation.url,
        settle_outcome=row.settle.outcome if row.settle is not None else "unrecorded",
        settle_waited_ms=row.settle.waited_ms if row.settle is not None else None,
        settle_waiting_for=list(row.settle.waiting_for) if row.settle is not None else [],
        previous_action=previous_action(run, row.step),
        delayed=delayed,
        before_action=before,
        verdict=verdict(
            delayed.distance if delayed is not None else None,
            before.distance if before is not None else None,
            limit,
        ),
        media_playing=labels,
        media_boxes=media_boxes,
        media_change=any(
            imaging.overlaps(box, media) for box in changed_boxes for media in media_boxes
        ),
    )


def analyse(runs: RunSet, *, limit: int, method: DifferenceMethod) -> list[ObservationStability]:
    found: list[ObservationStability] = []
    for run in runs.runs:
        for row in step_rows(run, runs):
            item = observation_stability(run, row, limit=limit, method=method)
            if item is not None:
                found.append(item)
    return found


# ── the review queue ──────────────────────────────────────────────────

CHOICES = [
    Choice(value="stable", text="Stable"),
    Choice(value="still_loading", text="Still loading"),
    Choice(value="animation_or_video", text="Animation or video"),
    Choice(value="other_change", text="Other change"),
    Choice(value="cannot_tell", text="Cannot tell"),
]
INSTRUCTIONS = (
    "The first image is the screen the agent was shown. The others are the same browser tab "
    "captured later, before the agent's next action could change it: about one second later, "
    "and right before the agent's first action (after it finished thinking). Did the screen "
    "stay the same? If not, say what kind of change it was, and draw boxes around changed "
    "areas you think matter. The suggested boxes come from a pixel difference."
)


def machine_choice(item: ObservationStability, limit: int) -> str:
    changed = any(
        comparison is not None and comparison.distance is not None and comparison.distance > limit
        for comparison in (item.delayed, item.before_action)
    )
    if not changed:
        return "stable" if item.verdict != "no_frames" else "cannot_tell"
    if item.media_change:
        return "animation_or_video"
    if item.settle_outcome == "capped":
        return "still_loading"
    return "other_change"


def _settle_text(item: ObservationStability) -> str:
    waited = f" after {item.settle_waited_ms} ms" if item.settle_waited_ms is not None else ""
    pending = (
        f", waiting for {', '.join(item.settle_waiting_for)}" if item.settle_waiting_for else ""
    )
    return f"{item.settle_outcome}{waited}{pending}"


def _distance_text(comparison: FrameComparison | None) -> str:
    if comparison is None:
        return "no frame"
    if comparison.distance is None:
        return comparison.reason or comparison.outcome
    later = f" ({comparison.seconds_after} s later)" if comparison.seconds_after is not None else ""
    return f"{comparison.distance}{later}, {len(comparison.boxes)} changed region(s)"


def queue_item(item: ObservationStability, limit: int) -> QueueItem:
    images: list[QueueImage] = [
        observation_image("observation", "What the agent saw", item.run, item.step)
    ]
    regions: list[SuggestedRegion] = []
    for role, text, comparison in (
        ("delayed", "About one second later", item.delayed),
        ("before_action", "Right before the first action", item.before_action),
    ):
        if comparison is None or comparison.distance is None or comparison.name is None:
            continue
        if comparison.batch_step is None:
            continue  # a trailing frame: no step to address it by
        images.append(
            archive_image(
                role, text, item.run, comparison.batch_step, FRAMES_ARCHIVER_ID, comparison.name
            )
        )
        if comparison.distance > limit:
            regions += [
                SuggestedRegion(image=role, kind="change", box=box) for box in comparison.boxes
            ]
    fields = [
        ItemField(text="Settle", value=_settle_text(item)),
        ItemField(text="Machine verdict", value=item.verdict),
        ItemField(text="Distance, one second later", value=_distance_text(item.delayed)),
        ItemField(text="Distance, before the action", value=_distance_text(item.before_action)),
        ItemField(text="Previous action", value=item.previous_action),
        ItemField(text="Playing media", value=", ".join(item.media_playing) or "none"),
        ItemField(text="URL", value=item.url or "unknown"),
    ]
    choice = machine_choice(item, limit)
    return QueueItem(
        item_id=f"{item.run}@{item.step}",
        title=f"{item.run} step {item.step}: {item.verdict}",
        run=item.run,
        step=item.step,
        images=images,
        fields=fields,
        links=[ItemLink(text="Run", run=item.run, step=item.step)],
        suggestion=Suggestion(
            choice=choice,
            regions=regions,
            source=MACHINE_SOURCE,
            rationale=(
                f"dHash distance limit {limit}; verdict {item.verdict}; settle {item.settle_outcome}"
                + ("; a changed region overlaps playing media" if item.media_change else "")
            ),
        ),
    )


def stratified(
    items: list[ObservationStability], size: int, seed: int
) -> list[ObservationStability]:
    """Up to *size* items, taken in turn from each (settle outcome, verdict) group."""
    rng = random.Random(seed)
    groups: dict[tuple[str, str], list[ObservationStability]] = {}
    for item in items:
        if item.verdict == "no_frames":
            continue
        groups.setdefault((item.settle_outcome, item.verdict), []).append(item)
    for members in groups.values():
        rng.shuffle(members)
    chosen: list[ObservationStability] = []
    order = sorted(groups)
    while len(chosen) < size and any(groups[key] for key in order):
        for key in order:
            if groups[key] and len(chosen) < size:
                chosen.append(groups[key].pop())
    return chosen


def build_queue(
    items: list[ObservationStability], *, queue_id: str, size: int, seed: int, limit: int
) -> ReviewQueue:
    return ReviewQueue(
        queue_id=queue_id,
        title="Does the observed screen stay the same while the agent thinks?",
        instructions=INSTRUCTIONS,
        label=LabelSpec(
            choices=CHOICES,
            multi=False,
            regions=RegionLabel(kinds=[RegionKind(value="change", text="Changed area")]),
            note=True,
        ),
        items=[queue_item(item, limit) for item in stratified(items, size, seed)],
    )


# ── the report ────────────────────────────────────────────────────────


def render_report(report: StabilityReport) -> str:
    items = report.observations
    total = len(items)
    with_frames = [item for item in items if item.verdict != "no_frames"]
    lines = [
        "# Observation stability",
        "",
        f"Observations with a screenshot: {total}. With at least one later frame: "
        f"{share(len(with_frames), total)}. Runs that recorded browser.frames: "
        f"{report.runs_with_frames} of {report.runs}.",
        f"A frame within dHash distance {report.max_distance} of the observation counts as the "
        "same screen. Changed regions: "
        f"{report.method.grid_size}-px grid, pixel threshold {report.method.pixel_threshold}, "
        f"cell fraction {report.method.cell_fraction}.",
        "",
        "## Verdicts",
        "",
        table(
            ["verdict", "observations"],
            [[name, sum(1 for item in items if item.verdict == name)] for name in VERDICTS],
        ),
        "",
        "## Settle outcome × verdict",
        "",
        crosstab(
            ((item.settle_outcome, item.verdict) for item in items),
            row_title="settle outcome",
            columns=VERDICTS,
        ),
        "",
        "## Last action of the previous step × verdict",
        "",
        crosstab(
            ((item.previous_action, item.verdict) for item in items),
            row_title="previous action",
            columns=VERDICTS,
        ),
        "",
        "## Changes over playing media",
        "",
        f"Observations with playing media: {sum(1 for item in items if item.media_playing)}. "
        f"Changed regions overlapping playing media: "
        f"{sum(1 for item in items if item.media_change)}.",
        "",
        crosstab(
            (
                ("media change" if item.media_change else "no media change", item.verdict)
                for item in items
            ),
            row_title="",
            columns=VERDICTS,
        ),
        "",
    ]
    if report.skipped_runs:
        lines += ["## Runs not read", ""]
        lines += [f"- `{key}`: {why}" for key, why in sorted(report.skipped_runs.items())]
        lines.append("")
    return "\n".join(lines)


def run_stability(
    runs: RunSet,
    out: Path,
    *,
    annotations: Path,
    queue_id: str,
    queue_size: int,
    seed: int,
    limit: int,
) -> tuple[StabilityReport, Path]:
    method = DifferenceMethod()
    items = analyse(runs, limit=limit, method=method)
    report = StabilityReport(
        runs=len(runs.runs),
        runs_with_frames=sum(1 for run in runs.runs if run.frames),
        max_distance=limit,
        method=method,
        inputs={"root": str(runs.root), "viewer_root": str(runs.viewer_root)},
        summary=dict(Counter(item.verdict for item in items)),
        skipped_runs=dict(runs.skipped),
        observations=items,
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "stability.json").write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out / "report.md").write_text(render_report(report), encoding="utf-8")
    queue = build_queue(items, queue_id=queue_id, size=queue_size, seed=seed, limit=limit)
    return report, write_queue(annotations, queue)
