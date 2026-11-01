"""The UI tree our accessibility service captures, in android_env's ``AndroidAccessibilityForest`` form.

``docs/architecture_design.md`` §11.2 ("片段"): the tree's wire format is
android_env's ``AndroidAccessibilityForest`` (``android_env/proto/a11y/``,
android_env 1.2.3, Apache-2.0), which the AndroidControl dataset stores too,
plus one field of ours on every node, ``is_important``. Each model here is one
proto message, with the proto's name and its fields, in its order. One field
reads otherwise in Python only: a node's ``actions`` is the attribute
``accessibility_actions``, because ``actions`` is the capability's own action
receptor, and no model of the capability's package may carry a receptor's name
(``tests/test_core/test_capabilities/test_receptor_hygiene.py``); on the wire,
and to every reader by alias, it is ``actions``.

The service sends the proto3 JSON form: the proto's field names, enums by
name, and every field at its default left out, so a field missing here has the
proto's default. ``google.protobuf.json_format.ParseDict(forest,
AndroidAccessibilityForest(), ignore_unknown_fields=True)`` turns one into the
proto itself, dropping ``is_important``.

The layout is AndroidWorld's accessibility forwarder's: displays in order,
each display's windows bottom to top, each window's nodes breadth first with
ids from 0 at the root, and each node's depth from 0. The tree is complete:
nodes that are not important for accessibility come with it, and view ids.
Nodes off the screen are kept up to a capture's ``max_nodes``; past it, a node
that is not visible to the user is left out with everything under it, and the
capture counts it. ``labeled_by_id`` and ``label_for_id`` are 0 for "none", as
in the proto, where 0 is also the root's id.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProtoRect(_Message):
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0


class AndroidAccessibilityAction(_Message):
    """An action a node offers: a standard ``AccessibilityAction`` id, or a custom one with its label."""

    id: int = 0
    label: str = ""


class AndroidAccessibilityNodeInfoClickableSpan(_Message):
    """A clickable span in a node's text or content description."""

    text: str = ""
    url: str = ""
    source: Literal["UNKNOWN_TYPE", "TEXT", "CONTENT_DESCRIPTION"] = "UNKNOWN_TYPE"
    #: Where the span starts in its source text.
    start: int = 0
    #: The ``unique_id`` of the node it belongs to.
    node_id: int = 0


class AndroidAccessibilityNodeInfo(_Message):
    """One node, with :attr:`is_important` added after the proto's fields."""

    unique_id: int = 0
    bounds_in_screen: ProtoRect = ProtoRect()
    class_name: str = ""
    content_description: str = ""
    hint_text: str = ""
    package_name: str = ""
    text: str = ""
    text_selection_start: int = 0
    text_selection_end: int = 0
    view_id_resource_name: str = ""
    window_id: int = 0
    is_checkable: bool = False
    is_checked: bool = False
    is_clickable: bool = False
    is_editable: bool = False
    is_enabled: bool = False
    is_focusable: bool = False
    is_focused: bool = False
    is_long_clickable: bool = False
    is_password: bool = False
    is_scrollable: bool = False
    is_selected: bool = False
    is_visible_to_user: bool = False
    #: The proto's ``actions`` (module docstring).
    accessibility_actions: tuple[AndroidAccessibilityAction, ...] = Field((), alias="actions")
    child_ids: tuple[int, ...] = ()
    clickable_spans: tuple[AndroidAccessibilityNodeInfoClickableSpan, ...] = ()
    depth: int = 0
    labeled_by_id: int = 0
    label_for_id: int = 0
    drawing_order: int = 0
    tooltip_text: str = ""
    #: Not in the proto: whether the view is important for accessibility. The
    #: service captures the views that are not as well; whether to drop them is
    #: a cleaner's choice (§11.2).
    is_important: bool = False


class AndroidAccessibilityTree(_Message):
    """One window's nodes; the root is the node whose ``unique_id`` is 0."""

    nodes: tuple[AndroidAccessibilityNodeInfo, ...] = ()


WindowType = Literal[
    "UNKNOWN_TYPE",
    "TYPE_APPLICATION",
    "TYPE_INPUT_METHOD",
    "TYPE_SYSTEM",
    "TYPE_ACCESSIBILITY_OVERLAY",
    "TYPE_SPLIT_SCREEN_DIVIDER",
]


class AndroidAccessibilityWindowInfo(_Message):
    bounds_in_screen: ProtoRect = ProtoRect()
    display_id: int = 0
    id: int = 0
    #: Its z-order: a window with a greater layer is in front.
    layer: int = 0
    title: str = ""
    window_type: WindowType = "UNKNOWN_TYPE"
    is_accessibility_focused: bool = False
    is_active: bool = False
    is_focused: bool = False
    is_in_picture_in_picture_mode: bool = False
    tree: AndroidAccessibilityTree = AndroidAccessibilityTree()


class AndroidAccessibilityForest(_Message):
    """Every window on every display."""

    windows: tuple[AndroidAccessibilityWindowInfo, ...] = ()
