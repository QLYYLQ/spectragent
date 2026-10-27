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


#: Whether a label that picks a choice may, must, or must not carry boxes.
RegionRule = Literal["optional", "required", "forbidden"]


class LabelChoice(_Strict):
    """One answer a person can pick: its stored ``value``, what they read, and its box rule.

    ``text`` is the short name on the button, ``help`` the explanation shown
    beside it. ``regions`` says what picking it means for the label's boxes:
    ``required`` needs at least one, ``forbidden`` allows none. A queue that
    labels no regions only takes ``optional``.
    """

    value: str = Field(min_length=1)
    text: str = ""
    help: str = ""
    regions: RegionRule = "optional"


class RegionKind(_Strict):
    value: str = Field(min_length=1)
    text: str = ""
    help: str = ""


class RegionSpec(_Strict):
    kinds: list[RegionKind] = Field(min_length=1)
    #: Only these image roles accept boxes; ``None`` allows every item image.
    image_roles: list[str] | None = Field(default=None, min_length=1)

    @field_validator("image_roles")
    @classmethod
    def _roles(cls, roles: list[str] | None) -> list[str] | None:
        if roles is not None and (any(not role for role in roles) or len(set(roles)) != len(roles)):
            raise ValueError("可画框的图片角色不能为空或重复")
        return roles


class Question(_Strict):
    """One of several separate questions a queue asks about an item."""

    key: str = Field(min_length=1)
    text: str = ""
    help: str = ""
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
            ("选项值", [choice.value for choice in self.choices]),
            ("框的类别", [kind.value for kind in self.regions.kinds] if self.regions else []),
            ("问题的 key", [question.key for question in self.questions or []]),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{name}不能重复")
        if self.questions is not None and (self.choices or self.multi):
            raise ValueError(
                "一个队列要么问几个问题（questions），要么给一组选项（choices），不能两者都有"
            )
        if self.questions is not None and not self.questions:
            raise ValueError("给了 questions 就至少要有一个问题")
        if self.regions is None:
            ruled = [choice.value for choice in self.all_choices() if choice.regions != "optional"]
            if ruled:
                raise ValueError(
                    f"这个队列不画框，选项 {ruled} 却规定了框（regions 只能是 optional）"
                )
        return self

    def all_choices(self) -> list[LabelChoice]:
        """The single question's choices, or every question's."""
        if self.questions is None:
            return list(self.choices)
        return [choice for question in self.questions for choice in question.choices]

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
            raise ValueError("框写作 [x0, y0, x1, y1]，要求 0 <= x0 < x1 且 0 <= y0 < y1")
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


class MarkSource(_Strict):
    """A run step whose recorded actions are drawn on a queue image, under *label*."""

    run: str = Field(min_length=1)
    step: int = Field(ge=0)
    label: str = ""


class QueueImage(_Strict):
    """One image of an item, and whose actions are drawn on it.

    ``marks_from`` ``None`` follows the image: an observation shows the
    actions its step decided on that screen, a ``browser.frames``
    before-action frame shows the action it was taken before, any other image
    none. ``[]`` draws nothing; a list draws each named step's actions, one
    colour per entry, converted into this image's pixels.
    """

    role: str = Field(min_length=1)
    text: str = ""
    source: ImageSource
    marks_from: list[MarkSource] | None = None
    section: str | None = None


class ItemField(_Strict):
    """A fact shown with the item; a ``technical`` one sits in a collapsed details section."""

    text: str
    value: str | int | float | bool | None = None
    technical: bool = False
    section: str | None = None


class QueueSection(_Strict):
    """An explicitly grouped, independently expandable part of one item's evidence."""

    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""


class GuideSection(_Strict):
    """One section of a queue's labeling guide: a heading and plain text (lines starting
    with ``- `` are list items)."""

    title: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SuggestionDisplay(_Strict):
    """How the page presents the items' suggestions.

    ``title`` heads the suggestion (``""``: the page's own wording);
    ``on_request`` keeps it hidden until the person asks to see it, so a model's
    answer cannot anchor theirs.
    """

    title: str = ""
    shown: Literal["always", "on_request"] = "always"


