"""Synthetic browser runs for the lookahead pipeline, written through the real WAL writer.

Fragments go through ``encode_fragment`` and archive files through
``encode_archive_file``, so the bytes on disk are what a recording writes.
The features built beside this pipeline are written in their agreed shapes:
``StepRecord.action_spans``, the ``browser.frames`` archiver's ``frames.json``
index with its content-addressed PNG frames, and a branch's ``task_variant``
provenance. :class:`ContractTrace` reads the first and the last back the way
a reader with those fields does, when the installed models do not declare
them yet; :func:`frames_reader_module` stands in for the frames reader when it
is not installed.
"""

from __future__ import annotations

import io
import json
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.archive.privacy import RedactionLedger
from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    PointerEffect,
    ScrollEffect,
    ScrollOffset,
    TextInputEffect,
    ViewportPoint,
    WaitEffect,
)
from spectragent.core.capabilities.browser.media import (
    MediaElementState,
    MediaReading,
    MediaRect,
    MediaState,
)
from spectragent.core.capabilities.browser.observations import (
    BrowserCaptureStateObs,
    ScreenshotObs,
)
from spectragent.core.capabilities.browser.settle import PageSettle
from spectragent.core.trace.encoding import encode_archive_file, encode_fragment, sha256_hex
from spectragent.core.trace.fsio import atomic_write
from spectragent.core.trace.models import (
    TaskSetupRecord,
    TraceConfig,
    TraceNegotiation,
    TraceStatus,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation.capture_state import FailedVisibleResources, PointerState
from spectragent.schemas.restoration import (
    BranchProvenance,
    RestorationCheck,
    RestorationEvidence,
)
from spectragent.schemas.step_record import StepRecord

FRAMES = "browser.frames"
FRAMES_MODULE = "spectragent.core.capabilities.browser.archive.frames"
SIZE = (128, 96)


# ── images ────────────────────────────────────────────────────────────


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def gradient(descending: bool = True, size: tuple[int, int] = SIZE) -> bytes:
    """A horizontal grey gradient: every dHash bit set (descending) or none (ascending)."""
    width, height = size
    image = Image.new("RGB", size)
    for x in range(width):
        value = round(255 * (1 - x / (width - 1))) if descending else round(255 * x / (width - 1))
        for y in range(height):
            image.putpixel((x, y), (value, value, value))
    return _png(image)


def with_box(data: bytes, box: tuple[int, int, int, int], color: tuple[int, int, int]) -> bytes:
    """*data* with a filled rectangle (right and bottom exclusive)."""
    image = Image.open(io.BytesIO(data)).convert("RGB")
    ImageDraw.Draw(image).rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=color)
    return _png(image)


def flipped_right_half(data: bytes) -> bytes:
    """*data* with its right half replaced by the ascending gradient: about half the bits change."""
    image = Image.open(io.BytesIO(data)).convert("RGB")
    other = Image.open(io.BytesIO(gradient(descending=False, size=image.size))).convert("RGB")
    half = image.width // 2
    image.paste(other.crop((half, 0, image.width, image.height)), (half, 0))
    return _png(image)


# ── actions ───────────────────────────────────────────────────────────


def click(x: int, y: int, role: str, name: str) -> tuple[Action, ActionResult]:
    effect = PointerEffect(
        effect_id="browser.click_coordinate",
        point=ViewportPoint(x=x, y=y),
        hit=ElementFacts(role=role, name=name),
        focus_after="nothing",
    )
    return (
        Action(name="browser.click_coordinate", params={"coordinate_x": x, "coordinate_y": y}),
        ActionResult(success=True, effect=effect),
    )


def type_text(text: str) -> tuple[Action, ActionResult]:
    effect = TextInputEffect(
        effect_id="browser.type",
        target=ElementFacts(role="textbox", name="Search"),
        sensitive=False,
        value_changed=True,
        text_entered=True,
        focus_after=ElementFacts(role="textbox", name="Search"),
    )
    return (
        Action(name="browser.type", params={"index": 3, "text": text}),
        ActionResult(success=True, effect=effect),
    )


