"""Synthetic browser runs for the lookahead pipeline, written through the real WAL writer.

Fragments go through ``encode_fragment`` and archive files through
``encode_archive_file``, so the bytes on disk are what a recording writes.
``browser.frames`` evidence is written the way its archiver hands it over:
one content-addressed PNG per captured frame, named by ``frame_name``, and the
step's ``frames.json`` index of ``FrameRecord`` entries; the real frames
reader reads it back.
"""

from __future__ import annotations

import io
import itertools
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw

from spectragent.core.archive import ArchiveFile, ArchiveProvenance
from spectragent.core.archive.privacy import RedactionLedger
from spectragent.core.capabilities.browser.archive.frames import (
    FRAMES_ARCHIVER_ID,
    FRAMES_FORMAT,
    FRAMES_INDEX_NAME,
    FrameKind,
    FrameOutcome,
    FrameRecord,
    FrameSuperseder,
    frame_name,
)
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
from spectragent.core.capabilities.browser.settle import PageSettle, SettlePending
from spectragent.core.trace.encoding import encode_archive_file, encode_fragment, sha256_hex
from spectragent.core.trace.models import (
    TaskSetupRecord,
    TraceConfig,
    TraceNegotiation,
    TraceStatus,
)
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.observation.capture_state import FailedVisibleResources, PointerState
from spectragent.schemas.restoration import (
    BranchProvenance,
    RestorationCheck,
    RestorationEvidence,
)
from spectragent.schemas.step_record import ActionSpan, StepRecord

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


def flat(color: tuple[int, int, int], size: tuple[int, int] = SIZE) -> bytes:
    """One colour all over: a page that shows nothing (a dark skeleton background, say)."""
    return _png(Image.new("RGB", size, color))


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


