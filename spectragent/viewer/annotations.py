"""HTTP surface of the annotation directory: review queues, labels and their images.

The viewer writes label lines into the annotation directory it was started
with and nowhere else; without one every write here is refused. Item images
are resolved from the queue item, so the page can only fetch what a queue
references (``docs/architecture_design.md`` §4.17).
"""

from __future__ import annotations

import io
import ipaddress
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from spectragent.annotation import (
    ADHOC_SPEC,
    AnnotationError,
    AnnotationStore,
    ArchiveImage,
    ImageSource,
    LabelDraft,
    LabelLine,
    LabelLog,
    LoadedQueue,
    QueueChangedError,
    QueueItem,
    Region,
)
from spectragent.annotation.labels import step_target, target
from spectragent.inspection.repository import RunRepository
from spectragent.schemas.observation.image import image_media_type

DISABLED = "标注没有打开。用 --annotations DIR 启动查看器，才能标注审阅队列和步骤。"


class QueueLabelRequest(LabelDraft):
    queue_id: str
    item_id: str
    #: The digest of the queue version the person loaded and labeled.
    queue_sha256: str = Field(min_length=64, max_length=64)


class StepLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    run: str
    step: int = Field(ge=0)
    regions: list[Region] = Field(default_factory=list)
    note: str = ""


def _label(line: LabelLine, queue_sha256: str | None = None) -> dict[str, Any]:
    data = line.model_dump(mode="json")
    data["queue_changed"] = queue_sha256 is not None and line.queue_sha256 != queue_sha256
    return data


def _is_empty(line: LabelLine) -> bool:
    return not line.regions and not line.note


def _item_states(loaded: LoadedQueue, log: LabelLog, annotator: str) -> list[dict[str, Any]]:
    current = log.current()
    counts: dict[str, int] = {}
    for line in log.lines:
        counts[target(line)] = counts.get(target(line), 0) + 1
    states = []
    for item in loaded.queue.items:
        mine = current.get((item.item_id, annotator))
        others = [
            _label(line, loaded.sha256)
            for (key, who), line in sorted(current.items())
            if key == item.item_id and who != annotator
        ]
        states.append(
            {
                "item": item.model_dump(mode="json"),
                "mine": _label(mine, loaded.sha256) if mine else None,
                "others": others,
                "history": counts.get(item.item_id, 0),
            }
        )
    return states


_PROGRESS: tuple[str, ...] = (
    "total",
    "labeled",
    "skipped",
    "remaining",
    "accepted",
    "corrected",
    "rejected",
    "labeled_by_anyone",
)


def _progress(states: list[dict[str, Any]]) -> dict[str, int]:
    progress: dict[str, int] = dict.fromkeys(_PROGRESS, 0)
    for state in states:
        progress["total"] += 1
        mine = state["mine"]
        if any(not label["skipped"] for label in [mine, *state["others"]] if label):
            progress["labeled_by_anyone"] += 1
        if mine is None:
            progress["remaining"] += 1
        elif mine["skipped"]:
            progress["skipped"] += 1
        else:
            progress["labeled"] += 1
            if mine["verdict"]:
                progress[mine["verdict"]] += 1
    return progress


def host_refusal(request: Request) -> tuple[int, str] | None:
    """Why an API request is refused for its Host, or ``None``.

    Only an IP literal or ``localhost`` is answered: another site could point
    any other name at this local server (DNS rebinding) and read or label
    through it.
    """
    name = urlsplit(f"//{request.headers.get('host', '')}").hostname or ""
    if name == "localhost":
        return None
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return 403, "请用 IP 地址或 localhost 打开查看器；不接受其他主机名"
    return None


def write_refusal(request: Request) -> tuple[int, str] | None:
    """Why a write request is refused, or ``None``: JSON from this page's own origin.

    Checked before the body is read, for every review request that is not a read.
    """
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return 415, "保存标注必须是 JSON 请求"
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    parts = urlsplit(origin or "")
    if (
        origin is None
        or parts.scheme not in {"http", "https"}
        or parts.netloc.lower() != host.lower()
    ):
        return 403, "只接受从查看器页面发来的标注"
    return None


