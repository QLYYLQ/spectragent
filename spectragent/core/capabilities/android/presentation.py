"""How the Android capability's recorded actions read to a person, and where they touched.

Android's side of ``core/presentation.py`` (``docs/architecture_design.md``
§4.17 "动作标记"): each Android action class takes :class:`AndroidWords` and
names its verb; the UI tree fragment describes its elements
(:func:`element_description`). Points are the screen's physical pixels. A
touch is drawn where its effect says the finger went down, else where its
params put it, never at a point nothing recorded; a drag also where it
lifted; a scroll's arrow points the way its verb says, as the browser's. An
element an index named is drawn as its recorded bounds and named by its
class and text.
"""

from __future__ import annotations

from typing import ClassVar

from spectragent.core.capabilities.android.effects import (
    AndroidTextEffect,
    AndroidTouchEffect,
    ScreenPoint,
    Touch,
)
from spectragent.core.capabilities.android.ui_tree.models import UITree
from spectragent.core.presentation import (
    DIRECTIONS,
    NAME_CHARS,
    TYPED_CHARS,
    ActionPresentation,
    ElementDescription,
    Point,
    clip,
    element_param,
    param_points,
    seconds,
    xy,
)
from spectragent.schemas.action import Action, ActionEffect, ActionResult

#: Android view classes (the class name's last part) as a person names the
#: thing on screen; any other class is an element.
_ANDROID_ROLES = {
    "Button": "按钮",
    "ImageButton": "按钮",
    "MaterialButton": "按钮",
    "FloatingActionButton": "按钮",
    "ExtendedFloatingActionButton": "按钮",
    "TextView": "文字",
    "CheckedTextView": "文字",
    "EditText": "输入框",
    "AutoCompleteTextView": "输入框",
    "TextInputEditText": "输入框",
    "ImageView": "图片",
    "Switch": "开关",
    "SwitchCompat": "开关",
    "SwitchMaterial": "开关",
    "ToggleButton": "开关",
    "CheckBox": "复选框",
    "RadioButton": "单选项",
    "SeekBar": "滑块",
    "Spinner": "下拉框",
    "RecyclerView": "列表",
    "ListView": "列表",
    "ScrollView": "滚动区域",
    "HorizontalScrollView": "滚动区域",
    "ViewPager": "翻页区域",
    "TabWidget": "标签栏",
}
#: The Android system keys ``android.press_key`` presses, as a person names them.
_ANDROID_KEYS = {
    "back": "返回",
    "home": "主屏幕",
    "enter": "回车",
    "delete": "删除",
    "recent_apps": "最近任务",
}


def element_description(tree: UITree, index: int) -> ElementDescription:
    """Element *index* of a cleaned UI tree: its class as a role, its text, its bounds."""
    node = tree.node(index)
    if node is None:
        return ElementDescription(f"#{index}（界面树里没有这个元素）")
    role = _ANDROID_ROLES.get(node.class_name.rsplit(".", 1)[-1], "元素")
    name = node.text or node.content_description or node.hint_text
    text = f"#{index} {role}“{clip(name, NAME_CHARS)}”" if name else f"#{index} {role}"
    box = node.bounds_in_screen
    bounds = (box.left, box.top, box.right, box.bottom) if box.right > box.left else None
    return ElementDescription(text, bounds)


def _touch(effect: ActionEffect | None) -> Touch | None:
    """The touch an Android action's effect says it made, or ``None``."""
    if isinstance(effect, AndroidTouchEffect):
        return effect.touch
    if isinstance(effect, AndroidTextEffect):
        return effect.tap
    return None


def _screen(point: ScreenPoint) -> Point:
    return point.x, point.y


class AndroidWords:
    """An Android action read in plain words: its class names ``person_verb``, :func:`present` the rest."""

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
        return present(cls.person_verb, action, result, element)