class ItemLink(_Strict):
    text: str
    run: str = Field(min_length=1)
    step: int | None = Field(default=None, ge=0)
    section: str | None = None


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
    sections: list[QueueSection] = Field(default_factory=list)
    #: Expose the committed history of ``run`` through ``step``, never later steps.
    trajectory: bool = False
    #: The question keys this item asks; ``None`` defers to each question's ``applies_to``.
    questions: list[str] | None = None
    suggestion: Suggestion | None = None

    @model_validator(mode="after")
    def _evidence_scope(self) -> QueueItem:
        keys = [section.key for section in self.sections]
        if len(keys) != len(set(keys)):
            raise ValueError("条目的证据分组 key 不能重复")
        for evidence in [*self.images, *self.fields, *self.links]:
            if evidence.section is not None and evidence.section not in keys:
                raise ValueError(f"证据引用了未声明的分组 {evidence.section!r}")
        if self.trajectory and (not self.run or self.step is None):
            raise ValueError("展示轨迹历史的条目需要 run 和 step")
        return self

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
    #: The labeling guide the page keeps one click away: definitions and examples.
    guide: list[GuideSection] = Field(default_factory=list)
    suggestion_display: SuggestionDisplay = Field(default_factory=SuggestionDisplay)
    label: LabelSpec
    items: list[QueueItem]

    @field_validator("queue_id")
    @classmethod
    def _file_name(cls, value: str) -> str:
        if not QUEUE_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "队列 id 由 1-128 个字母、数字、'.'、'_' 或 '-' 组成，以字母或数字开头"
            )
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ReviewQueue:
        seen: set[str] = set()
        for item in self.items:
            if item.item_id in seen:
                raise ValueError(f"item_id {item.item_id!r} 出现了不止一次")
            seen.add(item.item_id)
            roles = [image.role for image in item.images]
            if len(set(roles)) != len(roles):
                raise ValueError(f"条目 {item.item_id!r} 有重复的图片角色")
            keys = {question.key for question in self.label.questions or []}
            unknown = sorted(set(item.questions or []) - keys)
            if unknown:
                raise ValueError(f"条目 {item.item_id!r} 问了队列里没有的问题 {unknown}")
            if self.label.questions is not None and not asked_questions(self.label, item):
                raise ValueError(f"条目 {item.item_id!r} 没有问队列里的任何问题")
            if item.suggestion is not None:
                if item.suggestion.answers == {}:
                    raise ValueError(
                        f"条目 {item.item_id!r} 的建议没有任何答案；没有建议答案时请省略 'answers'"
                    )
                problems = answer_problems(
                    self.label,
                    item,
                    choice=item.suggestion.choice,
                    choices=item.suggestion.choices,
                    answers=item.suggestion.answers,
                    regions=item.suggestion.regions,
                    require_choice=False,
                )
                asked = {question.key for question in asked_questions(self.label, item)}
                for key in sorted(set(item.suggestion.reasons or {}) - asked):
                    problems.append(f"给了问题 {key!r} 的理由，但这个条目没有问它")
                if item.suggestion.regions is not None and self.label.regions is None:
                    problems.insert(0, "这个队列不画框")
                if problems:
                    raise ValueError(f"条目 {item.item_id!r} 的建议无效：{problems[0]}")
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
                raise ValueError("随手标注写 run 和 step，不写 item_id")
        elif self.item_id is None or self.run is not None or self.step is not None:
            raise ValueError("队列标注写 item_id，不写 run 和 step")
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
    regions: list[Region] | None,
    require_choice: bool,
) -> list[str]:
    """Why an answer does not fit *spec* (sizes aside); ``[]`` when it does.

    *item* names the images a region may refer to and the questions it
    asks; ``None`` means the one ad-hoc screenshot and no questions.
    *regions* ``None`` means the answer gives no boxes at all (a suggestion
    that proposes none), so the chosen options' box rules are not checked.
    Every reason is written for the person labeling, in Chinese.
    """
    problems: list[str] = []
    allowed = spec.choice_values
    picked: list[LabelChoice] = []
    if spec.questions is not None:
        if choice is not None or choices is not None:
            problems.append(
                "这个队列分几个问题作答：请提交 'answers'，不要提交 'choice' 或 'choices'"
            )
        asked = {question.key: question for question in asked_questions(spec, item)}
        for key, value in (answers or {}).items():
            question = asked.get(key)
            if question is None:
                problems.append(f"这个条目没有问问题 {key!r}")
            elif value not in question.choice_values():
                problems.append(f"{value!r} 不是问题 {key!r} 的选项")
            else:
                picked += [option for option in question.choices if option.value == value]
        missing = [key for key in asked if key not in (answers or {})]
        if require_choice and missing:
            problems.append(f"请回答这个条目的每个问题；还没回答：{missing}")
    elif answers is not None:
        problems.append("这个队列只有一个问题：不用 'answers'")
    elif spec.multi:
        if choice is not None:
            problems.append("这个队列可以多选：请提交 'choices'，不要提交 'choice'")
        for value in choices or []:
            if value not in allowed:
                problems.append(f"{value!r} 不是这个队列的选项")
        if choices is not None and len(set(choices)) != len(choices):
            problems.append("同一个选项选了两次")
        picked += [option for option in spec.choices if option.value in (choices or [])]
    else:
        if choices is not None:
            problems.append("这个队列只能单选：请提交 'choice'，不要提交 'choices'")
        if choice is not None and choice not in allowed:
            problems.append(f"{choice!r} 不是这个队列的选项")
        if require_choice and allowed and choice is None:
            problems.append("请选一个答案，或者跳过这个条目、驳回建议")
        picked += [option for option in spec.choices if option.value == choice]
    if regions and spec.regions is None:
        problems.append("这个队列不画框")
    kinds = spec.kind_values
    images = [image.role for image in item.images] if item is not None else [ADHOC_IMAGE]
    for region in regions or []:
        if spec.regions is not None and region.kind not in kinds:
            problems.append(f"{region.kind!r} 不是这个队列的框类别")
        if region.image not in images:
            problems.append(f"{region.image!r} 不是这个条目的图片")
        elif (
            spec.regions is not None
            and spec.regions.image_roles is not None
            and region.image not in spec.regions.image_roles
        ):
            problems.append(f"图片 {region.image!r} 只供查看，不能在这张图上画框")
    if regions is not None:
        problems += region_rule_problems(picked, regions)
    return problems


def region_rule_problems(picked: list[LabelChoice], regions: list[Region]) -> list[str]:
    """Why the boxes break a box rule of the picked choices (``LabelChoice.regions``)."""

    def named(options: list[LabelChoice]) -> str:
        return "、".join(f"“{option.text or option.value}”" for option in options)

    forbidding = [option for option in picked if option.regions == "forbidden"]
    requiring = [option for option in picked if option.regions == "required"]
    if forbidding and requiring:
        return [f"{named(requiring)}要画框，{named(forbidding)}不能画框，不能同时选"]
    if forbidding and regions:
        return [f"选了{named(forbidding)}就不能画框：请删掉框，或者换一个选项"]
    if requiring and not regions:
        return [f"选了{named(requiring)}就至少要画一个框"]
    return []