def scroll_down() -> tuple[Action, ActionResult]:
    effect = ScrollEffect(
        effect_id="browser.scroll",
        direction="down",
        container="page",
        before=ScrollOffset(x=0, y=0),
        after=ScrollOffset(x=0, y=400),
        at_boundary=False,
    )
    return (
        Action(name="browser.scroll", params={"direction": "down"}),
        ActionResult(success=True, effect=effect),
    )


def wait() -> tuple[Action, ActionResult]:
    return (
        Action(name="browser.wait", params={}),
        ActionResult(success=True, effect=WaitEffect(effect_id="browser.wait", waited_ms=1000)),
    )


def done(answer: str) -> tuple[Action, ActionResult]:
    return (
        Action(name="browser.done", params={"extracted_content": answer}),
        ActionResult(success=True, is_done=True, extracted_content=answer),
    )


def playing_video(box: tuple[int, int, int, int]) -> MediaState:
    reading = MediaReading(current_time=3.0, paused=False, ended=False)
    return MediaState(
        read_before="measured",
        read_after="measured",
        elements=[
            MediaElementState(
                frame="main",
                order=0,
                tag="video",
                rect=MediaRect(x=box[0], y=box[1], width=box[2] - box[0], height=box[3] - box[1]),
                source_kind="url",
                before=reading,
                after=reading,
            )
        ],
    )


# ── frames (the agreed browser-frames-1 shape) ────────────────────────


def frame(
    kind: str,
    cycle: int,
    *,
    image: bytes | None = None,
    action_index: int | None = None,
    action: str | None = None,
    requested_at: float = 0.0,
    captured_at: float | None = None,
    outcome: str = "captured",
    superseded_by: str | None = None,
) -> tuple[dict[str, Any], bytes | None]:
    """One ``frames.json`` entry and its image; a frame that was not captured has none."""
    if (image is not None) != (outcome == "captured"):
        raise ValueError("a captured frame has an image, and only a captured one")
    name = sha = None
    width = height = None
    if image is not None:
        sha = sha256_hex(image)
        index = action_index if action_index is not None else "x"
        name = f"frame-{cycle}-{kind}-{index}.png"
        width, height = Image.open(io.BytesIO(image)).size
    entry = {
        "kind": kind,
        "cycle": cycle,
        "action_index": action_index,
        "action": action,
        "tab": "TAB1",
        "requested_at": requested_at,
        "captured_at": (captured_at if captured_at is not None else requested_at + 0.05)
        if image is not None
        else None,
        "outcome": outcome,
        "superseded_by": superseded_by,
        "name": name,
        "sha256": sha,
        "width": width,
        "height": height,
        "detail": None,
    }
    return entry, image


def frames_files(
    frames: list[tuple[dict[str, Any], bytes | None]],
    *,
    step: int | None = None,
    trailing: int | None = None,
) -> list[Any]:
    """The archive files of one ``browser.frames`` collection: the PNGs and ``frames.json``."""
    files: list[Any] = []
    for entry, image in frames:
        if image is None:
            continue
        item = ArchiveFile(
            archiver_id=FRAMES,
            name=entry["name"],
            media_type="image/png",
            data=image,
            meta=ArchiveProvenance(
                captured_at=entry["captured_at"], cycle=entry["cycle"], ids={"tab": "TAB1"}
            ),
            content_addressed=True,
        )
        files.append(encode_archive_file(item, step=step, trailing=trailing))
    index = {"format": "browser-frames-1", "frames": [entry for entry, _ in frames]}
    item = ArchiveFile(
        archiver_id=FRAMES,
        name="frames.json",
        media_type="application/json",
        data=json.dumps(index).encode(),
        meta=ArchiveProvenance(captured_at=1000.0),
    )
    files.append(encode_archive_file(item, step=step, trailing=trailing))
    return files


def dom_raw_file(step: int, cycle: int) -> Any:
    item = ArchiveFile(
        archiver_id="browser.dom_raw",
        name=f"dom-{cycle}-TAB1.json.gz",
        media_type="application/gzip",
        data=f"raw dom of cycle {cycle}".encode(),
        meta=ArchiveProvenance(captured_at=1000.0 + cycle, cycle=cycle, ids={"tab": "TAB1"}),
        content_addressed=True,
    )
    return encode_archive_file(item, step=step)


# ── runs ──────────────────────────────────────────────────────────────


