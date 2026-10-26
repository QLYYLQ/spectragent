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

**Media areas** (``--media-areas DIR``, a replay rule set directory). The
media-area rules restoration verification applies
(``spectragent/core/capabilities/browser/replay/media_areas.py``) declare
video and search pages. When one declares the observation's page (its
screenshot's ``source_url``, matched against itself: frames record no page
URL, so a frame is taken to show the observation's page), the media boxes
measured on the observation's screenshot — every ``<video>`` / ``<audio>``
element and every embedded frame the media read could not reach — are masked
out of the observation and of each frame before the grid and the dHash
compare them, by the browser's own ``media_mask.py``; past its
``MAX_MASKED_SHARE`` nothing is masked and the whole images are compared.
The unmasked comparisons stay as they are; the masked ones decide the
verdict when the mask was applied. A screenshot without ``source_url`` or
without measured media facts is not masked, and is counted as such.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from lookahead import imaging
from lookahead.extract import step_rows
from lookahead.output import write_json, write_text
from lookahead.queues import frame_image, item_title, observation_image, region
from lookahead.report import crosstab, excluded_section, share, table
from lookahead.rows import FrameRow, StepRow
from lookahead.runs import LookaheadInputError, Run, RunSet
from spectragent.annotation import (
    QUEUE_FORMAT,
    GuideSection,
    ItemField,
    LabelChoice,
    LabelSpec,
    QueueImage,
    QueueItem,
    Region,
    RegionKind,
    RegionSpec,
    ReviewQueue,
    Suggestion,
    SuggestionDisplay,
    write_queue,
)
from spectragent.core.capabilities.browser.media_mask import (
    MAX_MASKED_SHARE,
    MediaBox,
    image_box,
    mask_image,
    screenshot_media_boxes,
)
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.capabilities.browser.replay.media_areas import (
    MediaAreaRule,
    media_area_rule_ids,
)
from spectragent.core.capabilities.browser.replay.site_rules import SiteRuleError, SiteRules
from spectragent.flywheel.diagnostics import DifferenceMethod
from spectragent.inspection.marks import action_lines, action_text
from spectragent.schemas.restoration import MaskedAreaKind

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
#: Whether an observation's media areas were masked (``--media-areas``), and why not.
MediaAreaStatus = Literal[
    "masked", "mask_too_large", "no_page_url", "no_media_facts", "undeclared", "no_image"
]
MEDIA_AREA_STATUSES: tuple[str, ...] = (
    "masked",
    "mask_too_large",
    "no_page_url",
    "no_media_facts",
    "undeclared",
    "no_image",
)
#: The two frames an observation is compared with, in the order they follow it.
FRAME_KINDS = ("after_observation", "before_action")
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
STABILITY_FORMAT = "lookahead-stability-4"


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


class MaskedBox(_Model):
    """One media box masked out: what it is, and the pixels painted (image pixels)."""

    kind: MaskedAreaKind
    element: str
    box: list[int]


class MediaAreas(_Model):
    """The observation's media areas, with ``--media-areas``.

    ``status``: ``masked``; ``mask_too_large`` (the boxes cover more than
    ``MAX_MASKED_SHARE`` of the screenshot, so the whole images are
    compared); or why nothing was masked: ``no_page_url`` (the screenshot
    has no ``source_url`` to match), ``no_media_facts`` (it carries no
    measured media facts, so its boxes are unknown), ``undeclared`` (no rule
    declares the page), ``no_image`` (the screenshot does not decode).
    ``rule_ids``: the rules whose scope matches the page URL. ``boxes`` and
    ``masked_share`` are measured on the observation's screenshot.
    ``frames_on_other_tab``: compared frames taken on another tab than the
    observation's delayed frame, so on another page. ``delayed`` /
    ``before_action`` / ``verdict``: the comparisons and the verdict with the
    mask, only when it was applied; ``media_change``: one of their changed
    regions overlaps playing media.
    """

    status: MediaAreaStatus
    rule_ids: list[str] = Field(default_factory=list)
    mask_applied: bool = False
    masked_share: float | None = None
    boxes: list[MaskedBox] = Field(default_factory=list)
    detail: str = ""
    frames_on_other_tab: list[str] = Field(default_factory=list)
    delayed: FrameComparison | None = None
    before_action: FrameComparison | None = None
    verdict: Verdict | None = None
    media_change: bool = False


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
    #: The comparisons of the whole images.
    delayed: FrameComparison | None = None
    before_action: FrameComparison | None = None
    #: With the media areas masked when the mask was applied, else ``unmasked_verdict``.
    verdict: Verdict
    unmasked_verdict: Verdict
    media_playing: list[str] = Field(default_factory=list)
    media_boxes: list[list[int]] = Field(default_factory=list)
    media_change: bool = False
    #: ``None`` without ``--media-areas``.
    media_areas: MediaAreas | None = None

    @property
    def masked(self) -> bool:
        """Whether the verdict was decided with the media areas masked."""
        return self.media_areas is not None and self.media_areas.mask_applied

    def deciding_comparisons(self) -> tuple[FrameComparison | None, FrameComparison | None]:
        """The delayed and before-action comparisons the verdict was decided by."""
        if self.media_areas is not None and self.media_areas.mask_applied:
            return self.media_areas.delayed, self.media_areas.before_action
        return self.delayed, self.before_action


