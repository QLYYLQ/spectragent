"""Where a recorded action pointed, drawn as marks on an image, and how it reads to a person.

The one place a recorded browser action becomes a mark. The viewer serves
these marks to its run page and its review page (``/api/marks``,
``/api/review/marks``); the lookahead queue builders write the same Chinese
action lines into their items. Everything is read from a committed run through
:class:`RunTrace`.

Action parameters and measured effects are CSS viewport pixels
(``docs/architecture_design.md`` §4.1). A mark is in the pixels of the image it
is drawn on, converted with that image's recorded geometry — its own pixel
size and the viewport it depicts — by the one conversion rule
(:func:`~spectragent.schemas.observation.geometry.point_grid`); device scale 1
is never assumed. An image whose geometry is unknown gets its actions listed
and none drawn, and says why.

Which point a mark uses: the point the action's effect measured (where the
input was actually dispatched) when there is one, else the point its
parameters named. A page scroll is dispatched where the pointer was, so its
mark sits at the pointer position the step's observation recorded, moved by
the pointer actions before it in the same step; unknown is left unknown.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from spectragent.core.capabilities.browser.archive.frames import (
    FRAMES_ARCHIVER_ID,
    FrameRecord,
    iter_frames,
    read_frames,
)
from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    PointerEffect,
    ScrollEffect,
    SelectionEffect,
    TextInputEffect,
    ViewportPoint,
    WaitEffect,
)
from spectragent.core.trace.errors import RecordingScopeError, TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation.geometry import PointGrid, ScreenshotGeometry, point_grid
from spectragent.schemas.step_record import StepRecord

MarkKind = Literal["click", "drag", "type", "scroll"]
Direction = Literal["up", "down", "left", "right"]

#: How far quoted page text, typed text and answers are shown in a line.
NAME_CHARS = 30
TYPED_CHARS = 40
ANSWER_CHARS = 80

_DIRECTIONS = {"up": "上", "down": "下", "left": "左", "right": "右"}
_EDGES = {"up": "顶", "down": "底", "left": "最左边", "right": "最右边"}
#: ARIA roles as a person names the thing on screen; others keep the role word.
_ROLES = {
    "button": "按钮",
    "link": "链接",
    "textbox": "输入框",
    "searchbox": "搜索框",
    "combobox": "下拉框",
    "listbox": "列表框",
    "checkbox": "复选框",
    "radio": "单选项",
    "switch": "开关",
    "tab": "标签",
    "menuitem": "菜单项",
    "menuitemcheckbox": "菜单项",
    "menuitemradio": "菜单项",
    "option": "选项",
    "slider": "滑块",
    "spinbutton": "数字框",
    "treeitem": "树节点",
    "img": "图片",
    "image": "图片",
    "heading": "标题",
    "listitem": "列表项",
    "list": "列表",
    "row": "行",
    "cell": "单元格",
    "gridcell": "单元格",
    "dialog": "对话框",
    "paragraph": "段落",
    "navigation": "导航栏",
    "generic": "元素",
    "text": "文字",
    "StaticText": "文字",
    "video": "视频",
    "article": "文章",
    "region": "区域",
    "main": "主体区域",
    "banner": "页头",
    "contentinfo": "页脚",
    "form": "表单",
    "figure": "图",
    "iframe": "内嵌页面",
    "document": "页面",
    "presentation": "元素",
    "none": "元素",
    "svg": "图标",
    "graphics-symbol": "图标",
    "group": "分组",
    "toolbar": "工具栏",
    "menu": "菜单",
    "menubar": "菜单栏",
    "tablist": "标签栏",
    "tabpanel": "标签页内容",
    "table": "表格",
    "grid": "表格",
    "columnheader": "表头",
    "rowheader": "表头",
    "alert": "提示",
    "status": "状态提示",
    "progressbar": "进度条",
    "scrollbar": "滚动条",
    "separator": "分隔线",
    "complementary": "侧栏",
    "search": "搜索区域",
    "section": "区域",
}


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


# ── reading a run ──────────────────────────────────────────────────────


def _local(name: str) -> str:
    return name.rpartition(".")[2]


def _fragment_named(trace: RunTrace, step: int, local: str) -> Any:
    """*step*'s fragment whose local name is *local* (``screenshot``), or ``None``."""
    entry = next((entry for entry in trace.manifest.steps if entry.step == step), None)
    if entry is None:
        return None
    for key, files in entry.fragments.items():
        if _local(key) == local and not files.failed:
            try:
                return trace.observation_fragment(step, key)
            except RecordingScopeError:
                return None
    return None


