"""Review queues for the trace viewer (``review-queue-1``).

A queue is written to ``<annotations>/queues/<queue_id>.json``; the viewer
started with ``--annotations <annotations>`` reads it and appends labels under
``<annotations>/labels/``. Every run named in an item is a key relative to the
root the viewer is started with. Boxes are ``[x0, y0, x1, y1]`` in pixels of
the referenced image's original size.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

QUEUE_FORMAT = "review-queue-1"
_QUEUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Choice(_Model):
    value: str
    text: str


class RegionKind(_Model):
    value: str
    text: str


class RegionLabel(_Model):
    kinds: list[RegionKind]


class LabelSpec(_Model):
    choices: list[Choice] = Field(default_factory=list)
    multi: bool = False
    regions: RegionLabel | None = None
    note: bool = True


class ObservationSource(_Model):
    kind: Literal["observation"] = "observation"
    run: str
    step: int


class ArchiveSource(_Model):
    kind: Literal["archive"] = "archive"
    run: str
    step: int
    archiver: str
    name: str


class QueueImage(_Model):
    role: str
    text: str
    source: ObservationSource | ArchiveSource


class ItemField(_Model):
    text: str
    value: str


class ItemLink(_Model):
    text: str
    run: str
    step: int | None = None


class SuggestedRegion(_Model):
    image: str
    kind: str
    box: list[int] = Field(min_length=4, max_length=4)


class Suggestion(_Model):
    choice: str | None = None
    choices: list[str] | None = None
    regions: list[SuggestedRegion] = Field(default_factory=list)
    note: str | None = None
    source: str
    rationale: str | None = None


class QueueItem(_Model):
    item_id: str
    title: str
    run: str
    step: int
    images: list[QueueImage] = Field(default_factory=list)
    fields: list[ItemField] = Field(default_factory=list)
    links: list[ItemLink] = Field(default_factory=list)
    suggestion: Suggestion | None = None

    @model_validator(mode="after")
    def _regions_name_images(self) -> QueueItem:
        roles = [image.role for image in self.images]
        if len(set(roles)) != len(roles):
            raise ValueError(f"item {self.item_id!r} repeats an image role: {roles}")
        if self.suggestion is not None:
            for region in self.suggestion.regions:
                if region.image not in roles:
                    raise ValueError(
                        f"item {self.item_id!r} suggests a region on image {region.image!r}, "
                        f"which is not one of its images {roles}"
                    )
        return self


class ReviewQueue(_Model):
    format: Literal["review-queue-1"] = QUEUE_FORMAT
    queue_id: str
    title: str
    instructions: str
    label: LabelSpec
    items: list[QueueItem]

    @model_validator(mode="after")
    def _consistent(self) -> ReviewQueue:
        if not _QUEUE_ID.match(self.queue_id):
            raise ValueError(
                f"queue_id {self.queue_id!r} must be a file name: letters, digits, '.', '_', '-'"
            )
        ids = [item.item_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("item_id must be unique within a queue")
        choices = {choice.value for choice in self.label.choices}
        kinds = {kind.value for kind in self.label.regions.kinds} if self.label.regions else set()
        for item in self.items:
            suggestion = item.suggestion
            if suggestion is None:
                continue
            named = [suggestion.choice] if suggestion.choice is not None else []
            named += suggestion.choices or []
            unknown = [value for value in named if value not in choices]
            if unknown:
                raise ValueError(f"item {item.item_id!r} suggests unknown choice(s) {unknown}")
            if suggestion.choices is not None and not self.label.multi:
                raise ValueError(
                    f"item {item.item_id!r} suggests several choices in a single-choice queue"
                )
            for region in suggestion.regions:
                if region.kind not in kinds:
                    raise ValueError(
                        f"item {item.item_id!r} suggests a region of kind {region.kind!r}, "
                        f"which the queue does not offer"
                    )
        return self


def default_queue_id(prefix: str) -> str:
    """``<prefix>-<YYYYMMDD>`` for today."""
    return f"{prefix}-{datetime.now().strftime('%Y%m%d')}"


def queue_path(annotations: Path, queue_id: str) -> Path:
    return annotations / "queues" / f"{queue_id}.json"


def write_queue(annotations: Path, queue: ReviewQueue) -> Path:
    """Write *queue* under ``<annotations>/queues/``; the file written."""
    path = queue_path(annotations, queue.queue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = queue.model_dump(mode="json")
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def observation_image(role: str, text: str, run: str, step: int) -> QueueImage:
    return QueueImage(role=role, text=text, source=ObservationSource(run=run, step=step))


def archive_image(
    role: str, text: str, run: str, step: int, archiver: str, name: str
) -> QueueImage:
    return QueueImage(
        role=role,
        text=text,
        source=ArchiveSource(run=run, step=step, archiver=archiver, name=name),
    )
