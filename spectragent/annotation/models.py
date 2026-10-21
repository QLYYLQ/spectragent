"""The review queue and label formats a person labels against.

Tools write ``review-queue-1`` files; the viewer appends ``review-label-1``
lines. Both are strict: an unknown field is a broken file named with its
error, never silently dropped (``docs/architecture_design.md`` §4.17).
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

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
    # NaN and infinity are not numbers a label or queue can mean.
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class LabelChoice(_Strict):
    value: str = Field(min_length=1)
    text: str = ""


class RegionKind(_Strict):
    value: str = Field(min_length=1)
    text: str = ""


class RegionSpec(_Strict):
    kinds: list[RegionKind] = Field(min_length=1)


class Question(_Strict):
    """One of several separate questions a queue asks about an item."""

    key: str = Field(min_length=1)
    text: str = ""
    choices: list[LabelChoice] = Field(min_length=1)
    #: ``None``: every item; otherwise the item ids this question is asked of.
    applies_to: list[str] | None = None

    @model_validator(mode="after")
    def _unique_values(self) -> Question:
        values = [choice.value for choice in self.choices]
        if len(set(values)) != len(values):
            raise ValueError(f"each choice value of question {self.key!r} must be unique")
        return self

    def choice_values(self) -> list[str]:
        return [choice.value for choice in self.choices]


class LabelSpec(_Strict):
    """What a label for this queue may contain.

    One question (``choices``, ``multi``) or several (``questions``, each
    answered once), never both.
    """

    choices: list[LabelChoice] = Field(default_factory=list)
    multi: bool = False
    questions: list[Question] | None = None
    regions: RegionSpec | None = None
    note: bool = True

    @model_validator(mode="after")
    def _unique_values(self) -> LabelSpec:
        for name, values in (
            ("choice", [choice.value for choice in self.choices]),
            ("region kind", [kind.value for kind in self.regions.kinds] if self.regions else []),
            ("question key", [question.key for question in self.questions or []]),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"each {name} must be unique")
        if self.questions is not None and (self.choices or self.multi):
            raise ValueError("a queue asks either questions or one set of choices, not both")
        if self.questions is not None and not self.questions:
            raise ValueError("questions, when given, lists at least one question")
        return self

    @property
    def choice_values(self) -> list[str]:
        return [choice.value for choice in self.choices]

    @property
    def kind_values(self) -> list[str]:
        return [kind.value for kind in self.regions.kinds] if self.regions else []


#: Whole pixels stay integers; a tool may give fractional ones.
Box = Annotated[list[StrictInt | StrictFloat], Field(min_length=4, max_length=4)]


class Region(_Strict):
    """A box on one of the item's images, in that image's original pixels."""

    image: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    box: Box

    @field_validator("box")
    @classmethod
    def _ordered(cls, box: list[int | float]) -> list[int | float]:
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
    #: Question key to suggested choice value, for a queue that asks questions.
    answers: dict[str, str] | None = None
    #: Question key to a one-sentence reason for that answer.
    reasons: dict[str, str] | None = None
    regions: list[Region] | None = None
    note: str | None = None
    source: str | None = None
    rationale: str | None = None

    @property
    def has_answer(self) -> bool:
        return (
            self.choice is not None
            or self.choices is not None
            or self.answers is not None
            or self.regions is not None
        )


class QueueItem(_Strict):
    item_id: str = Field(min_length=1)
    title: str = ""
    run: str | None = None
    step: int | None = Field(default=None, ge=0)
    images: list[QueueImage] = Field(default_factory=list)
    fields: list[ItemField] = Field(default_factory=list)
    links: list[ItemLink] = Field(default_factory=list)
    #: The question keys this item asks; ``None`` defers to each question's ``applies_to``.
    questions: list[str] | None = None
    suggestion: Suggestion | None = None

    def image(self, role: str) -> QueueImage | None:
        return next((image for image in self.images if image.role == role), None)


def asked_questions(spec: LabelSpec, item: QueueItem | None) -> list[Question]:
    """The questions *item* asks, in the queue's order.

    The item's own ``questions`` list wins; without one, every question
    whose ``applies_to`` is ``None`` or names the item.
    """
    if spec.questions is None or item is None:
        return []
    if item.questions is not None:
        return [question for question in spec.questions if question.key in item.questions]
    return [
        question
        for question in spec.questions
        if question.applies_to is None or item.item_id in question.applies_to
    ]


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
            keys = {question.key for question in self.label.questions or []}
            unknown = sorted(set(item.questions or []) - keys)
            if unknown:
                raise ValueError(f"item {item.item_id!r} asks unknown questions {unknown}")
            if self.label.questions is not None and not asked_questions(self.label, item):
                raise ValueError(f"item {item.item_id!r} asks none of the queue's questions")
            if item.suggestion is not None:
                if item.suggestion.answers == {}:
                    raise ValueError(
                        f"item {item.item_id!r} has a suggestion with no answers; "
                        "leave 'answers' out when nothing is suggested"
                    )
                problems = answer_problems(
                    self.label,
                    item,
                    choice=item.suggestion.choice,
                    choices=item.suggestion.choices,
                    answers=item.suggestion.answers,
                    regions=item.suggestion.regions or [],
                    require_choice=False,
                )
                asked = {question.key for question in asked_questions(self.label, item)}
                for key in sorted(set(item.suggestion.reasons or {}) - asked):
                    problems.append(f"a reason is given for {key!r}, which this item does not ask")
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
    answers: dict[str, str] | None = None
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
        # A queue label is addressed by its item, an ad-hoc label by its step;
        # a queue that asks questions answers them instead of one choice.
        omit = ["item_id"] if self.queue_id == ADHOC_QUEUE_ID else ["run", "step"]
        omit += ["choice", "choices"] if self.answers is not None else ["answers"]
        for name in omit:
            del data[name]
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class LabelDraft(BaseModel):
    """What a person submits for one item; the annotator, time and verdict are the store's."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    choice: str | None = None
    choices: list[str] | None = None
    answers: dict[str, str] | None = None
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
    answers: dict[str, str] | None,
    regions: list[Region],
    require_choice: bool,
) -> list[str]:
    """Why an answer does not fit *spec* (sizes aside); ``[]`` when it does.

    *item* names the images a region may refer to and the questions it
    asks; ``None`` means the one ad-hoc screenshot and no questions.
    """
    problems: list[str] = []
    allowed = spec.choice_values
    if spec.questions is not None:
        if choice is not None or choices is not None:
            problems.append("this queue asks questions: send 'answers', not 'choice' or 'choices'")
        asked = {question.key: question for question in asked_questions(spec, item)}
        for key, value in (answers or {}).items():
            question = asked.get(key)
            if question is None:
                problems.append(f"this item does not ask question {key!r}")
            elif value not in question.choice_values():
                problems.append(f"{value!r} is not one of the choices of question {key!r}")
        missing = [key for key in asked if key not in (answers or {})]
        if require_choice and missing:
            problems.append(f"answer every question this item asks; missing {missing}")
    elif answers is not None:
        problems.append("this queue asks no separate questions: 'answers' is not used")
    elif spec.multi:
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
