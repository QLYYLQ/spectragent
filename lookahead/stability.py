"""``stability``: did the screen the agent saw stay the same while it thought?

For every observation with a screenshot (policy steps by default: a restored
step has no think time), two comparisons against the frames the
``browser.frames`` archiver took after it: the delayed frame of the same
observation (``after_observation``), and the frame taken right before the
step's first ``env.act`` call (``before_action``).

**A comparison is decided by the changed-region grid**, the method of
``spectragent flywheel diagnose`` (``DifferenceMethod``: a pixel changed when
its largest RGB difference exceeds 16; a 32-px cell is active when 1 % of its
pixels changed; four-neighbour cells form a region). It counts as *changed*
when one region's own cell area is at least ``min_change_area`` of the image
(default 0.5 %: a blinking caret or a ticking clock stays below it, a playing
preview does not). Every comparison reports the changed-pixel fraction and
every region with its area. The 64-bit dHash distance (the restorer's) is
reported beside it and decides nothing: it is blind to local changes, such as
a video preview that changed 6-8 % of the pixels at distance 0.

Verdicts:

- ``stable_through_think``: the before-action frame is unchanged (and the
  delayed one too, when there is one);
- ``stable_short``: the delayed frame is unchanged; the before-action frame
  changed, or there is none;
- ``changed_short``: the delayed frame changed;
- ``changed_before_action``: no delayed frame, and the before-action frame
  changed;
- ``no_frames``: no frame that could be compared.

A changed region overlapping a media element that was playing around the
screenshot (its ``media`` reading) is flagged separately (``media_change``),
whatever the verdict.
"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from lookahead import imaging
from lookahead.extract import step_rows
from lookahead.output import write_json, write_text
from lookahead.queues import frame_image, observation_image, region
from lookahead.report import crosstab, share, table
from lookahead.rows import FrameRow, StepRow
from lookahead.runs import Run, RunSet
from spectragent.annotation import (
    QUEUE_FORMAT,
    ItemField,
    ItemLink,
    LabelChoice,
    LabelSpec,
    QueueImage,
    QueueItem,
    Region,
    RegionKind,
    RegionSpec,
    ReviewQueue,
    Suggestion,
    write_queue,
)
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
#: Step origins that can have think time between observation and action.
ORIGINS = ("policy", "restored")
DEFAULT_ORIGINS = frozenset({"policy"})
#: A region at least this share of the image (its cells' own area) makes a change.
DEFAULT_MIN_CHANGE_AREA = 0.005
#: The restorer's default dHash limit: used only to say what the dHash alone would miss.
DHASH_LIMIT = 5
#: The previous-action label of a run's first observation.
START = "(start)"
NO_ACTION = "(no action)"
MACHINE_SOURCE = "lookahead-stability"
STABILITY_FORMAT = "lookahead-stability-2"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegionRow(_Model):
    """One changed region: its box (image pixels), its cells, its share of the image."""

    box: list[int]
    cells: int
    fraction: float
    significant: bool


class FrameComparison(_Model):
    """The observation compared with one later frame.

    ``changed`` is the grid's decision (``None``: nothing was compared, see
    ``reason``); ``dhash_distance`` is reported only.
    """

    kind: str
    batch_step: int | None
    name: str | None
    outcome: str
    action_index: int | None = None
    seconds_after: float | None = None
    compared: bool = False
    changed: bool | None = None
    changed_fraction: float | None = None
    largest_region_fraction: float | None = None
    regions: list[RegionRow] = Field(default_factory=list)
    changed_pixels: int | None = None
    total_pixels: int | None = None
    dhash_distance: int | None = None
    reason: str | None = None


class ObservationStability(_Model):
    run: str
    step: int
    origin: str
    kind: str
    site: str | None = None
    url: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    settle_outcome: str
    settle_waited_ms: int | None = None
    settle_waiting_for: list[str] = Field(default_factory=list)
    previous_action: str
    #: Seconds from the observation's capture to the start of the step's first
    #: ``env.act`` call (its action span); ``None`` when not measured.
    think_s: float | None = None
    delayed: FrameComparison | None = None
    before_action: FrameComparison | None = None
    verdict: Verdict
    media_playing: list[str] = Field(default_factory=list)
    media_boxes: list[list[int]] = Field(default_factory=list)
    media_change: bool = False


class StabilityReport(_Model):
    format: Literal["lookahead-stability-2"] = STABILITY_FORMAT
    runs: int
    runs_with_frames: int
    origins: list[str]
    min_change_area: float
    method: DifferenceMethod
    inputs: dict[str, str]
    summary: dict[str, int]
    skipped_runs: dict[str, str]
    observations: list[ObservationStability]


def verdict(delayed: bool | None, before: bool | None) -> Verdict:
    """The verdict from the two grid decisions (``None``: no comparable frame)."""
    if delayed is None and before is None:
        return "no_frames"
    if delayed:
        return "changed_short"
    if before is False:
        return "stable_through_think"
    if delayed is False:
        return "stable_short"
    return "changed_before_action"


def _compare(
    run: Run,
    frame: FrameRow,
    observation: bytes,
    captured_at: float | None,
    *,
    method: DifferenceMethod,
    min_change_area: float,
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
    result.dhash_distance = imaging.distance(observation, image)
    diff = imaging.difference(observation, image, method)
    result.reason = diff.reason
    if not diff.compared:
        return result
    significant = diff.significant(min_change_area)
    result.compared = True
    result.changed = bool(significant)
    result.changed_fraction = diff.changed_fraction
    result.largest_region_fraction = max((r.fraction for r in diff.regions), default=0.0)
    result.regions = [
        RegionRow(
            box=list(r.box),
            cells=r.cells,
            fraction=r.fraction,
            significant=r.fraction >= min_change_area,
        )
        for r in diff.regions
    ]
    result.changed_pixels = diff.changed_pixels
    result.total_pixels = diff.total_pixels
    return result


def playing_media(
    shot: ScreenshotObs | None, size: tuple[int, int] | None
) -> tuple[list[str], list[list[int]]]:
    """Labels and image-pixel boxes (clipped to the image) of the media playing around the screenshot."""
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
        box: list[float] = [
            rect.x * scale_x,
            rect.y * scale_y,
            (rect.x + rect.width) * scale_x,
            (rect.y + rect.height) * scale_y,
        ]
        clipped = imaging.clip(box, size) if size is not None else [round(v) for v in box]
        labels.append(element.label)
        if clipped is not None:
            boxes.append(list(clipped))
    return labels, boxes


def previous_action(run: Run, step: int) -> str:
    earlier = [other for other in run.steps() if other < step]
    if not earlier:
        return START
    record = run.records[earlier[-1]]
    return record.actions[-1].name if record.actions else NO_ACTION


def observation_stability(
    run: Run, row: StepRow, *, method: DifferenceMethod, min_change_area: float
) -> ObservationStability | None:
    """The stability of one observation, or ``None`` when it has no screenshot image."""
    observation = run.screenshot_bytes(row.step) if row.observation.screenshot else None
    if observation is None:
        return None
    captured_at = row.observation.captured_at
    decoded = imaging.decode(observation)
    size = decoded.size if decoded is not None else None
    comparisons: dict[str, FrameComparison | None] = {}
    for kind in ("after_observation", "before_action"):
        frame = row.frame(kind)
        comparisons[kind] = (
            _compare(
                run,
                frame,
                observation,
                captured_at,
                method=method,
                min_change_area=min_change_area,
            )
            if frame is not None
            else None
        )
    delayed, before = comparisons["after_observation"], comparisons["before_action"]
    labels, media_boxes = playing_media(run.screenshot(row.step), size)
    changed_boxes = [
        found.box
        for comparison in (delayed, before)
        if comparison is not None
        for found in comparison.regions
    ]
    spans = row.action_spans
    think = (
        round(spans[0].started_at - captured_at, 3) if spans and captured_at is not None else None
    )
    return ObservationStability(
        run=row.run,
        step=row.step,
        origin=row.origin,
        kind=row.kind,
        site=row.site,
        url=row.observation.url,
        image_width=size[0] if size is not None else None,
        image_height=size[1] if size is not None else None,
        settle_outcome=row.settle.outcome if row.settle is not None else "unrecorded",
        settle_waited_ms=row.settle.waited_ms if row.settle is not None else None,
        settle_waiting_for=list(row.settle.waiting_for) if row.settle is not None else [],
        previous_action=previous_action(run, row.step),
        think_s=think,
        delayed=delayed,
        before_action=before,
        verdict=verdict(
            delayed.changed if delayed is not None else None,
            before.changed if before is not None else None,
        ),
        media_playing=labels,
        media_boxes=media_boxes,
        media_change=any(
            imaging.overlaps(box, media) for box in changed_boxes for media in media_boxes
        ),
    )


def analyse(
    runs: RunSet,
    *,
    method: DifferenceMethod,
    min_change_area: float = DEFAULT_MIN_CHANGE_AREA,
    origins: frozenset[str] = DEFAULT_ORIGINS,
) -> list[ObservationStability]:
    """Every observation of the given origins; a run whose evidence does not read is skipped."""

    def of_run(run: Run) -> list[ObservationStability]:
        found: list[ObservationStability] = []
        for row in step_rows(run, runs):
            if row.origin not in origins:
                continue
            item = observation_stability(run, row, method=method, min_change_area=min_change_area)
            if item is not None:
                found.append(item)
        return found

    return [item for _, items in runs.each(of_run) for item in items]


# ── the review queue ──────────────────────────────────────────────────

CHOICES = [
    LabelChoice(value="stable", text="Stable"),
    LabelChoice(value="still_loading", text="Still loading"),
    LabelChoice(value="animation_or_video", text="Animation or video"),
    LabelChoice(value="other_change", text="Other change"),
    LabelChoice(value="cannot_tell", text="Cannot tell"),
]
INSTRUCTIONS = (
    "The first image is the screen the agent was shown. The others are the same browser tab "
    "captured later, before the agent's next action could change it: about one second later, "
    "and right before the agent's first action (after it finished thinking). Did the screen "
    "stay the same? If not, say what kind of change it was, and draw boxes around changed "
    "areas you think matter. The suggested boxes are every region a pixel grid found changed, "
    "small ones included."
)


def any_change(item: ObservationStability) -> bool:
    return any(
        comparison is not None and comparison.changed
        for comparison in (item.delayed, item.before_action)
    )


def machine_choice(item: ObservationStability) -> str:
    if item.verdict == "no_frames":
        return "cannot_tell"
    if not any_change(item):
        return "stable"
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


def _comparison_text(comparison: FrameComparison | None) -> str:
    if comparison is None:
        return "no frame"
    if not comparison.compared:
        return comparison.reason or comparison.outcome
    later = f" ({comparison.seconds_after} s later)" if comparison.seconds_after is not None else ""
    significant = sum(1 for found in comparison.regions if found.significant)
    return (
        f"{'changed' if comparison.changed else 'unchanged'}{later}: "
        f"{(comparison.changed_fraction or 0):.1%} of pixels, {len(comparison.regions)} region(s), "
        f"{significant} over the minimum, largest {(comparison.largest_region_fraction or 0):.1%}; "
        f"dHash distance {comparison.dhash_distance}"
    )


def queue_item(item: ObservationStability) -> QueueItem:
    images: list[QueueImage] = [
        observation_image("observation", "What the agent saw", item.run, item.step)
    ]
    regions: list[Region] = []
    for role, text, comparison in (
        ("delayed", "About one second later", item.delayed),
        ("before_action", "Right before the first action", item.before_action),
    ):
        if comparison is None or not comparison.compared or comparison.name is None:
            continue
        images.append(frame_image(role, text, item.run, comparison.batch_step, comparison.name))
        regions += [region(role, "change", found.box) for found in comparison.regions]
    fields = [
        ItemField(text="Settle", value=_settle_text(item)),
        ItemField(text="Machine verdict", value=item.verdict),
        ItemField(text="One second later", value=_comparison_text(item.delayed)),
        ItemField(text="Before the first action", value=_comparison_text(item.before_action)),
        ItemField(text="Previous action", value=item.previous_action),
        ItemField(
            text="Think time",
            value=f"{item.think_s} s" if item.think_s is not None else "not measured",
        ),
        ItemField(text="Playing media", value=", ".join(item.media_playing) or "none"),
        ItemField(text="URL", value=item.url or "unknown"),
    ]
    return QueueItem(
        item_id=f"{item.run}@{item.step}",
        title=f"{item.run} step {item.step}: {item.verdict}",
        run=item.run,
        step=item.step,
        images=images,
        fields=fields,
        links=[ItemLink(text="Run", run=item.run, step=item.step)],
        suggestion=Suggestion(
            choice=machine_choice(item),
            regions=regions,
            source=MACHINE_SOURCE,
            rationale=(
                f"grid verdict {item.verdict}; settle {item.settle_outcome}"
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
    items: list[ObservationStability], *, queue_id: str, size: int, seed: int
) -> ReviewQueue:
    return ReviewQueue(
        format=QUEUE_FORMAT,
        queue_id=queue_id,
        title="Does the observed screen stay the same while the agent thinks?",
        instructions=INSTRUCTIONS,
        label=LabelSpec(
            choices=CHOICES,
            multi=False,
            regions=RegionSpec(kinds=[RegionKind(value="change", text="Changed area")]),
            note=True,
        ),
        items=[queue_item(item) for item in stratified(items, size, seed)],
    )


# ── the report ────────────────────────────────────────────────────────


def render_report(report: StabilityReport) -> str:
    items = report.observations
    total = len(items)
    with_frames = [item for item in items if item.verdict != "no_frames"]
    comparisons = [
        comparison
        for item in items
        for comparison in (item.delayed, item.before_action)
        if comparison is not None and comparison.compared
    ]
    missed = sum(
        1
        for comparison in comparisons
        if comparison.changed
        and comparison.dhash_distance is not None
        and comparison.dhash_distance <= DHASH_LIMIT
    )
    flagged = sum(
        1
        for comparison in comparisons
        if not comparison.changed
        and comparison.dhash_distance is not None
        and comparison.dhash_distance > DHASH_LIMIT
    )
    lines = [
        "# Observation stability",
        "",
        f"Observations with a screenshot ({', '.join(report.origins)} steps): {total}. With a "
        f"comparable later frame: {share(len(with_frames), total)}. Runs that recorded "
        f"browser.frames: {report.runs_with_frames} of {report.runs}.",
        f"A comparison is changed when one changed region covers at least "
        f"{report.min_change_area:.2%} of the image ({report.method.grid_size}-px grid, pixel "
        f"threshold {report.method.pixel_threshold}, cell fraction {report.method.cell_fraction}). "
        "The dHash distance is reported only.",
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
    ]
    if len(report.origins) > 1:
        lines += [
            "## Step origin × verdict",
            "",
            crosstab(
                ((item.origin, item.verdict) for item in items),
                row_title="origin",
                columns=VERDICTS,
            ),
            "",
        ]
    lines += [
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
        "## What the dHash alone would say",
        "",
        f"Of {len(comparisons)} comparisons, the grid found {missed} changed at a dHash distance "
        f"of {DHASH_LIMIT} or less, and {flagged} unchanged at a distance over {DHASH_LIMIT}.",
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
    min_change_area: float = DEFAULT_MIN_CHANGE_AREA,
    origins: frozenset[str] = DEFAULT_ORIGINS,
) -> tuple[StabilityReport, Path]:
    if not 0 < min_change_area < 1:
        raise ValueError("--min-change-area is a share of the image, between 0 and 1")
    unknown = sorted(origins - set(ORIGINS))
    if unknown or not origins:
        raise ValueError(f"--origins takes {', '.join(ORIGINS)}; got {sorted(origins)}")
    method = DifferenceMethod()
    items = analyse(runs, method=method, min_change_area=min_change_area, origins=origins)
    report = StabilityReport(
        runs=len(runs.runs),
        runs_with_frames=sum(1 for run in runs.runs if run.key not in runs.skipped and run.frames),
        origins=[origin for origin in ORIGINS if origin in origins],
        min_change_area=min_change_area,
        method=method,
        inputs={"root": str(runs.root), "viewer_root": str(runs.viewer_root)},
        summary=dict(Counter(item.verdict for item in items)),
        skipped_runs=dict(runs.skipped),
        observations=items,
    )
    queue = build_queue(items, queue_id=queue_id, size=queue_size, seed=seed)
    write_json(out / "stability.json", report.model_dump(mode="json"))
    write_text(out / "report.md", render_report(report))
    return report, write_queue(annotations, queue)