class SpannedStepRecord(StepRecord):
    """A step record carrying per-action spans, written as the agreed field."""

    action_spans: list[dict[str, Any]] | None = None


@dataclass
class StepSpec:
    step: int
    origin: str = "policy"
    image: bytes | None = None
    url: str | None = None
    actions: list[tuple[Action, ActionResult]] = field(default_factory=list)
    settle: PageSettle | None = None
    media: MediaState | None = None
    restoration: RestorationCheck | None = None
    error_phase: str = ""
    error: str | None = None
    llm_output: str = ""
    dom_cycle: int | None = None
    frames: list[tuple[dict[str, Any], bytes | None]] | None = None
    action_spans: list[dict[str, Any]] | None = None
    observed: bool = True


def settled(waited_ms: int = 300) -> PageSettle:
    return PageSettle(waited_ms=waited_ms, outcome="settled")


def capped(waited_ms: int = 3000) -> PageSettle:
    return PageSettle(waited_ms=waited_ms, outcome="capped", waiting_for=["load"])


def ok_check(step: int) -> RestorationCheck:
    return RestorationCheck(
        step=step,
        verdict="ok",
        evidence=[
            RestorationEvidence(capability_id="browser", screenshot_distance=1, url_match=True)
        ],
    )


def write_run(
    base: Path,
    run_id: str,
    *,
    task: str,
    steps: list[StepSpec],
    start_url: str = "https://shop.example/",
    network_source: str | None = "live",
    branch: BranchProvenance | None = None,
    trailing_frames: list[tuple[dict[str, Any], bytes | None]] | None = None,
    status: TraceStatus = TraceStatus.COMPLETE,
) -> Path:
    """One run directory ``base/run_id``; the path."""
    resolved: dict[str, Any] = {}
    if network_source is not None:
        resolved = {
            "environment": [
                {
                    "env_type": "browser",
                    "capabilities": [
                        {
                            "capability_id": "browser",
                            "network_source": {"source_id": network_source},
                        }
                    ],
                }
            ]
        }
    wal = WALWriter(base, run_id)
    wal.init_run(
        TraceConfig(
            agent_id="ui_tars",
            loop_id="branch" if branch is not None else "react",
            benchmark="bench",
            task_id=run_id,
            task=task,
            resolved=resolved,
            negotiation=TraceNegotiation(
                active_fragments=["browser.capture_state", "browser.screenshot", "loop.step_info"],
                env_sourced_fragments=["browser.capture_state", "browser.screenshot"],
                loop_sourced_fragments=["loop.step_info"],
                active_archivers=["browser.dom_raw", FRAMES],
                archive_bindings={"browser.dom_raw": "browser", FRAMES: "browser"},
            ),
            task_setup=TaskSetupRecord(
                context={"start_url": start_url}, redactions=RedactionLedger()
            ),
            branch=branch,
        )
    )
    for spec in steps:
        fragments = []
        if spec.observed and spec.image is not None:
            shot = ScreenshotObs(
                image_bytes=spec.image,
                viewport_width=SIZE[0],
                viewport_height=SIZE[1],
                image_width=SIZE[0],
                image_height=SIZE[1],
                source_url=spec.url,
                settle=spec.settle,
                media=spec.media,
                captured_at=1000.0 + 10 * spec.step,
            )
            state = BrowserCaptureStateObs(
                pointer=PointerState(status="known", x=5, y=5),
                failed_visible_resources=FailedVisibleResources(status="measured", count=0),
                settle=spec.settle,
                captured_at=1000.0 + 10 * spec.step,
            )
            fragments = [encode_fragment(spec.step, shot), encode_fragment(spec.step, state)]
        extra = []
        if spec.dom_cycle is not None:
            extra.append(dom_raw_file(spec.step, spec.dom_cycle))
        if spec.frames is not None:
            extra += frames_files(spec.frames, step=spec.step)
        record = SpannedStepRecord(
            step=spec.step,
            origin=spec.origin,  # type: ignore[arg-type]
            llm_output=spec.llm_output,
            actions=[action for action, _ in spec.actions],
            action_results=[result for _, result in spec.actions],
            timestamp=1000.0 + 10 * spec.step,
            error_phase=spec.error_phase,
            error=spec.error,
            restoration=spec.restoration,
            action_spans=spec.action_spans,
        )
        wal.commit_step(
            spec.step,
            record,
            fragments=[fragment for fragment in fragments if fragment is not None],
            extra_files=extra,
        )
    if trailing_frames is not None:
        wal.commit_trailing(extra_files=frames_files(trailing_frames, trailing=0))
    wal.finalize(status)
    return wal.run_dir