def present(
    verb: str, action: Action, result: ActionResult | None, element: ElementDescription | None
) -> ActionPresentation:
    """An Android action in the screen's pixels: where the finger went, and its line.

    The point is where the effect says the finger went down, else the point
    the params give; never a point nothing recorded. An element the params
    name is drawn as its recorded bounds either way.
    """
    name, params = action.name, action.params
    effect = result.effect if result is not None else None
    touch = _touch(effect)
    points = param_points(action)
    first = points[0] if points else None
    named = element_param(action)
    target = element.text if element is not None else f"#{named}" if named is not None else None
    box = element.bounds if element is not None else None
    # Said for an action whose point nothing recorded: its element's box may still be drawn.
    no_point = "没有记录按下的位置" + ("；虚线框是它指名的元素" if box else "")
    if name in ("android.click", "android.click_coordinate", "android.long_press"):
        point = (_screen(touch.start) if touch else None) or first
        if point is not None:
            line = f"{verb} {xy(point)}" + (f" → {target}" if target else "")
        else:
            line = f"{verb} {target}" if target else verb
        if name == "android.long_press" and touch is not None:
            line += f"，按住 {seconds(touch.duration_ms / 1000)} 秒"
        return ActionPresentation(
            "click",
            line,
            point=point,
            box=box,
            measured=touch is not None,
            unplaced=None if point else no_point,
        )
    if name == "android.drag":
        start = _screen(touch.start) if touch else first
        end = _screen(touch.end) if touch else (points[-1] if len(points) > 1 else None)
        return ActionPresentation(
            "drag",
            f"{verb} {xy(start)} → {xy(end)}" if start and end else verb,
            point=start,
            end=end,
            measured=touch is not None,
            unplaced=None if start and end else "没有记录拖动的起点或终点",
        )
    if name == "android.scroll":
        direction = params.get("direction")
        word = DIRECTIONS.get(str(direction), "")
        line = f"向{word}{verb}" if word else verb
        if target:
            line += f" → {target}"
        elif first is not None:
            line = f"在 {xy(first)} {line}"
        start = _screen(touch.start) if touch else first
        if touch is not None:
            line += f"，手指从 {xy(start)} 滑到 {xy(_screen(touch.end))}"
        # The mark's arrow points the way the verb says, as on the browser; the
        # finger's opposite path is in the line.
        return ActionPresentation(
            "scroll",
            line,
            point=start,
            box=box,
            direction=direction if direction in DIRECTIONS else None,
            measured=touch is not None,
            unplaced=None
            if start
            else "没有记录滑动的位置" + ("；虚线框是它指名的元素" if box else ""),
        )
    if name in ("android.type", "android.type_coordinate"):
        text = params.get("text")
        sensitive = result is not None and result.typed_into_sensitive_field is True
        if not isinstance(text, str):
            line = verb
        elif sensitive:
            line = f"{verb}（敏感字段，内容不显示）"
        else:
            line = f"{verb}“{clip(text, TYPED_CHARS)}”"
        if target:
            line += f" → {target}"
        if isinstance(effect, AndroidTextEffect) and not effect.text_entered:
            line += "，没有输入文字"
        point = (_screen(touch.start) if touch else None) or first
        if point is None and box is None:
            # Typed into the field that had focus: no place to draw.
            return ActionPresentation(None, line)
        typed = clip(text, TYPED_CHARS) if isinstance(text, str) and not sensitive else None
        return ActionPresentation(
            "type",
            line,
            point=point,
            box=box,
            typed=typed,
            measured=touch is not None,
            unplaced=None if point else no_point,
        )
    if name == "android.press_key":
        key = params.get("key")
        return ActionPresentation(None, f"{verb} {_ANDROID_KEYS.get(str(key), key or '')}".strip())
    if name == "android.open_app":
        return ActionPresentation(None, f"{verb} {params.get('app_name', '')}".strip())
    if name == "android.wait":
        asked = params.get("seconds")
        if isinstance(asked, int | float) and not isinstance(asked, bool):
            return ActionPresentation(None, f"{verb} {seconds(float(asked))} 秒")
    return ActionPresentation(None, verb)
