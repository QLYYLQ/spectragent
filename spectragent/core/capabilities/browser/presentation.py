"""How the browser's recorded actions read to a person, and where they pointed.

The browser's side of ``core/presentation.py`` (``docs/architecture_design.md``
§4.17 "动作标记"): each browser action class takes :class:`BrowserWords`,
names its verb, and so reads in these words in the viewer, the lookahead
queues and anywhere else a recorded action is shown to a person. Points are
CSS viewport pixels, the browser's units; the framework places them on an
image. The point a mark uses is the one the action's effect measured (where
the input was actually dispatched), else the one its params named. A page
scroll without a point turns where the pointer was.
"""

from __future__ import annotations

from typing import ClassVar

from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    PointerEffect,
    ScrollEffect,
    SelectionEffect,
    TextInputEffect,
    ViewportPoint,
    WaitEffect,
)
from spectragent.core.presentation import (
    DIRECTIONS,
    NAME_CHARS,
    TYPED_CHARS,
    UNMOVED,
    ActionPresentation,
    ElementDescription,
    Point,
    clip,
    element_param,
    number,
    param_points,
    seconds,
    unworded,
    xy,
)
from spectragent.schemas.action import Action, ActionEffect, ActionResult

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


def element_text(fact: object) -> str | None:
    """An effect's element as a person names it (``按钮“最多播放”``), or ``None`` when unknown."""
    if isinstance(fact, ElementFacts):
        role = _ROLES.get(fact.role, fact.role)
        return f"{role}“{clip(fact.name, NAME_CHARS)}”" if fact.name else role
    if fact == "nothing":
        return "空白处"
    return None


def _point(fact: object) -> Point | None:
    return (fact.x, fact.y) if isinstance(fact, ViewportPoint) else None


def _typed(
    params: dict[str, object], result: ActionResult | None, effect: ActionEffect | None
) -> str | None:
    """The typed text as a person may read it; withheld for a sensitive field."""
    text = params.get("text")
    if not isinstance(text, str):
        return None
    sensitive = (result is not None and result.typed_into_sensitive_field) or (
        isinstance(effect, TextInputEffect) and effect.sensitive is True
    )
    return None if sensitive else text


class BrowserWords:
    """A browser action read in plain words: its class names ``person_verb``, :func:`present` the rest."""

    person_verb: ClassVar[str]

    @classmethod
    def present(
        cls,
        action: Action,
        result: ActionResult | None,
        *,
        element: ElementDescription | None,
        pointer: Point | None,
    ) -> ActionPresentation:
        return present(cls.person_verb, action, result, pointer)


