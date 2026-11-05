"""Where a recorded action pointed, drawn as marks on an image, and how it reads to a person.

The one place a recorded action becomes a mark and a line, for the viewer's
run page and review page (``/api/marks``, ``/api/review/marks``), its step
labels (the run API's ``action_verbs``) and the lookahead queues' action
lines. Everything is read from a committed run through :class:`RunTrace`, and
nothing here names a capability (``docs/architecture_design.md`` §4.17
"动作标记"): what only a capability knows it states itself, on the classes it
registers, through the Protocols of ``core/presentation.py``.

- How an action reads and where it pointed is its action class's
  (``PresentsAction``), found by the recorded action's name. A class that
  says nothing keeps its recorded name and params.
- The element an action named by index is described by the fragment its
  element param declares it addresses (``AddressesElement``), when that
  fragment's class describes elements (``DescribesElements``); else the
  element is named by its index. A fragment that does not is never read.
- A screen an archiver kept before an action is its archiver's to find
  (``KeepsActionFrames``).

Points are in the capability's viewport units. A mark is in the pixels of the
image it is drawn on, converted with that image's recorded geometry, its own
pixel size and the viewport it depicts, by the one conversion rule
(:func:`~spectragent.schemas.observation.geometry.point_grid`); device scale 1
is never assumed. An image whose geometry is unknown gets its actions listed
and none drawn, and says why. The image is the step's one screenshot, and the
pointer the step's one capture state, each found by its observation field
name (``screenshot``, ``capture_state``), which negotiation keeps unique in a
run, as ``RunTrace.screenshot`` finds it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from spectragent.core.actions._registrar import (
    ActionRegistrar,
    registered_params,
)
from spectragent.core.actions.params import element_params, param_fragments
from spectragent.core.archive._registrar import ArchiverRegistrar
from spectragent.core.presentation import (
    UNMOVED,
    ActionPresentation,
    ArchivedFrame,
    DescribesElements,
    Direction,
    ElementDescription,
    KeepsActionFrames,
    MarkKind,
    Point,
    PresentsAction,
    discover_capabilities,
    number,
    registered,
    unworded,
)
from spectragent.core.trace.errors import RecordingScopeError, TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import (
    FragmentBase,
    ObservationRegistrar,
    fragment_field_name,
)
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.geometry import PointGrid, ScreenshotGeometry, point_grid
from spectragent.schemas.step_record import StepRecord


class ActionMark(BaseModel):
    """One recorded action: its line for a person and, when it pointed somewhere, its mark.

    ``number`` is the action's 1-based position in its step's action list,
    the number drawn on the image. ``x``/``y`` (and a drag's ``end_x``/``end_y``)
    are pixels of the image the marks belong to; ``None`` when the action has
    no point or its point is not known there, ``unplaced`` saying why for an
    action that did point.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    name: str
    text: str
    kind: MarkKind | None = None
    x: int | None = None
    y: int | None = None
    end_x: int | None = None
    end_y: int | None = None
    #: The bounds of the element the action named, ``(left, top, right, bottom)``
    #: in the image's pixels, as its step's observation recorded them.
    box: tuple[int, int, int, int] | None = None
    direction: Direction | None = None
    typed: str | None = None
    #: The point is where the action's effect measured the input, not its parameters.
    measured: bool = False
    #: ``ActionResult.success``; ``None`` when no result was recorded.
    succeeded: bool | None = None
    error: str | None = None
    unplaced: str | None = None


class MarkSet(BaseModel):
    """The actions of one run step drawn on an image, under one label (one colour)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: str
    step: int
    label: str = ""
    marks: list[ActionMark] = Field(default_factory=list)
    #: Why this step's actions are not listed (not committed, not recorded).
    problem: str | None = None


class ImageMarks(BaseModel):
    """Every set of marks for one image, in the pixels of ``width`` x ``height``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    width: int | None = None
    height: int | None = None
    sets: list[MarkSet] = Field(default_factory=list)
    #: Why no mark can be placed on this image (its geometry is unknown).
    problem: str | None = None


class ImagePointer(BaseModel):
    """The pointer recorded with an image, distinct from later action targets.

    ``x``/``y`` use image pixels; ``viewport_x``/``viewport_y`` retain the
    measured viewport position even when missing image geometry prevents drawing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    width: int | None = None
    height: int | None = None
    status: Literal["known", "unknown", "unavailable"] = "unknown"
    x: int | None = None
    y: int | None = None
    viewport_x: int | None = None
    viewport_y: int | None = None
    source: Literal["capture_state", "screenshot_cursor"] | None = None
    problem: str | None = None


@dataclass(frozen=True)
class Geometry:
    """How CSS viewport pixels land on one image; ``grid`` ``None`` with the reason."""

    grid: PointGrid | None
    width: int | None
    height: int | None
    problem: str | None = None


@dataclass(frozen=True)
class MarkRequest:
    """A run step whose actions to draw; ``only`` restricts them to these action indices.

    ``problem`` says why the actions the image should show cannot be found.
    """

    run: str
    step: int
    label: str = ""
    only: frozenset[int] | None = None
    problem: str | None = None


#: Opens a run by its repository key (the viewer's own trace opener).
TraceOpener = Callable[[str], RunTrace]


# ── what a capability registers ───────────────────────────────────────


def presenter(name: str) -> PresentsAction | None:
    """The class of the action recorded as *name*, when it says how it reads (``PresentsAction``)."""
    action = registered(ActionRegistrar.get_or_none, name)
    return action if isinstance(action, PresentsAction) else None


def _describing(key: str) -> bool:
    """Whether the fragment class registered under *key* describes its elements."""
    fragment = registered(ObservationRegistrar.get_or_none, key)
    return isinstance(fragment, type) and issubclass(fragment, DescribesElements)


def frame_keeper(archiver: str) -> KeepsActionFrames | None:
    """The archiver class registered under *archiver*, when it keeps screens before actions."""
    found = registered(ArchiverRegistrar.get_or_none, archiver)
    return found if isinstance(found, KeepsActionFrames) else None


def frame_keepers() -> list[KeepsActionFrames]:
    """Every registered archiver class that keeps screens before actions."""
    discover_capabilities()
    return [
        found
        for found in ArchiverRegistrar.get_all().values()
        if isinstance(found, KeepsActionFrames)
    ]


# ── reading a run ──────────────────────────────────────────────────────


#: The observation fields a mark reads: the step's screenshot and its capture
#: state, whichever capability provides them. Negotiation keeps a field name
#: unique in a run, so each names at most one fragment of a step.
_SCREENSHOT = "screenshot"
_CAPTURE_STATE = "capture_state"


def _fragment(trace: RunTrace, step: int, key: str) -> FragmentBase | FragmentFailure | None:
    """*step*'s committed fragment *key*, or ``None`` when absent, failed or not recorded."""
    entry = next((entry for entry in trace.manifest.steps if entry.step == step), None)
    if entry is None:
        return None
    files = entry.fragments.get(key)
    if files is None or files.failed:
        return None
    try:
        return trace.observation_fragment(step, key)
    except RecordingScopeError:
        return None


def _field(trace: RunTrace, step: int, field: str) -> FragmentBase | FragmentFailure | None:
    """*step*'s committed fragment whose observation field is *field* (``screenshot``), or ``None``."""
    entry = next((entry for entry in trace.manifest.steps if entry.step == step), None)
    if entry is None:
        return None
    keys = [key for key in entry.fragments if fragment_field_name(key) == field]
    return _fragment(trace, step, keys[0]) if len(keys) == 1 else None


def addressed_elements(trace: RunTrace, record: StepRecord) -> list[ElementDescription | None]:
    """For each of *record*'s actions, the element its params named, from its step's observation.

    The fragment read is the one the action's element param declares it
    addresses, and only when its class describes elements
    (``DescribesElements``); any other (one that can be tens of MB) is never
    read, and its element is named by its index alone. ``None`` for an action
    that names no element.
    """
    found: list[ElementDescription | None] = []
    read: dict[str, FragmentBase | FragmentFailure | None] = {}
    for action in record.actions:
        params = registered_params(action.name)
        if params is None:
            found.append(None)
            continue
        fragments = param_fragments(params)
        element: ElementDescription | None = None
        for name in element_params(params):
            index = number(action.params.get(name))
            if index is None:
                continue
            for key in sorted(fragments.get(name, ())):
                if not _describing(key):
                    continue
                if key not in read:
                    try:
                        read[key] = _fragment(trace, record.step, key)
                    except (TraceSchemaError, KeyError, ValueError):
                        read[key] = None
                fragment = read[key]
                if isinstance(fragment, DescribesElements):
                    element = fragment.describe_element(index)
            element = element or ElementDescription(f"#{index}")
            break
        found.append(element)
    return found


def observation_geometry(trace: RunTrace, step: int) -> Geometry:
    """The geometry of *step*'s committed screenshot (its image and viewport sizes)."""
    try:
        shot = _field(trace, step, _SCREENSHOT)
    except (TraceSchemaError, KeyError, ValueError) as exc:
        return Geometry(None, None, None, f"这一步的截图信息读不出来：{exc}")
    if not isinstance(shot, ScreenshotGeometry):
        return Geometry(None, None, None, "这一步没有记录截图的尺寸，动作只列出、不画出")
    width, height = shot.image_width or None, shot.image_height or None
    grid = point_grid("screenshot_pixels", shot)
    if grid is None:
        return Geometry(None, width, height, "截图没有记录视口或图片尺寸，动作只列出、不画出")
    return Geometry(grid, width, height)


def frame_geometry(trace: RunTrace, step: int | None, frame: ArchivedFrame | None) -> Geometry:
    """A kept frame's geometry: its own pixel size over the viewport of the observation of *step*.

    For an action frame, callers that resolve its execution owner pass that
    step, independently of which archive collection committed the image.
    """
    if frame is None:
        return Geometry(None, None, None, "找不到这张画面的记录，动作只列出、不画出")
    if step is None:
        return Geometry(
            None,
            frame.width,
            frame.height,
            "这张画面属于收尾记录，不知道它的视口，动作只列出、不画出",
        )
    observed = observation_geometry(trace, step)
    if observed.grid is None or not frame.width or not frame.height:
        return Geometry(
            None,
            frame.width,
            frame.height,
            observed.problem or "这张画面没有记录尺寸，动作只列出、不画出",
        )
    grid = PointGrid(
        "screenshot_pixels",
        frame.width,
        frame.height,
        observed.grid.viewport_width,
        observed.grid.viewport_height,
    )
    return Geometry(grid, frame.width, frame.height)


def executed_action(record: StepRecord, call: int, name: str | None) -> int | None:
    """The index into ``record.actions`` of the step's ``env.act`` call number *call*.

    A frame is numbered by environment call (``StepRecord.action_spans``). Two
    kinds of recorded action never reach the environment: one the guard
    refused, and the Loop's own ``loop.*`` action (``loop.done``), which the
    Loop executes itself. So the recorded actions are aligned with the spans
    by name, in order (neither name is ever a span's). Without spans, the
    action at *call* if its name is *name*.
    """
    spans = record.action_spans
    if spans is None:
        if call < len(record.actions) and (name is None or record.actions[call].name == name):
            return call
        return None
    position = 0
    for index, span in enumerate(spans):
        while position < len(record.actions) and record.actions[position].name != span.action:
            position += 1
        if position >= len(record.actions):
            return None
        if index == call:
            return position
        position += 1
    return None


def _initial_pointer(trace: RunTrace, step: int) -> Point | None:
    """The pointer position *step*'s observation recorded, in CSS px, or ``None``."""
    try:
        state = _field(trace, step, _CAPTURE_STATE)
        pointer = getattr(state, "pointer", None)
        if pointer is not None and pointer.x is not None and pointer.y is not None:
            return pointer.x, pointer.y
        shot = _field(trace, step, _SCREENSHOT)
    except (TraceSchemaError, KeyError, ValueError):
        return None
    cursor = getattr(shot, "cursor", None)
    position = getattr(cursor, "position", None)
    return (int(position[0]), int(position[1])) if position is not None else None


# ── one action ─────────────────────────────────────────────────────────


def _present(
    action: Action,
    result: ActionResult | None,
    pointer: Point | None,
    element: ElementDescription | None = None,
) -> ActionPresentation:
    """How *action* reads and where it pointed, as its class says; else its name and params."""
    action_class = presenter(action.name)
    if action_class is None:
        return unworded(action)
    return action_class.present(action, result, element=element, pointer=pointer)


def action_verb(name: str) -> str:
    """The verb a person reads for the action recorded as *name* (``点击``); else *name* itself."""
    action_class = presenter(name)
    return action_class.person_verb if action_class is not None else name


def action_verbs(names: Iterable[str]) -> dict[str, str]:
    """Each of *names* with its verb (:func:`action_verb`), for a page that labels actions."""
    return {name: action_verb(name) for name in names}


def action_text(
    action: Action, result: ActionResult | None, element: ElementDescription | None = None
) -> str:
    """One Chinese line for a recorded action, with ``（没有成功）`` when it failed.

    *element* is the element the action named, as its step recorded it
    (:func:`addressed_elements`); without it an element is named by its index.
    """
    line = _present(action, result, None, element).text
    return line + ("（没有成功）" if result is not None and not result.success else "")


def _not_executed(record: StepRecord, index: int) -> str | None:
    """``（没有执行：第 N 个动作失败）`` for an action *record* skipped after a failed one, else ``None``.

    The Loop stops a step at its first failed action; the actions after it
    are recorded but never ran (``StepRecord.skipped_actions``).
    """
    if index < len(record.actions) - record.skipped_actions:
        return None
    return f"（没有执行：第 {len(record.action_results)} 个动作失败）"


def action_lines(
    record: StepRecord, elements: Sequence[ElementDescription | None] | None = None
) -> str:
    """A step's actions, one numbered Chinese line each (``1. 点击 (223, 234) → 按钮“最多播放”``).

    *elements* are :func:`addressed_elements` of the step, one per action.
    """
    lines = []
    for index, action in enumerate(record.actions):
        result = record.action_results[index] if index < len(record.action_results) else None
        element = elements[index] if elements is not None and index < len(elements) else None
        line = action_text(action, result, element) + (_not_executed(record, index) or "")
        lines.append(f"{index + 1}. {line}")
    return "\n".join(lines) or "（没有动作）"


def _place(point: tuple[int, int] | None, grid: PointGrid | None) -> tuple[int, int] | None:
    return grid.to_grid(*point) if point is not None and grid is not None else None


def _place_box(
    box: tuple[int, int, int, int] | None, grid: PointGrid | None
) -> tuple[int, int, int, int] | None:
    if box is None or grid is None:
        return None
    left, top = grid.to_grid(box[0], box[1])
    right, bottom = grid.to_grid(box[2], box[3])
    return left, top, right, bottom


def step_marks(
    record: StepRecord,
    geometry: Geometry,
    *,
    pointer: tuple[int, int] | None = None,
    only: frozenset[int] | None = None,
    elements: Sequence[ElementDescription | None] | None = None,
) -> list[ActionMark]:
    """The marks of *record*'s actions on an image of *geometry*.

    *pointer* is where the pointer was before the first action (CSS px);
    *only* keeps just these action indices (still numbered by their place in
    the step); *elements* are :func:`addressed_elements` of the step.
    """
    marks: list[ActionMark] = []
    for index, action in enumerate(record.actions):
        result = record.action_results[index] if index < len(record.action_results) else None
        element = elements[index] if elements is not None and index < len(elements) else None
        css = _present(action, result, pointer, element)
        failed = result is not None and not result.success
        not_executed = _not_executed(record, index)
        if not_executed is not None:
            # Never ran: listed, not drawn, and the pointer did not move.
            if only is None or index in only:
                marks.append(
                    ActionMark(
                        number=index + 1,
                        name=action.name,
                        text=css.text + not_executed,
                        kind=css.kind,
                        unplaced="没有执行，不画在图上" if css.kind is not None else None,
                    )
                )
            continue
        if css.pointer_after is not UNMOVED:
            # A pointer action that failed before measuring anything may not
            # have moved the pointer at all.
            pointer = None if failed and not css.measured else css.pointer_after
        if only is not None and index not in only:
            continue
        point, end = _place(css.point, geometry.grid), _place(css.end, geometry.grid)
        box = _place_box(css.box, geometry.grid)
        unplaced = css.unplaced
        if css.kind is not None and unplaced is None and point is None:
            unplaced = geometry.problem
        marks.append(
            ActionMark(
                number=index + 1,
                name=action.name,
                text=css.text + ("（没有成功）" if failed else ""),
                kind=css.kind,
                x=point[0] if point else None,
                y=point[1] if point else None,
                end_x=end[0] if end else None,
                end_y=end[1] if end else None,
                box=box,
                direction=css.direction,
                typed=css.typed,
                measured=css.measured,
                succeeded=result.success if result is not None else None,
                error=result.error if result is not None else None,
                unplaced=unplaced,
            )
        )
    return marks


# ── marks for an image ────────────────────────────────────────────────


def mark_set(trace: RunTrace, request: MarkRequest, geometry: Geometry) -> MarkSet:
    """The actions of *request*'s run step, drawn with *geometry* (the image's, not the step's)."""
    base = {"run": request.run, "step": request.step, "label": request.label}
    if request.problem is not None:
        return MarkSet(**base, problem=request.problem)
    if not trace.config.recording.trajectory:
        return MarkSet(**base, problem="这次运行没有记录动作")
    record = trace.step(request.step)
    if record is None:
        return MarkSet(**base, problem=f"这次运行没有已提交的第 {request.step} 步")
    marks = step_marks(
        record,
        geometry,
        pointer=_initial_pointer(trace, request.step),
        only=request.only,
        elements=addressed_elements(trace, record),
    )
    return MarkSet(**base, marks=marks)


def marks_on(
    geometry: Geometry, requests: Sequence[MarkRequest], open_trace: TraceOpener
) -> ImageMarks:
    """Every requested step's actions on an image of *geometry*, one set per request."""
    sets = [mark_set(open_trace(request.run), request, geometry) for request in requests]
    return ImageMarks(
        width=geometry.width, height=geometry.height, sets=sets, problem=geometry.problem
    )


def observation_marks(trace: RunTrace, run: str, step: int) -> ImageMarks:
    """*step*'s actions on its own screenshot: what it decided on that screen."""
    return marks_on(
        observation_geometry(trace, step), [observation_request(run, step)], lambda _key: trace
    )


def observation_pointer(trace: RunTrace, step: int) -> ImagePointer:
    """The pointer of the observation itself; action results never supply it."""
    geometry = observation_geometry(trace, step)
    try:
        state = _field(trace, step, _CAPTURE_STATE)
        pointer = getattr(state, "pointer", None)
        if pointer is not None:
            if pointer.status != "known":
                return ImagePointer(
                    width=geometry.width,
                    height=geometry.height,
                    status=pointer.status,
                    source="capture_state",
                    problem="这个环境没有鼠标"
                    if pointer.status == "unavailable"
                    else "观察时鼠标位置未知",
                )
            position = (pointer.x, pointer.y)
            source = "capture_state"
        else:
            shot = _field(trace, step, _SCREENSHOT)
            position = getattr(getattr(shot, "cursor", None), "position", None)
            source = "screenshot_cursor"
    except (TraceSchemaError, KeyError, ValueError) as exc:
        return ImagePointer(
            width=geometry.width, height=geometry.height, problem=f"观察时鼠标位置读不出来：{exc}"
        )
    if position is None:
        return ImagePointer(
            width=geometry.width, height=geometry.height, problem="这次观察没有记录鼠标位置"
        )
    viewport = (int(position[0]), int(position[1]))
    point = _place(viewport, geometry.grid)
    return ImagePointer(
        width=geometry.width,
        height=geometry.height,
        status="known",
        x=point[0] if point is not None else None,
        y=point[1] if point is not None else None,
        viewport_x=viewport[0],
        viewport_y=viewport[1],
        source=source,
        problem=geometry.problem,
    )


def before_action_marks(trace: RunTrace, run: str, step: int, frame: ArchivedFrame) -> ImageMarks:
    """Marks for a frame already associated with *step*'s actual action call.

    The step is its execution owner, not the later archive collection batch.
    """
    geometry = frame_geometry(trace, step, frame)
    record = trace.step(step) if trace.config.recording.trajectory else None
    index = (
        executed_action(record, frame.action_call, frame.action)
        if record is not None and frame.action_call is not None
        else None
    )
    request = MarkRequest(
        run,
        step,
        "这张画面之后马上执行的动作",
        only=frozenset({index}) if index is not None else None,
        problem="找不到这张画面之后执行的那个动作" if index is None else None,
    )
    return marks_on(geometry, [request], lambda _key: trace)


def observation_request(run: str, step: int) -> MarkRequest:
    """An observation's own actions: what its step decided on that screen."""
    return MarkRequest(run, step, f"第 {step} 步在这一屏上做的动作")


def default_requests(
    trace: RunTrace, run: str, step: int | None, archiver: str | None, name: str | None
) -> tuple[Geometry, list[MarkRequest]]:
    """An image's geometry and whose actions it shows when its queue names none.

    An observation shows its step's actions; a frame kept before an action
    (its archiver's, ``KeepsActionFrames``) the action it was taken before;
    any other image none.
    """
    if archiver is None:
        assert step is not None, "an observation image names its step"
        return observation_geometry(trace, step), [observation_request(run, step)]
    keeper = frame_keeper(archiver)
    if keeper is None or name is None:
        return Geometry(
            None, None, None, "这张图不是截图或画面帧，不知道它的视口，动作只列出、不画出"
        ), []
    frame = keeper.archived_frame(trace, step, name)
    geometry = frame_geometry(trace, step, frame)
    if frame is None or step is None or frame.action_call is None:
        return geometry, []
    step_record = trace.step(step) if trace.config.recording.trajectory else None
    index = (
        executed_action(step_record, frame.action_call, frame.action)
        if step_record is not None
        else None
    )
    label = "这张画面之后马上执行的动作"
    if index is None:
        return geometry, [MarkRequest(run, step, label, problem="找不到这张画面之后执行的那个动作")]
    return geometry, [MarkRequest(run, step, label, only=frozenset({index}))]


def image_geometry(
    trace: RunTrace, step: int | None, archiver: str | None, name: str | None
) -> Geometry:
    """The geometry of an observation (``archiver`` ``None``) or of a kept frame."""
    return default_requests(trace, "", step, archiver, name)[0]


__all__ = [
    "ActionMark",
    "Geometry",
    "ImageMarks",
    "ImagePointer",
    "MarkRequest",
    "MarkSet",
    "action_lines",
    "action_text",
    "action_verb",
    "action_verbs",
    "addressed_elements",
    "before_action_marks",
    "default_requests",
    "executed_action",
    "frame_geometry",
    "frame_keeper",
    "frame_keepers",
    "image_geometry",
    "mark_set",
    "marks_on",
    "observation_geometry",
    "observation_marks",
    "observation_pointer",
    "presenter",
    "step_marks",
]