class MediaAreaRuleSet(_Model):
    """The replay rule set ``--media-areas`` read: where, its digest, its media-area rules."""

    directory: str | None
    sha256: str
    rules: list[MediaAreaRule]


class StabilityReport(_Model):
    format: Literal["lookahead-stability-4"] = STABILITY_FORMAT
    runs: int
    runs_with_frames: int
    origins: list[str]
    min_change_area: float
    method: DifferenceMethod
    inputs: dict[str, str]
    summary: dict[str, int]
    skipped_runs: dict[str, str]
    #: Runs an exclusions file left out (``--exclude-runs``), and why.
    excluded_runs: dict[str, str]
    #: ``None`` without ``--media-areas``.
    media_areas: MediaAreaRuleSet | None = None
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


def _frame_comparison(frame: FrameRow, captured_at: float | None) -> FrameComparison:
    """What is known of a frame before it is compared."""
    return FrameComparison(
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


def _decide(
    result: FrameComparison, diff: imaging.Difference, min_change_area: float
) -> FrameComparison:
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


def _compare(
    frame: FrameRow,
    image: bytes | None,
    observation: bytes,
    captured_at: float | None,
    *,
    method: DifferenceMethod,
    min_change_area: float,
) -> FrameComparison:
    """The observation against one frame (*image*: its PNG, ``None`` when not captured)."""
    result = _frame_comparison(frame, captured_at)
    if image is None:
        result.reason = f"frame not captured ({frame.outcome})"
        return result
    result.dhash_distance = imaging.distance(observation, image)
    return _decide(result, imaging.difference(observation, image, method), min_change_area)


def _compare_masked(
    frame: FrameRow,
    image: bytes | None,
    observation: Image.Image,
    mask: Sequence[MediaBox],
    viewport: tuple[int, int],
    captured_at: float | None,
    *,
    method: DifferenceMethod,
    min_change_area: float,
) -> FrameComparison:
    """:func:`_compare` with *mask* painted on the frame; *observation* is already masked."""
    result = _frame_comparison(frame, captured_at)
    if image is None:
        result.reason = f"frame not captured ({frame.outcome})"
        return result
    decoded = imaging.decode(image)
    if decoded is None:
        result.reason = "an image does not decode"
        return result
    masked, _ = mask_image(decoded, mask, viewport)
    result.dhash_distance = imaging.image_distance(observation, masked)
    return _decide(result, imaging.image_difference(observation, masked, method), min_change_area)


def _changed_boxes(comparisons: Sequence[FrameComparison | None]) -> list[list[int]]:
    return [
        found.box
        for comparison in comparisons
        if comparison is not None
        for found in comparison.regions
    ]


def _overlap(changed: list[list[int]], media: list[list[int]]) -> bool:
    return any(imaging.overlaps(box, other) for box in changed for other in media)


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


def previous_action_text(run: Run, step: int) -> str:
    """The previous step's last action as a person reads it."""
    earlier = [other for other in run.steps() if other < step]
    if not earlier:
        return "（这是第一步）"
    record = run.records[earlier[-1]]
    if not record.actions:
        return "（没有动作）"
    index = len(record.actions) - 1
    result = record.action_results[index] if index < len(record.action_results) else None
    return action_text(record.actions[index], result)


def media_area_mask(
    shot: ScreenshotObs | None,
    observation: Image.Image | None,
    rules: Sequence[MediaAreaRule],
    frames: dict[str, FrameRow | None],
    images: dict[str, bytes | None],
    *,
    captured_at: float | None,
    playing: list[list[int]],
    method: DifferenceMethod,
    min_change_area: float,
) -> MediaAreas:
    """The observation's media areas under *rules*, and the comparisons with them masked.

    The page URL is the screenshot's ``source_url``, matched against itself
    (``media_area_rule_ids``: both sides are the observation's page). The
    boxes are the ones the observation's screenshot measured; the same mask,
    scaled from its viewport, is painted on the observation and on each
    frame (*images*: their PNGs, ``None`` when not captured).
    """
    url = shot.source_url if shot is not None else None
    if shot is None or not url:
        return MediaAreas(status="no_page_url")
    rule_ids = list(media_area_rule_ids(url, url, rules))
    boxes, note = screenshot_media_boxes(shot, "observation")
    notes = [note] if note is not None else []
    if shot.media is None or not shot.media.known:
        return MediaAreas(status="no_media_facts", rule_ids=rule_ids, detail="; ".join(notes))
    if not rule_ids:
        return MediaAreas(status="undeclared")
    if observation is None:
        return MediaAreas(
            status="no_image",
            rule_ids=rule_ids,
            detail="the observation screenshot does not decode",
        )
    viewport = (shot.viewport_width, shot.viewport_height)
    masked_observation, share = mask_image(observation, boxes, viewport)
    share = round(share, 4)
    painted = [
        MaskedBox(kind=box.kind, element=box.element, box=list(pixels))
        for box in boxes
        if (pixels := image_box(box, observation.size, viewport)) is not None
    ]
    delayed_frame = frames["after_observation"]
    other_tab = [
        kind
        for kind, frame in frames.items()
        if frame is not None and delayed_frame is not None and frame.tab != delayed_frame.tab
    ]
    if share > MAX_MASKED_SHARE:
        notes.append(
            f"the media areas cover {share:.0%} of the screenshot, more than "
            f"{MAX_MASKED_SHARE:.0%}: the whole images are compared"
        )
        return MediaAreas(
            status="mask_too_large",
            rule_ids=rule_ids,
            masked_share=share,
            boxes=painted,
            detail="; ".join(notes),
            frames_on_other_tab=other_tab,
        )
    masked = {
        kind: _compare_masked(
            frame,
            images[kind],
            masked_observation,
            boxes,
            viewport,
            captured_at,
            method=method,
            min_change_area=min_change_area,
        )
        if frame is not None
        else None
        for kind, frame in frames.items()
    }
    delayed, before = masked["after_observation"], masked["before_action"]
    return MediaAreas(
        status="masked",
        rule_ids=rule_ids,
        mask_applied=True,
        masked_share=share,
        boxes=painted,
        detail="; ".join(notes),
        frames_on_other_tab=other_tab,
        delayed=delayed,
        before_action=before,
        verdict=verdict(
            delayed.changed if delayed is not None else None,
            before.changed if before is not None else None,
        ),
        media_change=_overlap(_changed_boxes((delayed, before)), playing),
    )


def observation_stability(
    run: Run,
    row: StepRow,
    *,
    method: DifferenceMethod,
    min_change_area: float,
    media_area_rules: Sequence[MediaAreaRule] | None = None,
) -> ObservationStability | None:
    """The stability of one observation, or ``None`` when it has no screenshot image.

    *media_area_rules*: ``--media-areas``'s rules (``None``: not given).
    """
    observation = run.screenshot_bytes(row.step) if row.observation.screenshot else None
    if observation is None:
        return None
    captured_at = row.observation.captured_at
    decoded = imaging.decode(observation)
    size = decoded.size if decoded is not None else None
    frames = {kind: row.frame(kind) for kind in FRAME_KINDS}
    images = {
        kind: run.frame_image(frame) if frame is not None and frame.captured else None
        for kind, frame in frames.items()
    }
    comparisons = {
        kind: _compare(
            frame,
            images[kind],
            observation,
            captured_at,
            method=method,
            min_change_area=min_change_area,
        )
        if frame is not None
        else None
        for kind, frame in frames.items()
    }
    delayed, before = comparisons["after_observation"], comparisons["before_action"]
    shot = run.screenshot(row.step)
    labels, media_boxes = playing_media(shot, size)
    unmasked = verdict(
        delayed.changed if delayed is not None else None,
        before.changed if before is not None else None,
    )
    areas = (
        media_area_mask(
            shot,
            decoded,
            media_area_rules,
            frames,
            images,
            captured_at=captured_at,
            playing=media_boxes,
            method=method,
            min_change_area=min_change_area,
        )
        if media_area_rules is not None
        else None
    )
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
        verdict=areas.verdict if areas is not None and areas.verdict is not None else unmasked,
        unmasked_verdict=unmasked,
        media_playing=labels,
        media_boxes=media_boxes,
        media_change=_overlap(_changed_boxes((delayed, before)), media_boxes),
        media_areas=areas,
    )


