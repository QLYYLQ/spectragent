"""Offline screenshot differences, never a restoration verdict (architecture §4.16).

Only committed observation artifacts and branch provenance are read. A region that
changes between repeats might contain the task's answer; no region is a safe mask.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from html import escape
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from PIL import Image, ImageChops, ImageDraw, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from spectragent.core.capabilities.browser.restoration import dhash, hamming, observation_url
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.flywheel.metrics import ChainLayout, ChainRuns, Run, open_role
from spectragent.schemas.restoration import RestorationCheck

INTERPRETATION = (
    "Candidate changes are not permission to ignore a region. Business text, prices, search "
    "results and error messages can change too. Repeated replay differences do not establish "
    "a timer as their cause. No mask, restoration verdict or site exclusion is changed."
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DifferenceMethod(_Model):
    pixel_threshold: int = Field(default=16, ge=0, le=255)
    grid_size: int = Field(default=32, ge=1)
    cell_fraction: float = Field(default=0.01, gt=0, le=1, allow_inf_nan=False)
    description: str = (
        "RGB maximum absolute channel difference > pixel_threshold; a grid cell is active "
        "when at least cell_fraction of its pixels changed; four-neighbour connected cells "
        "become bounding boxes. No resizing. Repeat candidates union active cells from all "
        "comparable pairs. Full-image dHash uses the restoration algorithm, without masking."
    )


class ImageEvidence(_Model):
    id: str
    run: str
    step: int
    status: Literal["readable", "missing", "unreadable"]
    reason: str | None = None
    sha256: str | None = None
    dhash: str | None = None
    width: int | None = None
    height: int | None = None
    original: str | None = None
    preview: str | None = None
    source_url: str | None = None
    url_reason: str | None = None


class Region(_Model):
    x: int
    y: int
    width: int
    height: int
    cells: int


class ImageComparison(_Model):
    id: str
    kind: Literal["record_replay", "repeat_replay"]
    source_run: str
    step: int
    left: str
    right: str
    status: Literal["compared", "not_comparable"]
    reason: str | None = None
    byte_equal: bool | None = None
    dhash_distance: int | None = None
    url_match: bool | None = None
    url_reason: str | None = None
    changed_pixels: int | None = None
    total_pixels: int | None = None
    regions: list[Region] = []
    difference: str | None = None
    annotated: str | None = None
    recorded_check: RestorationCheck | None = None


class RepeatCandidates(_Model):
    source_run: str
    step: int
    images: list[str]
    comparison_ids: list[str] = []
    comparable_pairs: int = 0
    different_pairs: int = 0
    reason: str | None = None
    regions: list[Region] = []
    annotated: str | None = None
    interpretation: str = INTERPRETATION


class DiagnosticReport(_Model):
    format: Literal["af-replay-diagnostics-1"] = "af-replay-diagnostics-1"
    interpretation: str = INTERPRETATION
    method: DifferenceMethod
    inputs: dict[str, Any]
    summary: dict[str, int]
    images: list[ImageEvidence]
    recording_comparisons: list[ImageComparison]
    repeat_comparisons: list[ImageComparison]
    repeat_candidates: list[RepeatCandidates]
    unmatched: dict[str, str]


class DiagnosticInputError(ValueError):
    """Inputs or the output location do not meet the read-only diagnostic contract."""


@dataclass
class _Capture:
    evidence: ImageEvidence
    preview: Path | None = None

    def image(self) -> Image.Image | None:
        """Decode one generated preview; do not retain every run's full-size image in memory."""
        if self.preview is None:
            return None
        with Image.open(self.preview) as opened:
            return opened.convert("RGB")


