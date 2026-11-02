"""The UI tree serializers: AndroidWorld's M3A and T3A element lists (``docs/architecture_design.md`` §11.2).

Ported from AndroidWorld (``google-research/android_world`` at commit
``55ddaf5edc6f6c25673aed343907d5b9dca66f84``, Apache-2.0; ``THIRD_PARTY_NOTICES.md``)
so that its agents, re-implemented here, read the elements as they read them
there:

- an element is first converted the way ``representation_utils.py``'s
  ``accessibility_node_to_ui_element`` converts a forest node into its
  ``UIElement`` (:func:`ui_element`): an empty string is ``None``; the bounds
  are ``bbox_pixels``; ``bbox`` (the normalized bounds) stays ``None``,
  because AndroidWorld's controller extracts its elements without the screen
  size; ``tooltip``, ``resource_id`` and ``metadata`` stay ``None``, because
  that conversion never fills them;
- ``m3a`` prints each as ``m3a.py``'s ``_generate_ui_element_description``
  does, ``t3a`` as ``t3a.py``'s ``_generate_ui_elements_description_list_full``
  does (``UI element <i>: `` and the dataclass's own ``repr``); both write one
  line per element, each ending in a newline, and put strings in unescaped, as
  AndroidWorld does.

One difference comes from the tree, not the format: our service fills
``is_focused`` (``device/forest.py``), which AndroidWorld's accessibility
forwarder leaves false, so T3A's line can say ``is_focused=True`` where
AndroidWorld's never does.
"""

from __future__ import annotations

import dataclasses

from spectragent.core.capabilities.android.device.forest import AndroidAccessibilityNodeInfo
from spectragent.core.capabilities.android.ui_tree._registrar import AndroidUITreeSerializerBase
from spectragent.core.capabilities.android.ui_tree.models import UITree

#: What both formats say about the numbers; true of every cleaner.
_INDEX = (
    "The number after `UI element` is the element's index: an action names the element by it "
    "(its `index` param). Indexes increase down the list and may skip numbers."
)


@dataclasses.dataclass(frozen=True)
class BoundingBox:
    """AndroidWorld's ``BoundingBox``: the same fields in the same order, so the same ``repr``."""

    x_min: int
    x_max: int
    y_min: int
    y_max: int


@dataclasses.dataclass(frozen=True)
class UIElement:
    """AndroidWorld's ``UIElement``: the same fields in the same order, so the same ``repr``."""

    text: str | None = None
    content_description: str | None = None
    class_name: str | None = None
    bbox: BoundingBox | None = None
    bbox_pixels: BoundingBox | None = None
    hint_text: str | None = None
    is_checked: bool | None = None
    is_checkable: bool | None = None
    is_clickable: bool | None = None
    is_editable: bool | None = None
    is_enabled: bool | None = None
    is_focused: bool | None = None
    is_focusable: bool | None = None
    is_long_clickable: bool | None = None
    is_scrollable: bool | None = None
    is_selected: bool | None = None
    is_visible: bool | None = None
    package_name: str | None = None
    resource_name: str | None = None
    tooltip: str | None = None
    resource_id: str | None = None
    metadata: None = None


def ui_element(node: AndroidAccessibilityNodeInfo) -> UIElement:
    """*node* as AndroidWorld's ``accessibility_node_to_ui_element`` converts it, without a screen size."""
    box = node.bounds_in_screen
    return UIElement(
        text=node.text or None,
        content_description=node.content_description or None,
        class_name=node.class_name or None,
        bbox_pixels=BoundingBox(x_min=box.left, x_max=box.right, y_min=box.top, y_max=box.bottom),
        hint_text=node.hint_text or None,
        is_checked=node.is_checked,
        is_checkable=node.is_checkable,
        is_clickable=node.is_clickable,
        is_editable=node.is_editable,
        is_enabled=node.is_enabled,
        is_focused=node.is_focused,
        is_focusable=node.is_focusable,
        is_long_clickable=node.is_long_clickable,
        is_scrollable=node.is_scrollable,
        is_selected=node.is_selected,
        is_visible=node.is_visible_to_user,
        package_name=node.package_name or None,
        resource_name=node.view_id_resource_name or None,
    )


def _flag(value: bool | None) -> str:
    return "True" if value else "False"


class M3ASerializer(AndroidUITreeSerializerBase):
    """M3A's element list: ``UI element 3: {"index": 3, "text": "Wi-Fi", "is_clickable": True, ...}``."""

    __registry_key__ = "m3a"

    def serialize(self, tree: UITree) -> str:
        return "".join(f"{_m3a(ui_element(node), index)}\n" for index, node in tree.element_nodes())

    def describe_format(self) -> str:
        return (
            "The screen's UI elements are listed one per line, as "
            '`UI element 3: {"index": 3, "text": "Wi-Fi", "is_clickable": True, '
            '"is_long_clickable": False, "is_editable": False, "is_selected": False, '
            '"is_checked": False}`. "text", "content_description" and "hint_text" appear only '
            'when the element has them; "is_clickable", "is_long_clickable", "is_editable", '
            '"is_selected" and "is_checked" always, True or False; "is_scrollable" and '
            '"is_focusable" only when True. The line gives no position. ' + _INDEX
        )


def _m3a(element: UIElement, index: int) -> str:
    """``m3a.py``'s ``_generate_ui_element_description``, line for line.

    Its ``"tooltip"`` key never appears: :func:`ui_element` never fills
    ``tooltip``, as AndroidWorld's conversion never does.
    """
    line = f'UI element {index}: {{"index": {index}, '
    if element.text:
        line += f'"text": "{element.text}", '
    if element.content_description:
        line += f'"content_description": "{element.content_description}", '
    if element.hint_text:
        line += f'"hint_text": "{element.hint_text}", '
    line += f'"is_clickable": {_flag(element.is_clickable)}, '
    line += f'"is_long_clickable": {_flag(element.is_long_clickable)}, '
    line += f'"is_editable": {_flag(element.is_editable)}, '
    if element.is_scrollable:
        line += '"is_scrollable": True, '
    if element.is_focusable:
        line += '"is_focusable": True, '
    line += f'"is_selected": {_flag(element.is_selected)}, '
    line += f'"is_checked": {_flag(element.is_checked)}, '
    return line[:-2] + "}"


class T3ASerializer(AndroidUITreeSerializerBase):
    """T3A's element list: ``UI element 3: UIElement(text='Wi-Fi', ..., bbox_pixels=BoundingBox(...), ...)``."""

    __registry_key__ = "t3a"

    def serialize(self, tree: UITree) -> str:
        return "".join(
            f"UI element {index}: {ui_element(node)!r}\n" for index, node in tree.element_nodes()
        )

    def describe_format(self) -> str:
        return (
            "The screen's UI elements are listed one per line, as `UI element 3: "
            "UIElement(text='Wi-Fi', content_description=None, "
            "class_name='android.widget.TextView', bbox=None, bbox_pixels=BoundingBox(x_min=63, "
            "x_max=1017, y_min=310, y_max=420), hint_text=None, is_checked=False, ...)`: every "
            "field of the element in a fixed order, None where it has none. bbox_pixels is "
            "where the element is on the screen, in screen pixels from its top-left corner; "
            "bbox, tooltip, resource_id and metadata are always None. " + _INDEX
        )