def analyse(
    runs: RunSet,
    *,
    method: DifferenceMethod,
    min_change_area: float = DEFAULT_MIN_CHANGE_AREA,
    origins: frozenset[str] = DEFAULT_ORIGINS,
    media_area_rules: Sequence[MediaAreaRule] | None = None,
) -> list[ObservationStability]:
    """Every observation of the given origins; a run whose evidence does not read is skipped."""

    def of_run(run: Run) -> list[ObservationStability]:
        found: list[ObservationStability] = []
        for row in step_rows(run, runs):
            if row.origin not in origins:
                continue
            item = observation_stability(
                run,
                row,
                method=method,
                min_change_area=min_change_area,
                media_area_rules=media_area_rules,
            )
            if item is not None:
                found.append(item)
        return found

    return [item for _, items in runs.each(of_run) for item in items]


# ── the review queue ──────────────────────────────────────────────────

CHOICES = [
    LabelChoice(
        value="stable",
        text="稳定",
        help="三张图实质上一样。光标闪烁、时钟秒数跳动这类小变化不算。",
        regions="forbidden",
    ),
    LabelChoice(
        value="still_loading",
        text="还在加载",
        help="第一张图没加载完（空白、灰色占位条、转圈、列表或图片缺失），后面的图里才补全。",
        regions="optional",
    ),
    LabelChoice(
        value="animation_or_video",
        text="页面自动变化",
        help="页面自己在动：视频在播放、轮播图在切换、动态预览、股票行情在刷新。"
        "行情数字自动刷新也归到这一类。",
        regions="optional",
    ),
    LabelChoice(
        value="other_change",
        text="其他变化",
        help="弹窗或遮罩出现或消失、搜索联想下拉框、插入的广告等。",
        regions="optional",
    ),
    LabelChoice(
        value="cannot_tell",
        text="看不出",
        help="图片看不清或不完整，判断不了；请在备注里说明。",
        regions="optional",
    ),
]
CHANGE = RegionKind(
    value="change",
    text="变化区域",
    help="和“智能体看到的”相比，后面的图里变了的地方。画在变了的那张图上。",
)
TITLE = "智能体思考时，屏幕有没有变"
INSTRUCTIONS = (
    "第一张图是智能体看到的画面，后面是同一个页面稍后的画面：约 1 秒后，以及智能体想完、"
    "刚要执行第一个动作之前。请判断画面有没有变；变了的话选出是哪一类变化，"
    "并在变了的那张图上框出重要的变化区域。机器建议的框（虚线）只包括面积超过阈值的变化区域。"
)
GUIDE = [
    GuideSection(
        title="三张图分别是什么",
        text=(
            "- 智能体看到的：交给智能体做决定的那张截图；上面的编号标记是它在这一屏上做的动作。\n"
            "- 约 1 秒后：同一个页面大约 1 秒后的样子，智能体的动作还没开始。\n"
            "- 第一个动作之前：智能体想完、刚要执行第一个动作时的样子；上面的标记是这个动作。\n"
            "- 有的条目只有其中两张图。"
        ),
    ),
    GuideSection(
        title="怎么判断",
        text=(
            "- 拿后面的图和第一张比。对比方式切到“差异”可以看出哪里变了：黑色表示没变。\n"
            "- 只有光标闪烁、时钟秒数这类很小的变化：选“稳定”。\n"
            "- 第一张图明显没加载完，后面补全了：选“还在加载”。\n"
            "- 视频、轮播图、动态预览、行情数字在自己变：选“页面自动变化”。\n"
            "- 弹窗、遮罩、联想下拉框、广告出现或消失：选“其他变化”。"
        ),
    ),
    GuideSection(
        title="框怎么画",
        text=(
            "- 选“稳定”时不能画框。\n"
            "- 其他选项可以画框：框出你认为重要的变化区域，画在变了的那张图上。\n"
            "- 虚线框是机器建议，可以按 E 复制过来再改，或按 A 直接接受。"
        ),
    ),
]
SUGGESTION_TITLE = "机器建议（由像素比较得出，仅供参考）"
VERDICT_TEXT = {
    "stable_through_think": "一直没变（到第一个动作之前都没变）",
    "stable_short": "约 1 秒内没变；第一个动作之前有变化或没有那张图",
    "changed_short": "约 1 秒后已经变了",
    "changed_before_action": "第一个动作之前变了（没有 1 秒后的图）",
    "no_frames": "没有可比较的图",
}
SETTLE_TEXT = {"settled": "页面已稳定", "capped": "等到上限仍未稳定", "unknown": "不知道是否稳定"}
WAITING_TEXT = {"load": "页面加载完成", "dom": "页面结构不再变化"}