def add_task_variant(run_dir: Path, variant: dict[str, Any]) -> None:
    """Record a task-variant branch's provenance, ``branch.task_variant``, in ``config.json``."""
    path = run_dir / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["branch"]["task_variant"] = variant
    config["task"] = variant["task_text"]
    atomic_write(path, json.dumps(config, indent=2).encode())


# ── reading the agreed fields back ────────────────────────────────────


class ContractTrace(RunTrace):
    """``RunTrace`` with the action spans and the task variant read back as their fields.

    When the installed ``StepRecord`` / ``BranchProvenance`` declare the fields,
    the reader itself returns them and this changes nothing.
    """

    @property
    def step_records(self) -> list[StepRecord]:
        records = super().step_records
        if "action_spans" in StepRecord.model_fields:
            return records
        spans = {}
        for entry in self.manifest.steps:
            spans[entry.step] = json.loads(self.read_committed(entry.step_file)).get("action_spans")
        return [
            record.model_copy(update={"action_spans": spans[record.step]}) for record in records
        ]

    @property
    def config(self) -> TraceConfig:
        config = super().config
        if config.branch is None or "task_variant" in BranchProvenance.model_fields:
            return config
        variant = json.loads(self.read_committed("config.json"))["branch"].get("task_variant")
        if variant is None:
            return config
        branch = config.branch.model_copy(update={"task_variant": variant})
        return config.model_copy(update={"branch": branch})


# ── the frames reader, as agreed ──────────────────────────────────────


@dataclass(frozen=True)
class FrameRecord:
    kind: str
    cycle: int
    action_index: int | None
    action: str | None
    tab: str
    requested_at: float
    captured_at: float | None
    outcome: str
    superseded_by: str | None
    name: str | None
    sha256: str | None
    width: int | None
    height: int | None
    detail: str | None


def _index(reader: Any, entries: list[Any]) -> list[FrameRecord]:
    for entry in entries:
        if entry.name == "frames.json":
            document = json.loads(reader.read_extra(entry.path))
            return [FrameRecord(**item) for item in document["frames"]]
    return []


def _read_frames(reader: Any, step: int) -> list[FrameRecord]:
    return _index(reader, list(reader.archive_files(step, FRAMES)))


def _iter_frames(
    reader: Any, *, include_trailing: bool = True
) -> Iterator[tuple[int | None, FrameRecord]]:
    for step, entry in reader.iter_archive(FRAMES, include_trailing=include_trailing):
        if entry.name == "frames.json":
            for record in _index(reader, [entry]):
                yield step, record


def _read_frame_image(reader: Any, step: int | None, record: FrameRecord) -> bytes:
    if step is None:
        entries = [
            entry for at, entry in reader.iter_archive(FRAMES, include_trailing=True) if at is None
        ]
    else:
        entries = list(reader.archive_files(step, FRAMES))
    for entry in entries:
        if entry.name == record.name:
            return reader.read_extra(entry.path)
    raise KeyError(record.name)


def frames_reader_module() -> types.ModuleType:
    module = types.ModuleType(FRAMES_MODULE)
    module.FRAMES_ARCHIVER_ID = FRAMES  # type: ignore[attr-defined]
    module.FrameRecord = FrameRecord  # type: ignore[attr-defined]
    module.read_frames = _read_frames  # type: ignore[attr-defined]
    module.iter_frames = _iter_frames  # type: ignore[attr-defined]
    module.read_frame_image = _read_frame_image  # type: ignore[attr-defined]
    return module


def install_frames_reader(monkeypatch: Any) -> None:
    """Use the installed frames reader; stand in for it only when it is not installed."""
    try:
        __import__(FRAMES_MODULE)
    except ImportError:
        monkeypatch.setitem(sys.modules, FRAMES_MODULE, frames_reader_module())
