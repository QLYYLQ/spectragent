"""How a recorded run reads to a person: what each capability states about its own records.

``docs/architecture_design.md`` §4.17 "动作标记", §4.2.1. The viewer's read side
(``spectragent/inspection``) names no capability: it reaches the classes a
capability already registers through their registries, and asks each, through
the small structural Protocols here, what only that capability knows.

- An **action class** (:class:`PresentsAction`) says how a recorded call of it
  reads (its verb, and the line built from its params, its effect and the
  element it named) and where it pointed, in the capability's viewport units.
- An **observation fragment class** an element param addresses
  (:class:`DescribesElements`) describes the element at an index.
- An **archiver class** may keep the screens before each action
  (:class:`KeepsActionFrames`) or a replay's serving decisions
  (:class:`LogsReplayDecisions`).
- A **restoration evidence class** (:class:`MeasuresRestorationGap`) says
  what its recorded measurements differ in, and what they did not measure.

A class that implements none of them still reads: an action as its recorded
name and params, an element as its index, an evidence as not shown. Placing a
point on an image (``schemas/observation/geometry.py::point_grid``) stays in the
framework, as do the wording helpers both capabilities and the Loop share.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol, TypeVar, runtime_checkable

from pydantic import JsonValue

from spectragent.core.actions._registrar import registered_params
from spectragent.core.actions.params import element_params, point_params
from spectragent.core.archive.source import ArchiveReader
from spectragent.schemas.action import Action, ActionResult

MarkKind = Literal["click", "drag", "type", "scroll"]
Direction = Literal["up", "down", "left", "right"]
Point = tuple[int, int]
Box = tuple[int, int, int, int]

#: How far quoted screen text, typed text and answers are shown in a line.
NAME_CHARS = 30
TYPED_CHARS = 40
ANSWER_CHARS = 80

#: Each scroll direction as a person says it (``向下滚动``).
DIRECTIONS: Mapping[str, str] = {"up": "上", "down": "下", "left": "左", "right": "右"}


class Pointer(Enum):
    """``UNMOVED``: an action left the pointer where it was."""

    UNMOVED = "unmoved"


UNMOVED = Pointer.UNMOVED


@dataclass(frozen=True)
class ElementDescription:
    """An element an action named, as its step's observation recorded it.

    ``text`` names it for a person (``#6 按钮“Save”``); ``bounds`` is where it
    was, ``(left, top, right, bottom)`` in the capability's viewport units, or
    ``None`` when the observation does not say.
    """

    text: str
    bounds: Box | None = None


@dataclass(frozen=True)
class ActionPresentation:
    """A recorded action in the capability's viewport units, before it is placed on an image.

    ``text`` is its line for a person. ``kind`` is its mark (``None``: it
    pointed nowhere and is only listed); ``point`` (and a drag's ``end``) where
    it pointed, ``box`` the bounds of the element it named, ``direction`` a
    scroll's, ``typed`` the text shown at a typing mark. ``measured``: the
    point is where the effect measured the input, not where the params asked.
    ``unplaced`` says why an action that did point has no point.
    ``pointer_after`` is where the pointer is after it (``None``: unknown;
    :data:`UNMOVED`: where it was).
    """

    kind: MarkKind | None
    text: str
    point: Point | None = None
    end: Point | None = None
    box: Box | None = None
    direction: Direction | None = None
    typed: str | None = None
    measured: bool = False
    unplaced: str | None = None
    pointer_after: Point | None | Literal[Pointer.UNMOVED] = UNMOVED


@runtime_checkable
class PresentsAction(Protocol):
    """An action class whose recorded calls read in plain words and mark where they pointed.

    Implemented on the registered action class, with ``present`` a classmethod.
    ``person_verb`` is the verb a person reads (``点击``): the head of every
    line and the viewer's step label. *element* is the element the call
    named, as its fragment described it (:class:`DescribesElements`);
    *pointer* where the pointer was before the call, in viewport units.
    """

    person_verb: str

    def present(
        self,
        action: Action,
        result: ActionResult | None,
        *,
        element: ElementDescription | None,
        pointer: Point | None,
    ) -> ActionPresentation: ...


@runtime_checkable
class DescribesElements(Protocol):
    """An observation fragment whose elements an action can name by index."""

    def describe_element(self, index: int) -> ElementDescription | None: ...


@dataclass(frozen=True)
class ArchivedFrame:
    """A screen an archiver kept beside the observations, never shown to a model.

    ``id`` names it within its run. ``collected_at`` is the step whose commit
    holds it (``None``: a trailing batch). ``kind`` is the archiver's own word
    for when it was taken; ``action_call`` the number of the environment
    action call (since its observation) it was taken just before, ``None``
    when it precedes no action, and ``action`` that call's action name.
    ``outcome`` (``captured`` or why not) and ``detail``; ``name`` its image
    among the archiver's files; ``width`` x ``height`` its pixel size.
    """

    id: str
    collected_at: int | None
    kind: str
    action_call: int | None
    action: str | None
    outcome: str
    detail: str | None
    name: str | None
    width: int | None
    height: int | None

    @property
    def has_image(self) -> bool:
        return self.outcome == "captured" and self.name is not None


@runtime_checkable
class KeepsActionFrames(Protocol):
    """An archiver class that keeps a screen before each action call; methods are classmethods."""

    def action_frames(
        self,
        reader: ArchiveReader,
        step: int,
        *,
        action_times: Sequence[tuple[float, float]] | None,
    ) -> list[ArchivedFrame]:
        """The frames of *step*'s actual action calls, whichever commit collected them."""
        ...

    def archived_frame(
        self, reader: ArchiveReader, step: int | None, name: str
    ) -> ArchivedFrame | None:
        """The frame of the image *name* committed at *step* (``None``: a trailing batch)."""
        ...

    def frame_image(self, reader: ArchiveReader, frame: ArchivedFrame) -> bytes:
        """The image of a captured *frame*."""
        ...


#: One serving decision of a replay: the step whose batch holds it (``None``:
#: trailing), the decision as JSON with its ``kind``, and how many it stands for.
ReplayDecision = tuple[int | None, dict[str, JsonValue], int]


@runtime_checkable
class LogsReplayDecisions(Protocol):
    """An archiver class that logs a replay's serving decisions; methods are classmethods."""

    #: Every kind of decision it logs, so a count of zero is a measured zero.
    decision_kinds: tuple[str, ...]

    def replay_decisions(self, reader: ArchiveReader) -> Iterator[ReplayDecision]: ...


@runtime_checkable
class MeasuresRestorationGap(Protocol):
    """A restoration evidence class that reads its own measurements; ``measured_gap`` is a classmethod.

    Given one recorded evidence as JSON, its measured differences and the
    facts it did not measure, each a line naming the capability; no tolerance
    is applied (the restorer's verdict is separate).
    """

    def measured_gap(self, evidence: Mapping[str, JsonValue]) -> tuple[list[str], list[str]]: ...


# ── reaching what a capability registers ──────────────────────────────


R = TypeVar("R")


def registered(lookup: Callable[[str], R | None], key: str) -> R | None:
    """What *lookup* finds under *key*, importing every capability package on a miss.

    A reader that composed no configuration (``spectragent view``) may not
    have imported the module that registers it, as ``registered_params`` and
    ``RunTrace`` handle for actions and fragments.
    """
    found = lookup(key)
    if found is None:
        discover_capabilities()
        found = lookup(key)
    return found


def discover_capabilities() -> None:
    """Import every capability package, so its registered classes can be found."""
    from spectragent.core.registries import CAPABILITIES_PACKAGE, discover_implementations

    discover_implementations(CAPABILITIES_PACKAGE)


# ── wording every capability's lines share ────────────────────────────


def clip(text: str, limit: int) -> str:
    """*text* on one line, cut to *limit* characters with an ellipsis."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def xy(point: Point | None) -> str:
    return f"({point[0]}, {point[1]})" if point is not None else "(?)"


def seconds(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def number(value: JsonValue | object) -> int | None:
    """*value* as an int when it is a number (never a bool), else ``None``."""
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def param_points(action: Action) -> list[Point | None]:
    """Each point *action*'s registered ``Params`` declare, as its params state it (``None``: not stated)."""
    params = registered_params(action.name)
    pairs = point_params(params) if params is not None else []
    found: list[Point | None] = []
    for x, y in pairs:
        px, py = number(action.params.get(x)), number(action.params.get(y))
        found.append((px, py) if px is not None and py is not None else None)
    return found


def element_param(action: Action) -> object:
    """The element *action*'s params name (its first ``element_params`` value), or ``None``."""
    params = registered_params(action.name)
    for name in element_params(params) if params is not None else ():
        if action.params.get(name) is not None:
            return action.params[name]
    return None


def unworded(action: Action) -> ActionPresentation:
    """An action no class words: its recorded name and params."""
    shown = json.dumps(action.params, ensure_ascii=False) if action.params else ""
    return ActionPresentation(None, f"{action.name} {shown}".strip())


__all__ = [
    "ANSWER_CHARS",
    "DIRECTIONS",
    "NAME_CHARS",
    "TYPED_CHARS",
    "UNMOVED",
    "ActionPresentation",
    "ArchivedFrame",
    "Box",
    "DescribesElements",
    "Direction",
    "ElementDescription",
    "KeepsActionFrames",
    "LogsReplayDecisions",
    "MarkKind",
    "MeasuresRestorationGap",
    "Point",
    "Pointer",
    "PresentsAction",
    "ReplayDecision",
    "clip",
    "discover_capabilities",
    "element_param",
    "number",
    "param_points",
    "registered",
    "seconds",
    "unworded",
    "xy",
]