def _changed(comparisons: Sequence[FrameComparison | None]) -> bool:
    return any(comparison is not None and comparison.changed for comparison in comparisons)


def any_change(item: ObservationStability) -> bool:
    """Whether a comparison the verdict was decided by changed."""
    return _changed(item.deciding_comparisons())


def deciding_media_change(item: ObservationStability) -> bool:
    """Whether a changed region of the comparisons the verdict was decided by overlaps playing media."""
    if item.media_areas is not None and item.media_areas.mask_applied:
        return item.media_areas.media_change
    return item.media_change


def machine_choice(item: ObservationStability) -> str:
    if item.verdict == "no_frames":
        return "cannot_tell"
    if not any_change(item):
        return "stable"
    if deciding_media_change(item):
        return "animation_or_video"
    if item.settle_outcome == "capped":
        return "still_loading"
    return "other_change"


def _settle_text(item: ObservationStability) -> str:
    if item.settle_outcome == "unrecorded":
        return "没有记录"
    head = SETTLE_TEXT.get(item.settle_outcome, item.settle_outcome)
    waited = (
        f"（等了 {item.settle_waited_ms / 1000:.1f} 秒）"
        if item.settle_waited_ms is not None
        else ""
    )
    pending = (
        "，还在等：" + "、".join(WAITING_TEXT.get(name, name) for name in item.settle_waiting_for)
        if item.settle_waiting_for
        else ""
    )
    return f"{head}{waited}{pending}"


