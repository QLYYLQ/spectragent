"""The review queue and label formats a person labels against.

Tools write ``review-queue-1`` files; the viewer appends ``review-label-1``
lines. Both are strict: an unknown field is a broken file named with its
error, never silently dropped (``docs/architecture_design.md`` §4.17).
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QUEUE_FORMAT = "review-queue-1"
LABEL_FORMAT = "review-label-1"
#: The ``queue_id`` of notes and regions attached to a run step outside any queue.
ADHOC_QUEUE_ID = "_adhoc"
#: The one image an ad-hoc region can name: the step's committed screenshot.
ADHOC_IMAGE = "screenshot"

#: A queue id is a file name stem; ``_``-prefixed names are reserved.
QUEUE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

Verdict = Literal["accepted", "corrected", "rejected"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabelChoice(_Strict):
    value: str = Field(min_length=1)
    text: str = ""


class RegionKind(_Strict):
    value: str = Field(min_length=1)
    text: str = ""


class RegionSpec(_Strict):
    kinds: list[RegionKind] = Field(min_length=1)


class LabelSpec(_Strict):
    """What a label for this queue may contain."""

    choices: list[LabelChoice] = Field(default_factory=list)
    multi: bool = False
    regions: RegionSpec | None = None
    note: bool = True

    @model_validator(mode="after")
    def _unique_values(self) -> LabelSpec:
        for name, values in (
            ("choice", [choice.value for choice in self.choices]),
            ("region kind", [kind.value for kind in self.regions.kinds] if self.regions else []),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"each {name} value must be unique")
        return self

    @property
    def choice_values(self) -> list[str]:
        return [choice.value for choice in self.choices]

    @property
    def kind_values(self) -> list[str]:
        return [kind.value for kind in self.regions.kinds] if self.regions else []


Box = Annotated[list[float], Field(min_length=4, max_length=4)]


class Region(_Strict):
    """A box on one of the item's images, in that image's original pixels."""

    image: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    box: Box

    @field_validator("box")
    @classmethod
    def _ordered(cls, box: list[float]) -> list[float]:
        x0, y0, x1, y1 = box
        if min(box) < 0 or x1 <= x0 or y1 <= y0:
            raise ValueError("a box is [x0, y0, x1, y1] with 0 <= x0 < x1 and 0 <= y0 < y1")
        return box

    def key(self) -> tuple[str, str, tuple[float, ...]]:
        return self.image, self.kind, tuple(self.box)


class ObservationImage(_Strict):
    """The committed screenshot of one run step (the clean browser state)."""

    kind: Literal["observation"]
    run: str = Field(min_length=1)
    step: int = Field(ge=0)


class ArchiveImage(_Strict):
    """One committed archive file of a run step; ``step: null`` is trailing evidence."""

    kind: Literal["archive"]
    run: str = Field(min_length=1)
    step: int | None = Field(ge=0)
    archiver: str = Field(min_length=1)
    name: str = Field(min_length=1)


ImageSource = Annotated[ObservationImage | ArchiveImage, Field(discriminator="kind")]


class QueueImage(_Strict):
    role: str = Field(min_length=1)
    text: str = ""
    source: ImageSource


class ItemField(_Strict):
    text: str
    value: str | int | float | bool | None = None


class ItemLink(_Strict):
    text: str
    run: str = Field(min_length=1)
    step: int | None = Field(default=None, ge=0)


class Suggestion(_Strict):
    """A proposed answer, usually a model's, that the person accepts or corrects."""

    choice: str | None = None
    choices: list[str] | None = None
    regions: list[Region] | None = None
    note: str | None = None
    source: str | None = None
    rationale: str | None = None

    @property
    def has_answer(self) -> bool:
        return self.choice is not None or self.choices is not None or self.regions is not None


class QueueItem(_Strict):
    item_id: str = Field(min_length=1)
    title: str = ""
    run: str | None = None
    step: int | None = Field(default=None, ge=0)
    images: list[QueueImage] = Field(default_factory=list)
    fields: list[ItemField] = Field(default_factory=list)
    links: list[ItemLink] = Field(default_factory=list)
    suggestion: Suggestion | None = None

    def image(self, role: str) -> QueueImage | None:
        return next((image for image in self.images if image.role == role), None)