def observation_geometry(trace: RunTrace, step: int) -> Geometry:
    """The geometry of *step*'s committed screenshot (its image and viewport sizes)."""
    try:
        shot = _fragment_named(trace, step, "screenshot")
    except (TraceSchemaError, KeyError, ValueError) as exc:
        return Geometry(None, None, None, f"这一步的截图信息读不出来：{exc}")
    if not isinstance(shot, ScreenshotGeometry):
        return Geometry(None, None, None, "这一步没有记录截图的尺寸，动作只列出、不画出")
    width, height = shot.image_width or None, shot.image_height or None
    grid = point_grid("screenshot_pixels", shot)
    if grid is None:
        return Geometry(None, width, height, "截图没有记录视口或图片尺寸，动作只列出、不画出")
    return Geometry(grid, width, height)


def frame_record(trace: RunTrace, step: int | None, name: str) -> FrameRecord | None:
    """The ``browser.frames`` record of the image *name* committed at *step* (``None``: trailing)."""
    records = (
        read_frames(trace, step)
        if step is not None
        else [record for at, record in iter_frames(trace, include_trailing=True) if at is None]
    )
    return next((record for record in records if record.name == name), None)


def frame_geometry(trace: RunTrace, step: int | None, record: FrameRecord | None) -> Geometry:
    """A frame's geometry: its own pixel size over the viewport of the observation of *step*.

    A frame shows the tab the observation of the step whose commit took it
    showed, so that observation's viewport is the area it depicts.
    """
    if record is None:
        return Geometry(None, None, None, "找不到这张画面的记录，动作只列出、不画出")
    if step is None:
        return Geometry(
            None,
            record.width,
            record.height,
            "这张画面属于收尾记录，不知道它的视口，动作只列出、不画出",
        )
    observed = observation_geometry(trace, step)
    if observed.grid is None or not record.width or not record.height:
        return Geometry(
            None,
            record.width,
            record.height,
            observed.problem or "这张画面没有记录尺寸，动作只列出、不画出",
        )
    grid = PointGrid(
        "screenshot_pixels",
        record.width,
        record.height,
        observed.grid.viewport_width,
        observed.grid.viewport_height,
    )
    return Geometry(grid, record.width, record.height)


def executed_action(record: StepRecord, call: int, name: str | None) -> int | None:
    """The index into ``record.actions`` of the step's ``env.act`` call number *call*.

    A frame is numbered by environment call (``StepRecord.action_spans``); an
    action the guard refused never reached the environment, so the recorded
    actions are aligned with the spans by name, in order (a refused action's
    name is never an executable one). Without spans, the action at *call* if
    its name is *name*.
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


def _initial_pointer(trace: RunTrace, step: int) -> tuple[int, int] | None:
    """The pointer position *step*'s observation recorded, in CSS px, or ``None``."""
    try:
        state = _fragment_named(trace, step, "capture_state")
        pointer = getattr(state, "pointer", None)
        if pointer is not None and pointer.x is not None and pointer.y is not None:
            return pointer.x, pointer.y
        shot = _fragment_named(trace, step, "screenshot")
    except (TraceSchemaError, KeyError, ValueError):
        return None
    cursor = getattr(shot, "cursor", None)
    position = getattr(cursor, "position", None)
    return (int(position[0]), int(position[1])) if position is not None else None


# ── one action ─────────────────────────────────────────────────────────


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def element_text(fact: Any) -> str | None:
    """An effect's element as a person names it (``按钮“最多播放”``), or ``None`` when unknown."""
    if isinstance(fact, ElementFacts):
        role = _ROLES.get(fact.role, fact.role)
        return f"{role}“{_clip(fact.name, NAME_CHARS)}”" if fact.name else role
    if fact == "nothing":
        return "空白处"
    return None