def failed_click(x: int, y: int, error: str) -> tuple[Action, ActionResult]:
    return (
        Action(name="browser.click_coordinate", params={"coordinate_x": x, "coordinate_y": y}),
        ActionResult(success=False, error=error),
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


def send_keys(keys: str) -> tuple[Action, ActionResult]:
    return (Action(name="browser.send_keys", params={"keys": keys}), ActionResult(success=True))


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


# ── frames, as the browser.frames archiver writes them ────────────────

Frame = tuple[FrameRecord, bytes | None]


def frame(
    kind: FrameKind,
    cycle: int,
    *,
    image: bytes | None = None,
    action_index: int | None = None,
    action: str | None = None,
    requested_at: float = 0.0,
    outcome: FrameOutcome = "captured",
    superseded_by: FrameSuperseder | None = None,
) -> Frame:
    """One ``frames.json`` entry and its image; a frame that was not captured has none."""
    if (image is not None) != (outcome == "captured"):
        raise ValueError("a captured frame has an image, and only a captured one")
    width = height = None
    if image is not None:
        width, height = Image.open(io.BytesIO(image)).size
    record = FrameRecord(
        kind=kind,
        cycle=cycle,
        action_index=action_index,
        action=action,
        tab="TAB1",
        requested_at=requested_at,
        captured_at=requested_at + 0.05 if image is not None else None,
        released_at=requested_at + 0.06 if kind == "before_action" else None,
        outcome=outcome,
        superseded_by=superseded_by,
        name=frame_name(kind, cycle, action_index) if image is not None else None,
        sha256=sha256_hex(image) if image is not None else None,
        width=width,
        height=height,
        detail=None if image is not None else f"not captured: {outcome}",
    )
    return record, image


def frames_files(
    frames: list[Frame], *, step: int | None = None, trailing: int | None = None
) -> list[Any]:
    """The archive files of one ``browser.frames`` collection: the PNGs and ``frames.json``."""
    files: list[Any] = []
    for record, image in frames:
        if image is None:
            continue
        assert record.name is not None and record.captured_at is not None
        item = ArchiveFile(
            archiver_id=FRAMES_ARCHIVER_ID,
            name=record.name,
            media_type="image/png",
            data=image,
            meta=ArchiveProvenance(
                captured_at=record.captured_at,
                cycle=record.cycle,
                ids={"tab": record.tab, "kind": record.kind},
            ),
            content_addressed=True,
        )
        files.append(encode_archive_file(item, step=step, trailing=trailing))
    index = {"format": FRAMES_FORMAT, "frames": [asdict(record) for record, _ in frames]}
    item = ArchiveFile(
        archiver_id=FRAMES_ARCHIVER_ID,
        name=FRAMES_INDEX_NAME,
        media_type="application/json",
        data=json.dumps(index).encode(),
        meta=ArchiveProvenance(captured_at=1000.0),
    )
    files.append(encode_archive_file(item, step=step, trailing=trailing))
    return files


_RECORD_IDS = itertools.count(1)


def exchange(
    url: str,
    resource_type: str = "XHR",
    *,
    status: int | None = 200,
    error_text: str | None = None,
    method: str = "GET",
) -> dict[str, Any]:
    """One ``kind: exchange`` line of ``browser.network``'s ``index.jsonl``, as the recorder writes it.

    A request that failed before any response has no status and Chrome's error text.
    """
    return {
        "kind": "exchange",
        "record_id": f"record-{next(_RECORD_IDS)}",
        "method": method,
        "url": url,
        "status": status,
        "resource_type": resource_type,
        "body_status": "ok" if error_text is None else "incomplete",
        "error_text": error_text,
    }


def network_index_file(step: int, lines: list[dict[str, Any]]) -> Any:
    """``browser.network``'s ``index.jsonl`` of one step's commit (no WARC: nothing here serves)."""
    item = ArchiveFile(
        archiver_id="browser.network",
        name="index.jsonl",
        media_type="application/x-ndjson",
        data="".join(json.dumps(line) + "\n" for line in lines).encode(),
        meta=ArchiveProvenance(captured_at=1000.0 + 10 * step),
    )
    return encode_archive_file(item, step=step)


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


@dataclass
class StepSpec:
    step: int
    origin: Literal["policy", "restored", "terminal"] = "policy"
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
    frames: list[Frame] | None = None
    action_spans: list[ActionSpan] | None = None
    observed: bool = True
    #: ``index.jsonl`` lines of ``browser.network`` this step's commit takes.
    network: list[dict[str, Any]] | None = None


def settled(waited_ms: int = 300) -> PageSettle:
    return PageSettle(waited_ms=waited_ms, outcome="settled")


def capped(waited_ms: int = 3000, waiting_for: tuple[SettlePending, ...] = ("load",)) -> PageSettle:
    return PageSettle(waited_ms=waited_ms, outcome="capped", waiting_for=list(waiting_for))


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
    trailing_frames: list[Frame] | None = None,
    status: TraceStatus = TraceStatus.COMPLETE,
    result: AgentResult | None = None,
    viewport: tuple[int, int] = SIZE,
    pointer: tuple[int, int] | None = (5, 5),
) -> Path:
    """One run directory ``base/run_id``; the path.

    Screenshots are ``SIZE`` pixels showing a *viewport* of CSS pixels (by
    default the same size: device scale 1); *pointer* is the pointer position
    each capture recorded (``None``: unknown).
    """
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
                active_archivers=["browser.dom_raw", FRAMES_ARCHIVER_ID],
                archive_bindings={"browser.dom_raw": "browser", FRAMES_ARCHIVER_ID: "browser"},
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
                viewport_width=viewport[0],
                viewport_height=viewport[1],
                image_width=SIZE[0],
                image_height=SIZE[1],
                source_url=spec.url,
                settle=spec.settle,
                media=spec.media,
                captured_at=1000.0 + 10 * spec.step,
            )
            state = BrowserCaptureStateObs(
                pointer=PointerState(status="known", x=pointer[0], y=pointer[1])
                if pointer is not None
                else PointerState(status="unknown"),
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
        if spec.network is not None:
            extra.append(network_index_file(spec.step, spec.network))
        record = StepRecord(
            step=spec.step,
            origin=spec.origin,
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
    if result is not None:
        wal.write_result(result)
    wal.finalize(status)
    return wal.run_dir