def annotation_router(
    repository: RunRepository,
    store: AnnotationStore | None,
    run_key: Callable[[str], str],
    image_type: Callable[[bytes], str],
) -> APIRouter:
    router = APIRouter(prefix="/api/review")

    def require() -> AnnotationStore:
        if store is None:
            raise HTTPException(403, DISABLED)
        return store

    def image_bytes(source: ImageSource) -> bytes:
        key = run_key(source.run)
        if isinstance(source, ArchiveImage):
            return repository.archive_image(key, source.step, source.archiver, source.name)
        data = repository.screenshot(key, source.step, view="browser")
        if data is None:
            raise FileNotFoundError(f"运行 {source.run} 的第 {source.step} 步没有截图")
        return data

    def image_size(source: ImageSource) -> tuple[int, int]:
        return _size(image_bytes(source))

    def item(
        queue_id: str, item_id: str, queue_sha256: str | None = None
    ) -> tuple[LoadedQueue, QueueItem]:
        loaded = require().load_queue(queue_id)
        if queue_sha256 is not None and loaded.sha256 != queue_sha256:
            raise QueueChangedError(
                f"队列 {queue_id!r} 在你打开之后被重新生成了；请重新加载，查看当前版本"
            )
        found = loaded.queue.item(item_id)
        if found is None:
            raise FileNotFoundError(f"队列 {queue_id!r} 里没有条目 {item_id!r}")
        return loaded, found

    @router.get("/queues")
    def queues() -> dict[str, Any]:
        current = require()
        rows = []
        for listing in current.list_queues():
            row: dict[str, Any] = {
                "queue_id": listing.queue_id,
                "file": listing.file,
                "error": listing.error,
                "title": listing.queue_id,
                "progress": None,
                "label_errors": 0,
            }
            if listing.loaded is not None:
                log = current.labels(listing.queue_id)
                states = _item_states(listing.loaded, log, current.annotator)
                row.update(
                    title=listing.loaded.queue.title or listing.queue_id,
                    progress=_progress(states),
                    label_errors=len(log.errors),
                )
            rows.append(row)
        adhoc = current.adhoc_labels()
        return {
            "queues": rows,
            "adhoc": {
                "labels": sum(not _is_empty(line) for line in adhoc.current().values()),
                "errors": len(adhoc.errors),
            },
        }

    @router.get("/queue")
    def queue(queue_id: str) -> dict[str, Any]:
        current = require()
        loaded = current.load_queue(queue_id)
        log = current.labels(queue_id)
        states = _item_states(loaded, log, current.annotator)
        known = {state["item"]["item_id"] for state in states}
        return {
            "queue_id": loaded.queue.queue_id,
            "title": loaded.queue.title or loaded.queue.queue_id,
            "instructions": loaded.queue.instructions,
            "guide": [section.model_dump(mode="json") for section in loaded.queue.guide],
            "suggestion_display": loaded.queue.suggestion_display.model_dump(mode="json"),
            "label": loaded.queue.label.model_dump(mode="json"),
            "sha256": loaded.sha256,
            "items": states,
            "progress": _progress(states),
            "label_errors": [{"line": e.line, "error": e.error} for e in log.errors],
            # Items that have labels but are no longer in the queue, counted once each.
            "removed_items_with_labels": len({target(line) for line in log.lines} - known),
        }

    @router.get("/history")
    def history(queue_id: str, item_id: str) -> dict[str, Any]:
        loaded, _ = item(queue_id, item_id)
        lines = require().labels(queue_id).history(item_id)
        return {"labels": [_label(line, loaded.sha256) for line in lines]}

    @router.get("/image")
    def image(queue_id: str, item_id: str, role: str, queue_sha256: str) -> Response:
        _, found = item(queue_id, item_id, queue_sha256)
        picture = found.image(role)
        if picture is None:
            raise FileNotFoundError(f"条目 {item_id!r} 没有图片 {role!r}")
        data = image_bytes(picture.source)
        return Response(content=data, media_type=image_type(data))

    @router.get("/marks")
    def marks(queue_id: str, item_id: str, role: str, queue_sha256: str) -> dict[str, Any]:
        """The actions drawn on one item image: its ``marks_from``, else what the image shows."""
        _, found = item(queue_id, item_id, queue_sha256)
        picture = found.image(role)
        if picture is None:
            raise FileNotFoundError(f"条目 {item_id!r} 没有图片 {role!r}")
        source = picture.source
        return repository.image_marks(
            run_key(source.run),
            source.step,
            archiver=source.archiver if isinstance(source, ArchiveImage) else None,
            name=source.name if isinstance(source, ArchiveImage) else None,
            marks_from=[
                (run_key(entry.run), entry.step, entry.label) for entry in picture.marks_from
            ]
            if picture.marks_from is not None
            else None,
        )

    @router.post("/label")
    def label(body: QueueLabelRequest) -> dict[str, Any]:
        current = require()
        draft = LabelDraft.model_validate(
            body.model_dump(exclude={"queue_id", "item_id", "queue_sha256"})
        )
        line = current.append_label(
            body.queue_id,
            body.item_id,
            draft,
            queue_sha256=body.queue_sha256,
            image_size=image_size,
        )
        loaded = current.load_queue(body.queue_id)
        states = _item_states(loaded, current.labels(body.queue_id), current.annotator)
        state = next(state for state in states if state["item"]["item_id"] == body.item_id)
        return {"label": _label(line, loaded.sha256), "item": state, "progress": _progress(states)}

    def step_labels(current: AnnotationStore, run: str, step: int) -> dict[str, Any]:
        log = current.adhoc_labels()
        key = step_target(run, step)
        latest = log.current()
        mine = latest.get((key, current.annotator))
        others = [
            _label(line)
            for (at, who), line in sorted(latest.items())
            if at == key and who != current.annotator and not _is_empty(line)
        ]
        return {
            "spec": ADHOC_SPEC.model_dump(mode="json"),
            "mine": _label(mine) if mine is not None and not _is_empty(mine) else None,
            "others": others,
            "history": [_label(line) for line in log.history(key)],
            "errors": len(log.errors),
        }

    @router.get("/step")
    def step(run: str, step: Annotated[int, Query(ge=0)]) -> dict[str, Any]:
        return step_labels(require(), run_key(run), step)

    @router.post("/step")
    def save_step(body: StepLabelRequest) -> dict[str, Any]:
        current = require()
        key = run_key(body.run)
        if not repository.has_step(key, body.step):
            raise FileNotFoundError(f"运行 {key} 没有已提交的第 {body.step} 步")
        # A committed step never changes, so an ad-hoc label needs no version;
        # its screenshot is read only to check boxes drawn on it.
        screenshot = repository.screenshot(key, body.step, view="browser") if body.regions else None
        current.append_adhoc(
            key,
            body.step,
            LabelDraft(regions=body.regions, note=body.note),
            screenshot_size=_size(screenshot) if screenshot is not None else None,
        )
        return step_labels(current, key, body.step)

    @router.get("/adhoc")
    def adhoc() -> dict[str, Any]:
        log = require().adhoc_labels()
        current = log.current()
        rows = [
            _label(line)
            for line in reversed(log.lines)
            if current.get((target(line), line.annotator)) is line and not _is_empty(line)
        ]
        return {"labels": rows, "errors": [{"line": e.line, "error": e.error} for e in log.errors]}

    return router


def _size(data: bytes) -> tuple[int, int]:
    if image_media_type(data) is None:
        raise AnnotationError("引用的文件不是图片")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            return picture.size
    except (UnidentifiedImageError, OSError) as exc:
        raise AnnotationError(f"引用的图片读不出来：{exc}") from exc