def _comparison_text(comparison: FrameComparison | None, min_change_area: float) -> str:
    """How one later frame compares, in words a person reads."""
    if comparison is None:
        return "没有这张图"
    if not comparison.compared:
        return "这张图没法比较"
    later = (
        f"（{comparison.seconds_after:.1f} 秒后）" if comparison.seconds_after is not None else ""
    )
    significant = sum(1 for found in comparison.regions if found.significant)
    if not comparison.changed:
        return f"没有明显变化{later}"
    return (
        f"有变化{later}：{(comparison.changed_fraction or 0):.1%} 的像素变了，"
        f"{significant} 处变化超过 {min_change_area:.1%} 的面积阈值，"
        f"最大一处占画面 {(comparison.largest_region_fraction or 0):.1%}"
    )


def _comparison_detail(comparison: FrameComparison | None) -> str:
    """The same comparison's machine facts, for the technical details."""
    if comparison is None:
        return "no frame"
    if not comparison.compared:
        return comparison.reason or comparison.outcome
    return (
        f"{comparison.name}: changed={comparison.changed}, "
        f"{len(comparison.regions)} grid region(s), dHash distance {comparison.dhash_distance}"
    )


def _media_area_text(areas: MediaAreas | None, unmasked: Verdict) -> str | None:
    """What the media areas did to this item's comparison, in words a person reads; ``None``: nothing."""
    if areas is None or not areas.rule_ids:
        return None
    rules = "、".join(areas.rule_ids)
    if areas.status == "masked":
        share = f"{areas.masked_share:.0%}" if areas.masked_share is not None else "未知"
        return (
            f"已遮掉（规则 {rules}）：截图记录的 {len(areas.boxes)} 处视频、音频或读不到的内嵌页面，"
            f"共占画面 {share}，在每张图上都涂黑之后再比较，这些地方的变化不算。"
            f"不遮时的判断：{VERDICT_TEXT.get(unmasked, unmasked)}"
        )
    if areas.status == "mask_too_large":
        share = f"{areas.masked_share:.0%}" if areas.masked_share is not None else "未知"
        return (
            f"没有遮（规则 {rules}）：媒体区域占画面 {share}，超过 {MAX_MASKED_SHARE:.0%}，"
            "比较的是整张图"
        )
    if areas.status == "no_media_facts":
        return f"没有遮（规则 {rules}）：截图没有记录媒体状态，不知道视频在哪里"
    return None