class _Assets:
    def __init__(self, out: Path) -> None:
        self.out = out
        (out / "assets").mkdir()

    def put(self, data: bytes, suffix: str) -> str:
        name = f"assets/{hashlib.sha256(data).hexdigest()}.{suffix}"
        path = self.out / name
        if not path.exists():
            path.write_bytes(data)
        return name

    def png(self, image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return self.put(buffer.getvalue(), "png")


def _capture(run: Run, step: int, identifier: str, assets: _Assets) -> _Capture:
    evidence = ImageEvidence(id=identifier, run=run.pointer, step=step, status="missing")
    record = run.step(step)
    if record is None:
        evidence.reason = "step was not committed"
        evidence.url_reason = "step was not committed"
        return _Capture(evidence)
    if record.origin == "terminal":
        evidence.reason = "terminal observation is not a source-path policy state"
        evidence.url_reason = evidence.reason
        return _Capture(evidence)
    try:
        data = run.trace.screenshot(step)
    except TraceSchemaError as exc:
        evidence.status = "unreadable"
        evidence.reason = f"committed screenshot does not read back ({type(exc).__name__})"
        evidence.url_reason = "screenshot evidence does not read back"
        return _Capture(evidence)
    if data is None:
        evidence.reason = "no successful screenshot was committed"
    else:
        evidence.sha256 = hashlib.sha256(data).hexdigest()
        try:
            with Image.open(io.BytesIO(data)) as opened:
                image_format = opened.format
                image = opened.convert("RGB")
            evidence.width, evidence.height = image.size
            digest = dhash(data)
            evidence.dhash = f"{digest:016x}" if digest is not None else None
            suffix = {"PNG": "png", "JPEG": "jpg"}.get(image_format or "", "image")
            evidence.original = assets.put(data, suffix)
            evidence.preview = evidence.original if image_format == "PNG" else assets.png(image)
            evidence.status = "readable"
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            evidence.status = "unreadable"
            evidence.reason = f"screenshot image cannot decode ({type(exc).__name__})"
            evidence.original = assets.put(data, "image")
    try:
        screenshot = run.trace.observation_fragment(step, "browser.screenshot")
        meta = run.trace.observation_fragment(step, "browser.browser_meta")
        evidence.source_url = observation_url(
            SimpleNamespace(screenshot=screenshot, browser_meta=meta)
        )
        if evidence.source_url is None:
            evidence.url_reason = "no document URL in this observation cycle"
    except TraceSchemaError as exc:
        evidence.url_reason = f"document URL evidence does not read back ({type(exc).__name__})"
    return _Capture(evidence, assets.out / evidence.preview if evidence.preview else None)


def _regions(cells: set[tuple[int, int]], size: tuple[int, int], grid: int) -> list[Region]:
    pending = set(cells)
    result: list[Region] = []
    while pending:
        start = min(pending)
        pending.remove(start)
        group, stack = {start}, [start]
        while stack:
            x, y = stack.pop()
            for cell in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if cell in pending:
                    pending.remove(cell)
                    group.add(cell)
                    stack.append(cell)
        x = min(c[0] for c in group) * grid
        y = min(c[1] for c in group) * grid
        right = min((max(c[0] for c in group) + 1) * grid, size[0])
        bottom = min((max(c[1] for c in group) + 1) * grid, size[1])
        result.append(Region(x=x, y=y, width=right - x, height=bottom - y, cells=len(group)))
    return sorted(result, key=lambda region: (region.y, region.x))


def _difference(
    left: Image.Image, right: Image.Image, method: DifferenceMethod
) -> tuple[Image.Image, int, set[tuple[int, int]]]:
    difference = ImageChops.difference(left, right)
    red, green, blue = difference.split()
    maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    changed = maximum.point([255 if value > method.pixel_threshold else 0 for value in range(256)])
    cells: set[tuple[int, int]] = set()
    grid = method.grid_size
    for y in range(0, left.height, grid):
        for x in range(0, left.width, grid):
            tile = changed.crop((x, y, min(x + grid, left.width), min(y + grid, left.height)))
            if tile.histogram()[255] / (tile.width * tile.height) >= method.cell_fraction:
                cells.add((x // grid, y // grid))
    return difference, changed.histogram()[255], cells


def _annotate(image: Image.Image, regions: list[Region], assets: _Assets) -> str:
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)
    for region in regions:
        draw.rectangle(
            (region.x, region.y, region.x + region.width - 1, region.y + region.height - 1),
            outline="#f04b34",
            width=2,
        )
    return assets.png(annotated)


def _compare(
    left: _Capture,
    right: _Capture,
    *,
    identifier: str,
    kind: Literal["record_replay", "repeat_replay"],
    source: str,
    step: int,
    method: DifferenceMethod,
    assets: _Assets,
    check: RestorationCheck | None = None,
) -> tuple[ImageComparison, set[tuple[int, int]]]:
    a, b = left.evidence, right.evidence
    result = ImageComparison(
        id=identifier,
        kind=kind,
        source_run=source,
        step=step,
        left=a.id,
        right=b.id,
        status="not_comparable",
        recorded_check=check,
    )
    if a.source_url is not None and b.source_url is not None:
        result.url_match = a.source_url == b.source_url
    else:
        result.url_reason = (
            "both observations need document URL evidence; an unknown URL is not equal"
        )
    if a.sha256 is not None and b.sha256 is not None:
        result.byte_equal = a.sha256 == b.sha256
    if a.dhash is not None and b.dhash is not None:
        result.dhash_distance = hamming(int(a.dhash, 16), int(b.dhash, 16))
    left_image, right_image = left.image(), right.image()
    if left_image is None or right_image is None:
        result.reason = "; ".join(
            f"{side}: {capture.evidence.reason}"
            for side, capture in (("left", left), ("right", right))
            if capture.preview is None
        )
        return result, set()
    if left_image.size != right_image.size:
        result.reason = (
            f"image dimensions differ: {left_image.size} vs {right_image.size}; not resized"
        )
        return result, set()
    difference, count, cells = _difference(left_image, right_image, method)
    result.status = "compared"
    result.changed_pixels = count
    result.total_pixels = left_image.width * left_image.height
    result.regions = _regions(cells, left_image.size, method.grid_size)
    result.difference = assets.png(difference)
    result.annotated = _annotate(right_image, result.regions, assets)
    return result, cells


def diagnose(
    recordings: Path, replays: Path, out: Path, *, method: DifferenceMethod | None = None
) -> DiagnosticReport:
    """Write a fresh, deterministic offline diagnostic; inputs are never modified."""
    method = method or DifferenceMethod()
    recordings, replays = recordings.expanduser().resolve(), replays.expanduser().resolve()
    target = out.expanduser()
    if target.exists() or target.is_symlink():
        raise DiagnosticInputError(f"output already exists: {target}")
    out = target.resolve()
    for name, directory in (("recordings", recordings), ("replays", replays)):
        if not directory.is_dir():
            raise DiagnosticInputError(f"{name} directory does not exist: {directory}")
        if out.is_relative_to(directory):
            raise DiagnosticInputError(f"output must be outside the {name} directory: {out}")
    record_role, replay_role = open_role(recordings, "record"), open_role(replays, "same_path")
    # Reuse the metrics' provenance resolver. No layout-derived path is read here.
    chain = ChainRuns(
        ChainLayout(recordings.parent), {"record": record_role, "same_path": replay_role}
    )
    out.mkdir(parents=True, exist_ok=False)
    assets = _Assets(out)
    captures: dict[tuple[str, int], _Capture] = {}

    def capture(run: Run, step: int) -> _Capture:
        key = (run.pointer, step)
        if key not in captures:
            captures[key] = _capture(run, step, f"image-{len(captures):06d}", assets)
        return captures[key]

    recording_comparisons: list[ImageComparison] = []
    repeat_comparisons: list[ImageComparison] = []
    groups: dict[tuple[str, int], list[_Capture]] = defaultdict(list)
    unmatched: dict[str, str] = {}
    for run in replay_role.runs:
        source = chain.source_of(run)
        if source is None or run.checkpoint is None:
            branch = run.config.branch
            unmatched[run.pointer] = (
                f"source recording not found or unreadable: {branch.source_trace}"
                if branch is not None
                else "no branch source provenance"
            )
            continue
        steps = [
            step.step
            for step in source.steps
            if step.origin == "policy" and step.step <= run.checkpoint
        ]
        if run.checkpoint not in steps:
            unmatched[run.pointer] = "source checkpoint is not a committed policy step"
            continue
        for step in steps:
            left, right = capture(source, step), capture(run, step)
            record = run.step(step)
            comparison, _cells = _compare(
                left,
                right,
                identifier=f"record-{len(recording_comparisons):06d}",
                kind="record_replay",
                source=source.pointer,
                step=step,
                method=method,
                assets=assets,
                check=record.restoration if record else None,
            )
            recording_comparisons.append(comparison)
            groups[(source.pointer, step)].append(right)
    candidates: list[RepeatCandidates] = []
    for (source, step), members in sorted(groups.items()):
        candidate = RepeatCandidates(
            source_run=source, step=step, images=[m.evidence.id for m in members]
        )
        cells: set[tuple[int, int]] = set()
        comparable_sizes: set[tuple[int, int]] = set()
        base_capture: _Capture | None = None
        for left, right in combinations(members, 2):
            comparison, pair_cells = _compare(
                left,
                right,
                identifier=f"repeat-{len(repeat_comparisons):06d}",
                kind="repeat_replay",
                source=source,
                step=step,
                method=method,
                assets=assets,
            )
            repeat_comparisons.append(comparison)
            candidate.comparison_ids.append(comparison.id)
            if comparison.status == "compared":
                candidate.comparable_pairs += 1
                candidate.different_pairs += int(bool(comparison.changed_pixels))
                assert right.evidence.width is not None and right.evidence.height is not None
                comparable_sizes.add((right.evidence.width, right.evidence.height))
                base_capture = right
                cells.update(pair_cells)
        if not candidate.comparable_pairs:
            candidate.reason = (
                "only one replay; repeated variation cannot be inferred"
                if len(members) == 1
                else "fewer than two readable replay screenshots with the same dimensions"
            )
        elif len(comparable_sizes) > 1:
            candidate.reason = (
                "comparable pairs have different dimensions; regions cannot be combined"
            )
        else:
            assert base_capture is not None
            base = base_capture.image()
            assert base is not None
            candidate.regions = _regions(cells, base.size, method.grid_size)
            candidate.annotated = _annotate(base, candidate.regions, assets)
            if not cells:
                candidate.reason = "no repeated change exceeded the stated grid thresholds"
        candidates.append(candidate)
    images = [capture.evidence for capture in captures.values()]
    report = DiagnosticReport(
        method=method,
        inputs={
            "recordings": {
                "readable_runs": len(record_role.runs),
                "skipped": dict(record_role.skipped),
            },
            "replays": {
                "readable_runs": len(replay_role.runs),
                "skipped": dict(replay_role.skipped),
            },
            "scope": "source policy steps through each replay's checkpoint; no post-checkpoint comparison",
        },
        summary={
            "source_tasks": len({comparison.source_run for comparison in recording_comparisons}),
            "recording_pairs": len(recording_comparisons),
            "recording_pairs_compared": sum(c.status == "compared" for c in recording_comparisons),
            "recording_pairs_different": sum(bool(c.changed_pixels) for c in recording_comparisons),
            "recording_pairs_not_comparable": sum(
                c.status != "compared" for c in recording_comparisons
            ),
            "recording_pairs_url_measured": sum(
                c.url_match is not None for c in recording_comparisons
            ),
            "repeat_pairs": len(repeat_comparisons),
            "repeat_pairs_compared": sum(c.status == "compared" for c in repeat_comparisons),
            "repeat_pairs_different": sum(bool(c.changed_pixels) for c in repeat_comparisons),
            "repeat_pairs_not_comparable": sum(c.status != "compared" for c in repeat_comparisons),
            "repeat_steps_with_candidate_regions": sum(bool(c.regions) for c in candidates),
            "repeat_steps_without_comparable_pairs": sum(
                not c.comparable_pairs for c in candidates
            ),
            "unmatched_replays": len(unmatched),
            **{
                f"images_{status}": count
                for status, count in sorted(Counter(i.status for i in images).items())
            },
        },
        images=images,
        recording_comparisons=recording_comparisons,
        repeat_comparisons=repeat_comparisons,
        repeat_candidates=candidates,
        unmatched=unmatched,
    )
    (out / "diagnostics.json").write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (out / "index.html").write_text(_html(report), encoding="utf-8")
    return report


def _html(report: DiagnosticReport) -> str:
    images = {image.id: image for image in report.images}

    def code(value: Any) -> str:
        return (
            "<pre>"
            + escape(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
            + "</pre>"
        )

    def figure(path: str | None, label: str) -> str:
        if path is None:
            return f"<figure><figcaption>{escape(label)}: unavailable</figcaption></figure>"
        return (
            f'<figure><a href="{escape(path, quote=True)}"><img loading="lazy" '
            f'src="{escape(path, quote=True)}" alt="{escape(label, quote=True)}"></a>'
            f"<figcaption>{escape(label)}</figcaption></figure>"
        )

    def pair(comparison: ImageComparison) -> str:
        left, right = images[comparison.left], images[comparison.right]
        title = (
            f"{comparison.source_run} · step {comparison.step} · {right.run} · "
            f"{comparison.status} · dHash {comparison.dhash_distance} · "
            f"{comparison.changed_pixels} changed pixels"
        )
        gallery = "".join(
            figure(path, label)
            for path, label in (
                (left.preview, left.run),
                (right.preview, right.run),
                (comparison.difference, "Absolute RGB difference"),
                (comparison.annotated, "Changed grid regions; not an ignore mask"),
            )
        )
        return (
            f'<details id="{comparison.id}"><summary>{escape(title)}</summary>'
            f'<div class="gallery">{gallery}</div>'
            + code(
                {
                    "left": left.model_dump(),
                    "right": right.model_dump(),
                    "comparison": comparison.model_dump(mode="json"),
                }
            )
            + "</details>"
        )

    repeats = []
    for candidate in report.repeat_candidates:
        title = (
            f"{candidate.source_run} · step {candidate.step}: {candidate.comparable_pairs} comparable "
            f"pairs, {len(candidate.regions)} candidate regions"
        )
        repeats.append(
            f"<details><summary>{escape(title)}</summary>"
            + figure(
                candidate.annotated,
                "Repeated replay candidate changes; business content may be inside",
            )
            + code(candidate.model_dump())
            + "</details>"
        )
    return """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'">
<title>Replay screenshot diagnostics</title><style>
body{font:16px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:1500px;padding:0 1rem;color:#182635;background:#f5f6f8}
h1,h2{line-height:1.2}a{color:#075b94}.note{border-left:5px solid #ce542c;padding:1rem;background:#fff0e7}
details{background:white;border:1px solid #cbd3dc;border-radius:6px;margin:1rem 0;padding:1rem}summary{cursor:pointer;overflow-wrap:anywhere}
.gallery{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}figure{margin:1rem 0}img{max-width:100%;height:auto;border:1px solid #cbd3dc}
figcaption{overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef1f4;padding:1rem;font-size:13px}
</style><body><h1>Replay screenshot diagnostics</h1>
""" + (
        f'<p class="note">{escape(report.interpretation)}</p>'
        '<p><a href="diagnostics.json">Download the complete JSON evidence</a>. '
        "Images and comparisons are local. Expand a row to inspect its evidence.</p>"
        "<h2>Coverage</h2>"
        + code(report.summary)
        + "<h2>Method and limits</h2>"
        + code(report.method.model_dump())
        + "<h2>Unreadable or unmatched runs</h2>"
        + code({"inputs": report.inputs, "unmatched": report.unmatched})
        + "<h2>Recording versus replay</h2>"
        + "".join(pair(c) for c in report.recording_comparisons)
        + "<h2>Candidate changes between repeated replays</h2>"
        + "".join(repeats)
        + "<h2>Repeat pair evidence</h2>"
        + "".join(pair(c) for c in report.repeat_comparisons)
        + "</body></html>\n"
    )
