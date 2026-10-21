"""The annotation directory: queues tools wrote, and the label lines people appended.

Layout (``docs/architecture_design.md`` §4.17)::

    DIR/queues/<queue_id>.json     review-queue-1, written by tools, only read here
    DIR/labels/<queue_id>.jsonl    review-label-1 lines, appended here
    DIR/labels/_adhoc.jsonl        notes and regions on any run step

Every read is a fresh snapshot of the files; nothing is cached across calls.
A label line is never rewritten: the latest line per target and annotator is
the current label, and earlier lines are its history.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from spectragent.annotation.labels import (
    LabelError,
    LabelLog,
    compute_verdict,
    region_bounds_problems,
)
from spectragent.annotation.models import (
    ADHOC_IMAGE,
    ADHOC_QUEUE_ID,
    LABEL_FORMAT,
    QUEUE_ID_PATTERN,
    ImageSource,
    LabelDraft,
    LabelLine,
    LabelSpec,
    QueueItem,
    RegionKind,
    RegionSpec,
    ReviewQueue,
    Verdict,
    answer_problems,
)
from spectragent.core.trace.fsio import atomic_write, fsync_dir

#: What an ad-hoc label may contain: a note and boxes on the step's screenshot.
ADHOC_SPEC = LabelSpec(
    choices=[],
    regions=RegionSpec(
        kinds=[
            RegionKind(value="needed_information", text="Needed information"),
            RegionKind(value="problem", text="Problem"),
            RegionKind(value="other", text="Other"),
        ]
    ),
    note=True,
)

#: Notes are free text, but a label file is not a document store.
MAX_NOTE_LENGTH = 20_000
_ANNOTATOR = re.compile(r"[^\x00-\x1f\x7f]{1,64}")


class AnnotationError(ValueError):
    """A queue, label or request that cannot be used, with an actionable reason."""


@dataclass(frozen=True)
class LoadedQueue:
    queue: ReviewQueue
    #: sha256 of the queue file's bytes as read; every label line records it.
    sha256: str


@dataclass(frozen=True)
class QueueListing:
    """One file under ``queues/``: its queue, or why it cannot be used."""

    queue_id: str
    file: str
    loaded: LoadedQueue | None
    error: str | None


def validation_message(exc: ValidationError) -> str:
    """The first problems of a pydantic error, located and without echoing input values."""
    problems = []
    for error in exc.errors(include_input=False, include_url=False)[:3]:
        location = ".".join(str(part) for part in error["loc"])
        problems.append(f"{location}: {error['msg']}" if location else error["msg"])
    more = exc.error_count() - len(problems)
    return "; ".join(problems) + (f" (and {more} more)" if more > 0 else "")


def check_annotator(name: str) -> str:
    name = name.strip()
    if not _ANNOTATOR.fullmatch(name):
        raise AnnotationError("An annotator name is 1-64 printable characters")
    return name


def write_queue(directory: Path, queue: ReviewQueue) -> Path:
    """Write *queue* atomically as ``DIR/queues/<queue_id>.json`` (for the tools that make queues)."""
    path = Path(directory) / "queues" / f"{queue.queue_id}.json"
    data = json.dumps(queue.model_dump(mode="json"), ensure_ascii=False, indent=2)
    atomic_write(path, data.encode() + b"\n")
    return path


class AnnotationStore:
    """The one writer of label lines; it never writes outside *directory*."""

    def __init__(self, directory: Path, *, annotator: str) -> None:
        self.directory = Path(directory).expanduser().resolve()
        if not self.directory.is_dir():
            raise FileNotFoundError(
                f"Annotation directory does not exist: {self.directory}. Create it, or point "
                "--annotations at the directory that holds queues/"
            )
        self.annotator = check_annotator(annotator)

    # ── paths ────────────────────────────────────────────────

    def _inside(self, *parts: str) -> Path:
        path = self.directory.joinpath(*parts)
        if not path.resolve().is_relative_to(self.directory):
            raise AnnotationError(f"{'/'.join(parts)} leaves the annotation directory")
        return path

    @staticmethod
    def _queue_id(queue_id: str) -> str:
        if not QUEUE_ID_PATTERN.fullmatch(queue_id):
            raise AnnotationError(f"{queue_id!r} is not a queue id")
        return queue_id

    def _labels_path(self, queue_id: str) -> Path:
        return self._inside("labels", f"{queue_id}.jsonl")

    # ── queues ───────────────────────────────────────────────

    def list_queues(self) -> list[QueueListing]:
        folder = self._inside("queues")
        if not folder.is_dir():
            return []
        listings = []
        for path in sorted(folder.glob("*.json")):
            queue_id = path.stem
            try:
                loaded = self.load_queue(queue_id)
                listings.append(QueueListing(queue_id, f"queues/{path.name}", loaded, None))
            except (AnnotationError, OSError) as exc:
                listings.append(QueueListing(queue_id, f"queues/{path.name}", None, str(exc)))
        return listings

    def load_queue(self, queue_id: str) -> LoadedQueue:
        if queue_id.startswith("_"):
            raise AnnotationError(f"Queue ids starting with '_' are reserved: {queue_id}")
        path = self._inside("queues", f"{self._queue_id(queue_id)}.json")
        if not path.is_file():
            raise FileNotFoundError(f"No review queue {queue_id!r} in {self.directory / 'queues'}")
        data = path.read_bytes()
        try:
            queue = ReviewQueue.model_validate(json.loads(data))
        except (UnicodeError, ValueError) as exc:
            message = validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
            raise AnnotationError(f"queues/{path.name} is not a review queue: {message}") from exc
        if queue.queue_id != queue_id:
            raise AnnotationError(
                f"queues/{path.name} declares queue_id {queue.queue_id!r}; "
                "the file name must be the queue id"
            )
        return LoadedQueue(queue, hashlib.sha256(data).hexdigest())

    # ── labels: reading ──────────────────────────────────────

    def labels(self, queue_id: str) -> LabelLog:
        if queue_id != ADHOC_QUEUE_ID:
            self._queue_id(queue_id)
        path = self._labels_path(queue_id)
        if not path.is_file():
            return LabelLog((), ())
        lines: list[LabelLine] = []
        errors: list[LabelError] = []
        for number, raw in enumerate(path.read_bytes().splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                line = LabelLine.model_validate(json.loads(raw))
            except (UnicodeError, ValueError) as exc:
                message = validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
                errors.append(LabelError(number, message))
                continue
            if line.queue_id != queue_id:
                errors.append(LabelError(number, f"labels queue {line.queue_id!r}"))
                continue
            lines.append(line)
        return LabelLog(tuple(lines), tuple(errors))

    def adhoc_labels(self) -> LabelLog:
        return self.labels(ADHOC_QUEUE_ID)

    # ── labels: appending ────────────────────────────────────

    def append_label(
        self,
        queue_id: str,
        item_id: str,
        draft: LabelDraft,
        *,
        image_size: Callable[[ImageSource], tuple[int, int]],
    ) -> LabelLine:
        """Validate *draft* against the queue as it is now, then append it.

        *image_size* reads the (width, height) of an item image; it is
        called only for the images the draft's regions are drawn on.
        """
        loaded = self.load_queue(queue_id)
        spec, item = loaded.queue.label, loaded.queue.item(item_id)
        if item is None:
            raise FileNotFoundError(f"Queue {queue_id!r} has no item {item_id!r}")
        if draft.reject and (item.suggestion is None or not item.suggestion.has_answer):
            raise AnnotationError("This item has no suggestion to reject")
        self._check_draft(spec, item, draft)
        sizes = {}
        for role in {region.image for region in draft.regions}:
            image = item.image(role)
            assert image is not None, "answer_problems checked every region's image"
            sizes[role] = image_size(image.source)
        self._raise(region_bounds_problems(draft.regions, sizes))
        line = self._line(
            spec,
            draft,
            queue_id=queue_id,
            queue_sha256=loaded.sha256,
            item_id=item_id,
            verdict=compute_verdict(
                item.suggestion,
                choice=draft.choice,
                choices=draft.choices,
                answers=draft.answers,
                regions=draft.regions,
                skipped=draft.skipped,
                reject=draft.reject,
            ),
        )
        self._append(queue_id, line)
        return line

    def append_adhoc(
        self, run: str, step: int, draft: LabelDraft, *, screenshot_size: tuple[int, int] | None
    ) -> LabelLine:
        """Append a note and regions for one run step; an empty draft clears the current one."""
        if (
            draft.choice is not None
            or draft.choices is not None
            or draft.answers is not None
            or draft.skipped
            or draft.reject
        ):
            raise AnnotationError("An ad-hoc label carries only a note and regions")
        self._check_draft(ADHOC_SPEC, None, draft)
        if draft.regions:
            if screenshot_size is None:
                raise AnnotationError("This step has no committed screenshot to draw on")
            self._raise(region_bounds_problems(draft.regions, {ADHOC_IMAGE: screenshot_size}))
        line = self._line(
            ADHOC_SPEC,
            draft,
            queue_id=ADHOC_QUEUE_ID,
            queue_sha256=None,
            run=run,
            step=step,
            verdict=None,
        )
        self._append(ADHOC_QUEUE_ID, line)
        return line

    @staticmethod
    def _raise(problems: list[str]) -> None:
        if problems:
            raise AnnotationError("; ".join(problems))

    def _check_draft(self, spec: LabelSpec, item: QueueItem | None, draft: LabelDraft) -> None:
        if draft.skipped and draft.reject:
            raise AnnotationError("Skip an item or reject its suggestion, not both")
        if (draft.skipped or draft.reject) and (
            draft.choice is not None or draft.choices or draft.answers or draft.regions
        ):
            raise AnnotationError(
                "A skipped item or a rejected suggestion carries no answer; only a note"
            )
        if draft.note and not spec.note:
            raise AnnotationError("This queue does not take notes")
        if len(draft.note) > MAX_NOTE_LENGTH:
            raise AnnotationError(f"A note is at most {MAX_NOTE_LENGTH} characters")
        self._raise(
            answer_problems(
                spec,
                item,
                choice=draft.choice,
                choices=draft.choices,
                answers=draft.answers,
                regions=draft.regions,
                require_choice=not (draft.skipped or draft.reject),
            )
        )

    def _line(
        self, spec: LabelSpec, draft: LabelDraft, *, verdict: Verdict | None, **target: object
    ) -> LabelLine:
        answered = not (draft.skipped or draft.reject)
        return LabelLine.model_validate(
            {
                "format": LABEL_FORMAT,
                **target,
                "annotator": self.annotator,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "choice": draft.choice if answered and not spec.multi else None,
                "choices": list(draft.choices or []) if answered and spec.multi else None,
                "answers": (dict(draft.answers or {}) if answered else {})
                if spec.questions is not None
                else None,
                "regions": list(draft.regions) if spec.regions is not None else None,
                "note": draft.note,
                "verdict": verdict,
                "skipped": draft.skipped,
            }
        )

    def _append(self, queue_id: str, line: LabelLine) -> None:
        folder = self._inside("labels")
        try:
            folder.mkdir()
            fsync_dir(self.directory)
        except FileExistsError:
            pass  # created earlier, or by a concurrent first label
        path = self._labels_path(queue_id)
        created = not path.exists()
        data = line.to_json_line().encode() + b"\n"
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o666)
        try:
            # One writer at a time across threads and processes. A line torn
            # by an earlier crash stays an unreadable line of its own.
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            fsync_dir(folder)