def queue_item(
    item: ObservationStability,
    run: Run | None = None,
    *,
    min_change_area: float = DEFAULT_MIN_CHANGE_AREA,
) -> QueueItem:
    images: list[QueueImage] = [
        observation_image("observation", "智能体看到的", item.run, item.step)
    ]
    regions: list[Region] = []
    delayed, before = item.deciding_comparisons()
    for role, text, comparison in (
        ("delayed", "约 1 秒后", delayed),
        ("before_action", "第一个动作之前", before),
    ):
        if comparison is None or not comparison.compared or comparison.name is None:
            continue
        images.append(frame_image(role, text, item.run, comparison.batch_step, comparison.name))
        # Only the regions that count as a change: the grid's small cells
        # (a caret, a clock) are noise to a person.
        regions += [
            region(role, "change", found.box) for found in comparison.regions if found.significant
        ]
    record = run.records.get(item.step) if run is not None else None
    media_area_text = _media_area_text(item.media_areas, item.unmasked_verdict)
    fields = [
        ItemField(text="机器判断", value=VERDICT_TEXT.get(item.verdict, item.verdict)),
        ItemField(text="约 1 秒后", value=_comparison_text(delayed, min_change_area)),
        ItemField(text="第一个动作之前", value=_comparison_text(before, min_change_area)),
    ]
    if media_area_text is not None:
        fields.append(ItemField(text="媒体区域", value=media_area_text))
    fields += [
        ItemField(text="截图前等页面稳定", value=_settle_text(item)),
        ItemField(
            text="智能体思考了",
            value=f"{item.think_s:.1f} 秒" if item.think_s is not None else "没有测量",
        ),
        ItemField(
            text="上一步的最后一个动作",
            value=previous_action_text(run, item.step) if run is not None else item.previous_action,
        ),
        ItemField(text="这一步的动作", value=action_lines(record) if record else "未记录"),
        ItemField(text="正在播放的媒体", value="、".join(item.media_playing) or "无"),
        ItemField(text="网址", value=item.url or "未记录", technical=True),
        ItemField(text="机器判定（原值）", value=item.verdict, technical=True),
        ItemField(text="约 1 秒后（细节）", value=_comparison_detail(item.delayed), technical=True),
        ItemField(
            text="第一个动作之前（细节）",
            value=_comparison_detail(item.before_action),
            technical=True,
        ),
    ]
    if item.masked:
        fields.append(
            ItemField(
                text="遮掉媒体区域后（细节）",
                value=f"after_observation: {_comparison_detail(delayed)}; "
                f"before_action: {_comparison_detail(before)}",
                technical=True,
            )
        )
    fields += [
        ItemField(
            text="稳定等待（原值）",
            value=f"{item.settle_outcome}, waited {item.settle_waited_ms} ms, "
            f"waiting for {item.settle_waiting_for}",
            technical=True,
        ),
        ItemField(text="运行", value=item.run, technical=True),
    ]
    return QueueItem(
        item_id=f"{item.run}@{item.step}",
        title=item_title(
            item.url,
            run.start_url if run is not None else None,
            run.task_id if run is not None else None,
            f"第 {item.step} 步",
        ),
        run=item.run,
        step=item.step,
        images=images,
        fields=fields,
        suggestion=Suggestion(
            choice=machine_choice(item),
            regions=regions,
            source=MACHINE_SOURCE,
            rationale=(
                f"像素比较：{VERDICT_TEXT.get(item.verdict, item.verdict)}；"
                f"截图前{SETTLE_TEXT.get(item.settle_outcome, item.settle_outcome)}"
                + ("；变化区域和正在播放的媒体重叠" if deciding_media_change(item) else "")
                + (
                    f"；已遮掉媒体区域（规则 {'、'.join(item.media_areas.rule_ids)}）"
                    if item.media_areas is not None and item.masked
                    else ""
                )
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
    items: list[ObservationStability],
    *,
    queue_id: str,
    size: int,
    seed: int,
    runs: dict[str, Run] | None = None,
    min_change_area: float = DEFAULT_MIN_CHANGE_AREA,
) -> ReviewQueue:
    runs = runs or {}
    return ReviewQueue(
        format=QUEUE_FORMAT,
        queue_id=queue_id,
        title=TITLE,
        instructions=INSTRUCTIONS,
        guide=GUIDE,
        suggestion_display=SuggestionDisplay(title=SUGGESTION_TITLE),
        label=LabelSpec(
            choices=CHOICES,
            multi=False,
            regions=RegionSpec(kinds=[CHANGE]),
            note=True,
        ),
        items=[
            queue_item(item, runs.get(item.run), min_change_area=min_change_area)
            for item in stratified(items, size, seed)
        ],
    )


# ── the report ────────────────────────────────────────────────────────

MEDIA_AREA_STATUS_TEXT = {
    "masked": "masked",
    "mask_too_large": f"not masked: the boxes cover more than {MAX_MASKED_SHARE:.0%} of the image",
    "no_page_url": "not masked: the screenshot has no page URL",
    "no_media_facts": "not masked: the screenshot carries no measured media facts",
    "undeclared": "not masked: no rule declares the page",
    "no_image": "not masked: the screenshot does not decode",
}


def _page(url: str | None) -> tuple[str, str]:
    parts = urlsplit(url or "")
    return parts.hostname or "(unknown)", parts.path or "/"


def media_area_section(report: StabilityReport) -> list[str]:
    """The media-area section of the report: what was masked and what it changed."""
    rule_set = report.media_areas
    if rule_set is None:
        return []
    items = report.observations
    with_areas = [(item, item.media_areas) for item in items if item.media_areas is not None]
    masked = [item for item in items if item.masked]
    only_media = [
        item
        for item in masked
        if _changed((item.delayed, item.before_action))
        and not _changed(item.deciding_comparisons())
    ]
    still = [item for item in masked if _changed(item.deciding_comparisons())]
    other_tab = [
        item for item, areas in with_areas if areas.mask_applied and areas.frames_on_other_tab
    ]
    undeclared = [
        item
        for item, areas in with_areas
        if areas.status == "undeclared" and item.media_playing and item.media_change
    ]
    # Without media facts nothing is known to play, so only a missing page URL leaves
    # a change over playing media unmatched.
    unmatched = [
        item for item, areas in with_areas if areas.status == "no_page_url" and item.media_change
    ]
    lines = [
        "## Media areas",
        "",
        f"Media-area rules of `{rule_set.directory or '(in memory)'}` (sha256 "
        f"`{rule_set.sha256}`): {', '.join(rule.id for rule in rule_set.rules)}. On a page one "
        "declares (the screenshot's page URL in its scope), the boxes the screenshot's media "
        "facts measured are masked out of the observation and of each frame before comparing; "
        "the verdicts above are the masked ones where the mask was applied. Frames record no "
        "page URL, so a frame is taken to show the observation's page; "
        f"{len(other_tab)} masked observation(s) compared a frame taken on another tab than "
        "the observation's delayed frame.",
        "",
        table(
            ["media areas", "observations"],
            [
                [
                    MEDIA_AREA_STATUS_TEXT[status],
                    sum(1 for _, areas in with_areas if areas.status == status),
                ]
                for status in MEDIA_AREA_STATUSES
            ],
        ),
        "",
        f"Of {len(masked)} masked observation(s), {len(only_media)} changed only in the masked "
        f"media areas (changed on the whole images, unchanged with the mask) and {len(still)} "
        "still change with the mask.",
        "",
        table(
            ["rule", "masked", "changed only in media areas", "still change"],
            [
                [
                    rule.id,
                    sum(1 for item in masked if _rule_applied(item, rule.id)),
                    sum(1 for item in only_media if _rule_applied(item, rule.id)),
                    sum(1 for item in still if _rule_applied(item, rule.id)),
                ]
                for rule in rule_set.rules
            ],
        ),
        "",
        f"Observations whose playing media changed on pages no rule declares: {len(undeclared)}"
        + (
            f"; {len(unmatched)} more with a change over playing media could not be matched "
            "(no page URL)."
            if unmatched
            else "."
        ),
        "",
    ]
    if undeclared:
        pages = Counter(_page(item.url) for item in undeclared)
        lines += [
            table(
                ["host", "path", "observations"],
                [
                    [host, path, count]
                    for (host, path), count in sorted(
                        pages.items(), key=lambda entry: (-entry[1], entry[0])
                    )
                ],
            ),
            "",
        ]
    return lines


def _rule_applied(item: ObservationStability, rule_id: str) -> bool:
    return item.media_areas is not None and rule_id in item.media_areas.rule_ids


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
        "The dHash distance is reported only."
        + (
            " On pages a media-area rule declares, the media areas are masked out before "
            "comparing (see Media areas)."
            if report.media_areas is not None
            else ""
        ),
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
    lines += media_area_section(report)
    if report.skipped_runs:
        lines += ["## Runs not read", ""]
        lines += [f"- `{key}`: {why}" for key, why in sorted(report.skipped_runs.items())]
        lines.append("")
    lines += excluded_section(report.excluded_runs)
    return "\n".join(lines)


def load_media_areas(directory: Path) -> SiteRules:
    """The replay rule set in *directory*, read as a replay reads it; it must declare media areas.

    Raises:
        LookaheadInputError: The rule set does not load, or declares no
            media-area rules.
    """
    try:
        rules = SiteRules.load(directory)
    except SiteRuleError as exc:
        raise LookaheadInputError(f"--media-areas {directory}: {exc}") from exc
    if not rules.media_areas:
        raise LookaheadInputError(
            f"--media-areas {directory}: the rule set declares no media_areas rules"
        )
    return rules


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
    media_areas: SiteRules | None = None,
) -> tuple[StabilityReport, Path]:
    """Analyse, write ``stability.json`` and ``report.md`` under *out*, and the queue; the queue's path.

    *media_areas*: the replay rule set whose media-area rules mask the
    declared pages' media areas (:func:`load_media_areas`; ``None``: none).
    """
    if not 0 < min_change_area < 1:
        raise ValueError("--min-change-area is a share of the image, between 0 and 1")
    unknown = sorted(origins - set(ORIGINS))
    if unknown or not origins:
        raise ValueError(f"--origins takes {', '.join(ORIGINS)}; got {sorted(origins)}")
    method = DifferenceMethod()
    items = analyse(
        runs,
        method=method,
        min_change_area=min_change_area,
        origins=origins,
        media_area_rules=media_areas.media_areas if media_areas is not None else None,
    )
    rule_set = (
        MediaAreaRuleSet(
            directory=str(media_areas.directory) if media_areas.directory is not None else None,
            sha256=media_areas.sha256,
            rules=list(media_areas.media_areas),
        )
        if media_areas is not None
        else None
    )
    report = StabilityReport(
        runs=len(runs.runs),
        runs_with_frames=sum(1 for run in runs.runs if run.key not in runs.skipped and run.frames),
        origins=[origin for origin in ORIGINS if origin in origins],
        min_change_area=min_change_area,
        method=method,
        inputs={"root": str(runs.root), "viewer_root": str(runs.viewer_root)},
        summary=dict(Counter(item.verdict for item in items)),
        skipped_runs=dict(runs.skipped),
        excluded_runs=dict(runs.excluded),
        media_areas=rule_set,
        observations=items,
    )
    queue = build_queue(
        items,
        queue_id=queue_id,
        size=queue_size,
        seed=seed,
        runs={run.key: run for run in runs.runs},
        min_change_area=min_change_area,
    )
    write_json(out / "stability.json", report.model_dump(mode="json"))
    write_text(out / "report.md", render_report(report))
    return report, write_queue(annotations, queue)