def present(
    verb: str, action: Action, result: ActionResult | None, pointer: Point | None
) -> ActionPresentation:
    """A browser action's line and mark, in CSS px; *pointer* is where the pointer was before it."""
    name = action.name
    params = action.params
    effect = result.effect if result is not None else None
    points = param_points(action)
    first = points[0] if points else None
    element = element_param(action)
    if name in ("browser.click_coordinate", "browser.click", "browser.drag"):
        measured = _point(effect.point) if isinstance(effect, PointerEffect) else None
        hit = element_text(effect.hit) if isinstance(effect, PointerEffect) else None
        if name == "browser.drag":
            start = measured or first
            release = (
                _point(effect.release_point)
                if isinstance(effect, PointerEffect) and effect.release_point is not None
                else None
            )
            end = release or (points[-1] if len(points) > 1 else None)
            line = (f"{verb} {xy(start)} → {xy(end)}" if start and end else verb) + (
                f"，按在{hit}上" if hit else ""
            )
            return ActionPresentation(
                "drag",
                line,
                point=start,
                end=end,
                measured=measured is not None,
                unplaced=None if start and end else "没有记录拖动的起点或终点",
                pointer_after=end,
            )
        point = measured or first
        if point is not None:
            line = f"{verb} {xy(point)}" + (f" → {hit}" if hit else "")
        else:
            line = f"{verb}元素 #{element}" if element is not None else verb
            line += f" → {hit}" if hit else ""
        return ActionPresentation(
            "click",
            line,
            point=point,
            measured=measured is not None,
            unplaced=None if point else "没有记录点击的位置",
            pointer_after=point,
        )
    if name in ("browser.type_coordinate", "browser.type"):
        typed = _typed(params, result, effect)
        target = element_text(effect.target) if isinstance(effect, TextInputEffect) else None
        if not isinstance(params.get("text"), str):
            said = verb
        elif typed is None:
            said = f"{verb}（敏感字段，内容不显示）"
        else:
            said = f"{verb}“{clip(typed, TYPED_CHARS)}”"
        if target:
            said += f" → {target}"
        elif element is not None:
            said += f" → 元素 #{element}"
        if name == "browser.type":
            return ActionPresentation(None, said, pointer_after=None)
        if first is None:
            # Typed into the focus as it was: no place to draw, and the pointer did not move.
            return ActionPresentation(None, said)
        return ActionPresentation(
            "type",
            said,
            point=first,
            typed=clip(typed, TYPED_CHARS) if typed is not None else None,
            pointer_after=first,
        )
    if name == "browser.scroll":
        direction = params.get("direction")
        if isinstance(effect, ScrollEffect):
            direction = effect.direction
        word = DIRECTIONS.get(str(direction), "")
        line = f"向{word}{verb}" if word else verb
        if first is not None:
            line = f"在 {xy(first)} {line}"
        amount = number(params.get("amount"))
        delta = effect.delta if isinstance(effect, ScrollEffect) else None
        if delta is not None and isinstance(effect, ScrollEffect):
            moved = abs(delta.y if effect.direction in ("up", "down") else delta.x)
            what = "页面" if effect.container == "page" else "区域"
            line += f"，{what}移动了 {moved} 像素" if moved else "，没有移动"
        elif amount is not None:
            line += f" {amount} 像素"
        if isinstance(effect, ScrollEffect) and effect.at_boundary is True:
            line += f"，已到{_EDGES.get(effect.direction, '边缘')}"
        shown = direction if direction in DIRECTIONS else None
        if first is not None:
            # The pointer moved to the start and the wheel turned there.
            return ActionPresentation(
                "scroll", line, point=first, direction=shown, pointer_after=first
            )
        by_element = element is not None
        point = None if by_element else pointer
        unplaced = (
            "滚动的是页面中的某个元素，没有记录滚轮的位置"
            if by_element
            else None
            if point
            else "滚动时鼠标的位置没有记录"
        )
        return ActionPresentation(
            "scroll",
            line,
            point=point,
            direction=shown,
            unplaced=unplaced,
            pointer_after=None if by_element else UNMOVED,
        )
    if name == "browser.send_keys":
        return ActionPresentation(None, f"{verb} {params.get('keys', '')}".strip())
    if name == "browser.wait":
        if isinstance(effect, WaitEffect):
            return ActionPresentation(None, f"{verb} {seconds(effect.waited_ms / 1000)} 秒")
        asked = params.get("seconds")
        if isinstance(asked, int | float) and not isinstance(asked, bool):
            return ActionPresentation(None, f"{verb} {seconds(float(asked))} 秒")
        return ActionPresentation(None, verb)
    if name == "browser.navigate":
        return ActionPresentation(None, f"{verb} {params.get('url', '')}".strip())
    if name == "browser.go_back":
        return ActionPresentation(None, f"{verb}到上一页")
    if name == "browser.go_forward":
        return ActionPresentation(None, f"{verb}到下一页")
    if name == "browser.switch_tab":
        return ActionPresentation(None, verb)
    if name == "browser.select_dropdown":
        chosen = None
        if isinstance(effect, SelectionEffect) and not isinstance(effect.selected, str):
            chosen = effect.selected.text
        chosen = chosen or params.get("text")
        option = number(params.get("option_index"))
        if isinstance(chosen, str) and chosen:
            line = f"{verb}“{clip(chosen, NAME_CHARS)}”"
        elif option is not None:
            line = f"{verb}第 {option + 1} 项"
        else:
            line = verb
        return ActionPresentation(None, line, pointer_after=None)
    if name in ("browser.extract_content", "browser.screenshot"):
        return ActionPresentation(None, verb)
    return unworded(action)