def _number(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _param_point(params: dict[str, Any], x: str, y: str) -> tuple[int, int] | None:
    px, py = _number(params.get(x)), _number(params.get(y))
    return (px, py) if px is not None and py is not None else None


def _point(fact: Any) -> tuple[int, int] | None:
    return (fact.x, fact.y) if isinstance(fact, ViewportPoint) else None


def _seconds(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class _Css:
    """An action in CSS px, before it is placed on an image."""

    kind: MarkKind | None
    text: str
    point: tuple[int, int] | None = None
    end: tuple[int, int] | None = None
    direction: Direction | None = None
    typed: str | None = None
    measured: bool = False
    unplaced: str | None = None
    #: Where the pointer is after this action; ``...`` (Ellipsis): it did not move.
    pointer_after: Any = ...


def _typed(params: dict[str, Any], result: ActionResult | None, effect: Any) -> str | None:
    """The typed text as a person may read it; withheld for a sensitive field."""
    text = params.get("text")
    if not isinstance(text, str):
        return None
    sensitive = (result is not None and result.typed_into_sensitive_field) or (
        isinstance(effect, TextInputEffect) and effect.sensitive is True
    )
    return None if sensitive else text


def _css(action: Action, result: ActionResult | None, pointer: tuple[int, int] | None) -> _Css:
    local = _local(action.name)
    params = action.params
    effect = result.effect if result is not None else None
    if local in ("click_coordinate", "click", "drag"):
        measured = _point(effect.point) if isinstance(effect, PointerEffect) else None
        hit = element_text(effect.hit) if isinstance(effect, PointerEffect) else None
        if local == "drag":
            start = measured or _param_point(params, "start_x", "start_y")
            release = (
                _point(effect.release_point)
                if isinstance(effect, PointerEffect) and effect.release_point is not None
                else None
            )
            end = release or _param_point(params, "end_x", "end_y")
            line = (f"拖动 {_xy(start)} → {_xy(end)}" if start and end else "拖动") + (
                f"，按在{hit}上" if hit else ""
            )
            return _Css(
                "drag",
                line,
                point=start,
                end=end,
                measured=measured is not None,
                unplaced=None if start and end else "没有记录拖动的起点或终点",
                pointer_after=end,
            )
        point = measured or _param_point(params, "coordinate_x", "coordinate_y")
        if point is not None:
            line = f"点击 {_xy(point)}" + (f" → {hit}" if hit else "")
        else:
            index = params.get("index")
            line = f"点击元素 #{index}" if index is not None else "点击"
            line += f" → {hit}" if hit else ""
        return _Css(
            "click",
            line,
            point=point,
            measured=measured is not None,
            unplaced=None if point else "没有记录点击的位置",
            pointer_after=point,
        )
    if local in ("type_coordinate", "type"):
        typed = _typed(params, result, effect)
        target = element_text(effect.target) if isinstance(effect, TextInputEffect) else None
        if not isinstance(params.get("text"), str):
            said = "输入"
        elif typed is None:
            said = "输入（敏感字段，内容不显示）"
        else:
            said = f"输入“{_clip(typed, TYPED_CHARS)}”"
        if target:
            said += f" → {target}"
        elif params.get("index") is not None:
            said += f" → 元素 #{params['index']}"
        if local == "type":
            return _Css(None, said, pointer_after=None)
        point = _param_point(params, "coordinate_x", "coordinate_y")
        return _Css(
            "type",
            said,
            point=point,
            typed=_clip(typed, TYPED_CHARS) if typed is not None else None,
            unplaced=None if point else "没有记录输入的位置",
            pointer_after=point,
        )
    if local == "scroll":
        direction = params.get("direction")
        if isinstance(effect, ScrollEffect):
            direction = effect.direction
        word = _DIRECTIONS.get(str(direction), "")
        line = f"向{word}滚动" if word else "滚动"
        amount = _number(params.get("amount"))
        delta = effect.delta if isinstance(effect, ScrollEffect) else None
        if delta is not None and isinstance(effect, ScrollEffect):
            moved = abs(delta.y if effect.direction in ("up", "down") else delta.x)
            what = "页面" if effect.container == "page" else "区域"
            line += f"，{what}移动了 {moved} 像素" if moved else "，没有移动"
        elif amount is not None:
            line += f" {amount} 像素"
        if isinstance(effect, ScrollEffect) and effect.at_boundary is True:
            line += f"，已到{_EDGES.get(effect.direction, '边缘')}"
        by_element = params.get("index") is not None
        point = None if by_element else pointer
        unplaced = (
            "滚动的是页面中的某个元素，没有记录滚轮的位置"
            if by_element
            else None
            if point
            else "滚动时鼠标的位置没有记录"
        )
        return _Css(
            "scroll",
            line,
            point=point,
            direction=direction if direction in _DIRECTIONS else None,
            unplaced=unplaced,
            pointer_after=None if by_element else ...,
        )
    if local == "send_keys":
        return _Css(None, f"按键 {params.get('keys', '')}".strip())
    if local == "wait":
        if isinstance(effect, WaitEffect):
            return _Css(None, f"等待 {_seconds(effect.waited_ms / 1000)} 秒")
        seconds = params.get("seconds")
        if isinstance(seconds, int | float) and not isinstance(seconds, bool):
            return _Css(None, f"等待 {_seconds(float(seconds))} 秒")
        return _Css(None, "等待")
    if local == "done":
        answer = params.get("extracted_content")
        head = "结束（报告没有完成任务）" if params.get("success") is False else "完成并回答"
        if isinstance(answer, str) and answer.strip():
            return _Css(None, f"{head}：{_clip(answer, ANSWER_CHARS)}")
        return _Css(None, head)
    if local == "navigate":
        return _Css(None, f"打开网址 {params.get('url', '')}".strip())
    if local == "go_back":
        return _Css(None, "后退到上一页")
    if local == "go_forward":
        return _Css(None, "前进到下一页")
    if local == "switch_tab":
        return _Css(None, "切换标签页")
    if local == "select_dropdown":
        chosen = None
        if isinstance(effect, SelectionEffect) and not isinstance(effect.selected, str):
            chosen = effect.selected.text
        chosen = chosen or params.get("text")
        option = _number(params.get("option_index"))
        if isinstance(chosen, str) and chosen:
            line = f"在下拉框中选择“{_clip(chosen, NAME_CHARS)}”"
        elif option is not None:
            line = f"在下拉框中选择第 {option + 1} 项"
        else:
            line = "在下拉框中选择"
        return _Css(None, line, pointer_after=None)
    if local == "extract_content":
        return _Css(None, "读取页面文字")
    if local == "screenshot":
        return _Css(None, "截图")
    shown = json.dumps(params, ensure_ascii=False) if params else ""
    return _Css(None, f"{local} {shown}".strip())


def _xy(point: tuple[int, int] | None) -> str:
    return f"({point[0]}, {point[1]})" if point is not None else "(?)"


def action_text(action: Action, result: ActionResult | None) -> str:
    """One Chinese line for a recorded action, with ``（没有成功）`` when it failed."""
    line = _css(action, result, None).text
    return line + ("（没有成功）" if result is not None and not result.success else "")


def action_lines(record: StepRecord) -> str:
    """A step's actions, one numbered Chinese line each (``1. 点击 (223, 234) → 按钮“最多播放”``)."""
    lines = []
    for index, action in enumerate(record.actions):
        result = record.action_results[index] if index < len(record.action_results) else None
        lines.append(f"{index + 1}. {action_text(action, result)}")
    return "\n".join(lines) or "（没有动作）"


def _place(point: tuple[int, int] | None, grid: PointGrid | None) -> tuple[int, int] | None:
    return grid.to_grid(*point) if point is not None and grid is not None else None


def step_marks(
    record: StepRecord,
    geometry: Geometry,
    *,
    pointer: tuple[int, int] | None = None,
    only: frozenset[int] | None = None,
) -> list[ActionMark]:
    """The marks of *record*'s actions on an image of *geometry*.

    *pointer* is where the pointer was before the first action (CSS px);
    *only* keeps just these action indices (still numbered by their place in
    the step).
    """
    marks: list[ActionMark] = []
    for index, action in enumerate(record.actions):
        result = record.action_results[index] if index < len(record.action_results) else None
        css = _css(action, result, pointer)
        failed = result is not None and not result.success
        if css.pointer_after is not ...:
            # A pointer action that failed before measuring anything may not
            # have moved the pointer at all.
            pointer = None if failed and not css.measured else css.pointer_after
        if only is not None and index not in only:
            continue
        point, end = _place(css.point, geometry.grid), _place(css.end, geometry.grid)
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
        record, geometry, pointer=_initial_pointer(trace, request.step), only=request.only
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


def observation_request(run: str, step: int) -> MarkRequest:
    """An observation's own actions: what its step decided on that screen."""
    return MarkRequest(run, step, f"第 {step} 步在这一屏上做的动作")


def default_requests(
    trace: RunTrace, run: str, step: int | None, archiver: str | None, name: str | None
) -> tuple[Geometry, list[MarkRequest]]:
    """An image's geometry and whose actions it shows when its queue names none.

    An observation shows its step's actions; a ``browser.frames`` before-action
    frame the action it was taken before; any other image none.
    """
    if archiver is None:
        assert step is not None, "an observation image names its step"
        return observation_geometry(trace, step), [observation_request(run, step)]
    if archiver != FRAMES_ARCHIVER_ID or name is None:
        return Geometry(
            None, None, None, "这张图不是截图或画面帧，不知道它的视口，动作只列出、不画出"
        ), []
    record = frame_record(trace, step, name)
    geometry = frame_geometry(trace, step, record)
    if record is None or step is None or record.kind != "before_action":
        return geometry, []
    if record.action_index is None:
        return geometry, []
    step_record = trace.step(step) if trace.config.recording.trajectory else None
    index = (
        executed_action(step_record, record.action_index, record.action)
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
    """The geometry of an observation (``archiver`` ``None``) or of a ``browser.frames`` image."""
    return default_requests(trace, "", step, archiver, name)[0]


__all__ = [
    "ActionMark",
    "Geometry",
    "ImageMarks",
    "MarkRequest",
    "MarkSet",
    "action_lines",
    "action_text",
    "default_requests",
    "element_text",
    "executed_action",
    "frame_geometry",
    "frame_record",
    "image_geometry",
    "mark_set",
    "marks_on",
    "observation_geometry",
    "observation_marks",
    "step_marks",
]