class ReviewQueue(_Strict):
    format: Literal["review-queue-1"]
    queue_id: str
    title: str = ""
    instructions: str = ""
    label: LabelSpec
    items: list[QueueItem]

    @field_validator("queue_id")
    @classmethod
    def _file_name(cls, value: str) -> str:
        if not QUEUE_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "a queue id is 1-128 letters, digits, '.', '_' or '-', "
                "starting with a letter or digit"
            )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ReviewQueue:
        seen: set[str] = set()
        for item in self.items:
            if item.item_id in seen:
                raise ValueError(f"item_id {item.item_id!r} appears more than once")
            seen.add(item.item_id)
            roles = [image.role for image in item.images]
            if len(set(roles)) != len(roles):
                raise ValueError(f"item {item.item_id!r} repeats an image role")
            if item.suggestion is not None:
                problems = answer_problems(
                    self.label,
                    item,
                    choice=item.suggestion.choice,
                    choices=item.suggestion.choices,
                    regions=item.suggestion.regions or [],
                    require_choice=False,
                )
                if item.suggestion.regions is not None and self.label.regions is None:
                    problems.insert(0, "the queue does not label regions")
                if problems:
                    raise ValueError(
                        f"item {item.item_id!r} has an invalid suggestion: {problems[0]}"
                    )
        return self

    def item(self, item_id: str) -> QueueItem | None:
        return next((item for item in self.items if item.item_id == item_id), None)


class LabelLine(_Strict):
    """One appended label: a queue item's (``item_id``) or a run step's (``run`` + ``step``)."""

    format: Literal["review-label-1"]
    queue_id: str
    queue_sha256: str | None = None
    item_id: str | None = None
    run: str | None = None
    step: int | None = Field(default=None, ge=0)
    annotator: str = Field(min_length=1)
    at: str
    choice: str | None = None
    choices: list[str] | None = None
    regions: list[Region] | None = None
    note: str | None = None
    verdict: Verdict | None = None
    skipped: bool = False

    @model_validator(mode="after")
    def _target(self) -> LabelLine:
        if self.queue_id == ADHOC_QUEUE_ID:
            if self.item_id is not None or self.run is None or self.step is None:
                raise ValueError("an ad-hoc label names a run and a step, not an item")
        elif self.item_id is None or self.run is not None or self.step is not None:
            raise ValueError("a queue label names an item_id, not a run step")
        return self

    def to_json_line(self) -> str:
        data = self.model_dump(mode="json")
        # A queue label is addressed by its item, an ad-hoc label by its step.
        for name in ("item_id",) if self.queue_id == ADHOC_QUEUE_ID else ("run", "step"):
            del data[name]
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class LabelDraft(BaseModel):
    """What a person submits for one item; the annotator, time and verdict are the store's."""

    model_config = ConfigDict(extra="forbid")

    choice: str | None = None
    choices: list[str] | None = None
    regions: list[Region] = Field(default_factory=list)
    note: str = ""
    skipped: bool = False
    #: The person marks the suggestion wrong without giving a replacement answer.
    reject: bool = False


def answer_problems(
    spec: LabelSpec,
    item: QueueItem | None,
    *,
    choice: str | None,
    choices: list[str] | None,
    regions: list[Region],
    require_choice: bool,
) -> list[str]:
    """Why an answer does not fit *spec* (sizes aside); ``[]`` when it does.

    *item* names the images a region may refer to; ``None`` means the one
    ad-hoc screenshot.
    """
    problems: list[str] = []
    allowed = spec.choice_values
    if spec.multi:
        if choice is not None:
            problems.append("this queue takes several choices: send 'choices', not 'choice'")
        for value in choices or []:
            if value not in allowed:
                problems.append(f"{value!r} is not one of this queue's choices")
        if choices is not None and len(set(choices)) != len(choices):
            problems.append("a choice is listed twice")
    else:
        if choices is not None:
            problems.append("this queue takes one choice: send 'choice', not 'choices'")
        if choice is not None and choice not in allowed:
            problems.append(f"{choice!r} is not one of this queue's choices")
        if require_choice and allowed and choice is None:
            problems.append("choose one of this queue's choices, skip the item, or reject")
    if regions and spec.regions is None:
        problems.append("this queue does not label regions")
    kinds = spec.kind_values
    images = [image.role for image in item.images] if item is not None else [ADHOC_IMAGE]
    for region in regions:
        if spec.regions is not None and region.kind not in kinds:
            problems.append(f"{region.kind!r} is not one of this queue's region kinds")
        if region.image not in images:
            problems.append(f"{region.image!r} is not one of this item's images")
    return problems
