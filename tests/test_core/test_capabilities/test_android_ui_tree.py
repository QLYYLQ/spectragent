"""The UI tree pipeline: AndroidWorld's cleaner and the M3A and T3A serializers, on recorded forests.

The recorded forests (``data/android_ui_tree/*.forest.json.gz``) are our
service's full captures on AndroidWorld's AVD (API 33 Google APIs, Pixel 6,
2026-11-02), with AndroidWorld's own accessibility forwarder running beside
it. At the same still moment the forwarder sent its own forest, a service's
without ``FLAG_INCLUDE_NOT_IMPORTANT_VIEWS``, and AndroidWorld's own code
(commit ``55ddaf5``: ``forest_to_ui_elements``, then M3A's and T3A's element
lists) wrote ``*.androidworld.m3a.txt`` and ``*.androidworld.t3a.txt``. So the
first tests hold our whole pipeline, our tree to our text, against what
AndroidWorld's agents read on the same screen: Settings' main page, its list
of all apps, a date picker whose days are a provider's virtual nodes, the
Clock's stopwatch, and the Add network dialog with the keyboard up. One
difference is the tree's, not the format's: our service fills ``is_focused``,
which the forwarder leaves false.

The rules then each have a test on a small forest of their own, which fails
without the rule.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from spectragent.core.capabilities.android.device.clients import TreeCapture
from spectragent.core.capabilities.android.device.forest import (
    AndroidAccessibilityForest,
    AndroidAccessibilityNodeInfo,
)
from spectragent.core.capabilities.android.ui_tree.cleaning import AndroidWorldCleaner
from spectragent.core.capabilities.android.ui_tree.models import UITree, UITreeElement
from spectragent.core.capabilities.android.ui_tree.serializers import M3ASerializer, T3ASerializer

DATA = Path(__file__).parent / "data" / "android_ui_tree"
SCREEN = (1080, 2400)
SCREENS = (
    "settings_home",
    "settings_all_apps",
    "date_picker",
    "stopwatch_stopped",
    "add_network_dialog",
)


def _recorded(screen: str) -> TreeCapture:
    with gzip.open(DATA / f"{screen}.forest.json.gz") as recorded:
        return TreeCapture.model_validate(json.loads(recorded.read()))


def _cleaned(screen: str) -> UITree:
    return AndroidWorldCleaner().clean(_recorded(screen).forest, SCREEN)


def _labels(tree: UITree) -> list[str]:
    return [node.text or node.content_description for _, node in tree.element_nodes()]


# ── our pipeline against AndroidWorld's, screen by screen ─────────────


@pytest.mark.parametrize("screen", SCREENS)
def test_m3a_prints_what_androidworlds_m3a_reads_on_the_same_screen(screen: str) -> None:
    expected = (DATA / f"{screen}.androidworld.m3a.txt").read_text()

    assert M3ASerializer().serialize(_cleaned(screen)) == expected


@pytest.mark.parametrize("screen", SCREENS)
def test_t3a_prints_what_androidworlds_t3a_reads_but_where_focus_is(screen: str) -> None:
    expected = (DATA / f"{screen}.androidworld.t3a.txt").read_text()
    printed = T3ASerializer().serialize(_cleaned(screen))

    if screen == "add_network_dialog":  # the SSID field has input focus
        focused = [line for line in printed.splitlines() if "is_focused=True" in line]
        assert len(focused) == 1 and "resource_name='com.android.settings:id/ssid'" in focused[0]
        printed = printed.replace("is_focused=True", "is_focused=False")
    assert printed == expected


# ── which nodes AndroidWorld's forwarder reads ────────────────────────


def _node(unique_id: int, *children: int, **fields: JsonValue) -> dict[str, JsonValue]:
    """A laid-out, important, visible node on the screen, unless *fields* say otherwise."""
    node: dict[str, JsonValue] = {
        "unique_id": unique_id,
        "bounds_in_screen": {
            "left": 0,
            "top": 100 * unique_id,
            "right": 1080,
            "bottom": 100 * unique_id + 90,
        },
        "class_name": "android.view.View",
        "window_id": 5,
        "is_visible_to_user": True,
        "child_ids": list(children),
        "drawing_order": 1,
        "is_important": True,
    }
    node.update(fields)
    return node


def _forest(*nodes: dict[str, JsonValue]) -> AndroidAccessibilityForest:
    return AndroidAccessibilityForest.model_validate(
        {"windows": [{"id": 5, "window_type": "TYPE_APPLICATION", "tree": {"nodes": list(nodes)}}]}
    )


def _elements(*nodes: dict[str, JsonValue]) -> list[tuple[int, str]]:
    tree = AndroidWorldCleaner().clean(_forest(*nodes), SCREEN)
    return [(index, node.text or node.content_description) for index, node in tree.element_nodes()]


def test_a_view_not_important_for_accessibility_is_left_out_and_its_children_take_its_place() -> (
    None
):
    """Gboard's keys are important and described; each holds two text views marked not important,
    the hint digit among them. The forwarder reads the keys as leaves, without the text views."""
    assert _elements(
        _node(0, 1, drawing_order=0),
        _node(1, 2, content_description="q", is_clickable=True),
        _node(2, text="1", is_important=False),
    ) == [(0, "q")]
    keyboard = _cleaned("add_network_dialog")
    keys = {
        node.content_description
        for window in keyboard.forest.windows
        if window.window_type == "TYPE_INPUT_METHOD"
        for node in window.tree.nodes
    }
    assert {"q", "w", "Delete", "Space"} <= keys
    assert not {"1", "2", "0"} & set(_labels(keyboard)), "the keys' hint digits are no elements"


def test_a_leaf_under_a_left_out_view_is_a_leaf_of_its_nearest_read_ancestor() -> None:
    """A button whose only children are not important is a leaf where the forwarder reads it."""
    assert _elements(
        _node(0, 1, drawing_order=0),
        _node(1, 2, 3, text="OK", is_clickable=True),
        _node(2, is_important=False),
        _node(3, 4, is_important=False),
        _node(4, is_important=False, class_name="android.widget.ImageView"),
    ) == [(0, "OK")]


def test_a_node_a_provider_built_is_read_whatever_importance_it_reports() -> None:
    """A provider's nodes report no importance and no drawing order (the date picker's 92 days, a
    WebView's 5,111 nodes, measured); the forwarder reads them, the WebView's own node included."""
    assert _elements(
        _node(0, 1, drawing_order=0),
        _node(1, 2, is_important=False, class_name="android.webkit.WebView", drawing_order=0),
        _node(2, 3, 4, is_important=False, drawing_order=0, is_scrollable=True),
        _node(3, text="Libraries", is_important=False, drawing_order=0),
        _node(4, text="Files", is_important=False, drawing_order=0),
    ) == [(0, ""), (1, "Libraries"), (2, "Files")]
    days = [
        node.text
        for _, node in _cleaned("date_picker").element_nodes()
        if not node.is_important and node.drawing_order == 0
    ]
    # AndroidWorld sets the device to October 2023; the other months' days are hidden.
    assert days == [str(day) for day in range(1, 32)]


def test_elements_are_numbered_in_the_order_the_forwarder_reads_them() -> None:
    """Breadth first over the tree the forwarder reads: a node that takes a left-out view's place
    sits a level higher there, ahead of a sibling the full tree's walk would have put first."""
    assert _elements(
        _node(0, 1, 2, drawing_order=0),
        _node(1, 3, is_important=False),
        _node(2, text="second in the full tree"),
        _node(3, text="taking its parent's place"),
    ) == [(0, "taking its parent's place"), (1, "second in the full tree")]


def test_a_container_is_an_element_only_with_a_description_or_when_it_scrolls() -> None:
    assert _elements(
        _node(0, 1, 2, 3, drawing_order=0),
        _node(1, 4, content_description="Battery charging"),
        _node(2, 5, is_scrollable=True),
        _node(3, 6),
        _node(4, text="a"),
        _node(5, text="b"),
        _node(6, text="c"),
    ) == [(0, "Battery charging"), (1, ""), (2, "a"), (3, "b"), (4, "c")]


def test_an_invisible_node_takes_no_number_and_one_off_the_screen_leaves_its_number_unused() -> (
    None
):
    """As AndroidWorld: invisible nodes are skipped as it extracts; an element whose bounds are
    empty or off the screen is skipped as M3A and T3A print, its number counted."""
    off_screen: JsonValue = {"left": 0, "top": 2500, "right": 1080, "bottom": 2600}
    empty: JsonValue = {"left": 540, "top": 300, "right": 540, "bottom": 390}
    assert _elements(
        _node(0, 1, 2, 3, 4, 5, drawing_order=0),
        _node(1, text="hidden", is_visible_to_user=False),
        _node(2, text="first"),
        _node(3, text="below the screen", bounds_in_screen=off_screen),
        _node(4, text="no size", bounds_in_screen=empty),
        _node(5, text="last"),
    ) == [(0, "first"), (3, "last")]


def test_the_cleaned_tree_keeps_every_window_and_only_the_elements_nodes() -> None:
    capture = _recorded("settings_home")
    tree = AndroidWorldCleaner().clean(capture.forest, SCREEN)

    assert [window.id for window in tree.forest.windows] == [
        window.id for window in capture.forest.windows
    ]
    kept = {
        (window.id, node.unique_id) for window in tree.forest.windows for node in window.tree.nodes
    }
    assert kept == {(element.window_id, element.unique_id) for element in tree.elements}
    original = {
        (window.id, node.unique_id): node
        for window in capture.forest.windows
        for node in window.tree.nodes
    }
    assert all(
        node == original[window.id, node.unique_id]
        for window in tree.forest.windows
        for node in window.tree.nodes
    ), "a kept node's fields stay as captured"


# ── the cleaned tree ──────────────────────────────────────────────────


def test_a_cleaned_tree_refuses_an_element_naming_a_node_it_does_not_hold() -> None:
    forest = _forest(_node(0, drawing_order=0))
    with pytest.raises(ValidationError, match="names node 9 of window 5"):
        UITree(forest=forest, elements=(UITreeElement(index=0, window_id=5, unique_id=9),))
    with pytest.raises(ValidationError, match="increasing index order"):
        UITree(
            forest=forest,
            elements=(
                UITreeElement(index=1, window_id=5, unique_id=0),
                UITreeElement(index=1, window_id=5, unique_id=0),
            ),
        )


def test_a_cleaned_tree_finds_an_elements_node_by_its_index() -> None:
    tree = _cleaned("stopwatch_stopped")
    start = tree.node(2)

    assert start is not None and start.content_description == "Start"
    assert tree.node(99) is None


# ── the two formats ───────────────────────────────────────────────────


def _one_element(**fields: JsonValue) -> UITree:
    node = AndroidAccessibilityNodeInfo.model_validate(
        {"unique_id": 0, "window_id": 5, "is_visible_to_user": True, **fields}
    )
    forest = AndroidAccessibilityForest.model_validate(
        {"windows": [{"id": 5, "tree": {"nodes": [node.model_dump(mode="json")]}}]}
    )
    return UITree(forest=forest, elements=(UITreeElement(index=7, window_id=5, unique_id=0),))


def test_m3a_writes_the_optional_keys_only_when_set_and_its_strings_as_they_are() -> None:
    plain = _one_element(text="Wi-Fi")
    full = _one_element(
        text='Say "hi"',
        content_description="Greeting",
        hint_text="Type here",
        tooltip_text="never printed: AndroidWorld's conversion drops it",
        is_clickable=True,
        is_long_clickable=True,
        is_editable=True,
        is_scrollable=True,
        is_focusable=True,
        is_selected=True,
        is_checked=True,
    )

    assert M3ASerializer().serialize(plain) == (
        'UI element 7: {"index": 7, "text": "Wi-Fi", "is_clickable": False, '
        '"is_long_clickable": False, "is_editable": False, "is_selected": False, '
        '"is_checked": False}\n'
    )
    assert M3ASerializer().serialize(full) == (
        'UI element 7: {"index": 7, "text": "Say "hi"", "content_description": "Greeting", '
        '"hint_text": "Type here", "is_clickable": True, "is_long_clickable": True, '
        '"is_editable": True, "is_scrollable": True, "is_focusable": True, '
        '"is_selected": True, "is_checked": True}\n'
    )


def test_t3a_writes_androidworlds_uielement_repr() -> None:
    tree = _one_element(
        text="It's",
        class_name="android.widget.TextView",
        package_name="com.android.settings",
        view_id_resource_name="android:id/title",
        bounds_in_screen={"left": 10, "top": 20, "right": 30, "bottom": 40},
        is_enabled=True,
    )

    assert T3ASerializer().serialize(tree) == (
        'UI element 7: UIElement(text="It\'s", content_description=None, '
        "class_name='android.widget.TextView', bbox=None, bbox_pixels=BoundingBox(x_min=10, "
        "x_max=30, y_min=20, y_max=40), hint_text=None, is_checked=False, is_checkable=False, "
        "is_clickable=False, is_editable=False, is_enabled=True, is_focused=False, "
        "is_focusable=False, is_long_clickable=False, is_scrollable=False, is_selected=False, "
        "is_visible=True, package_name='com.android.settings', resource_name='android:id/title', "
        "tooltip=None, resource_id=None, metadata=None)\n"
    )


@pytest.mark.parametrize(
    ("serializer", "shown"),
    [(M3ASerializer(), '"index": 3'), (T3ASerializer(), "UIElement(text='Wi-Fi'")],
)
def test_each_format_describes_its_own_line_and_how_an_action_names_an_element(
    serializer: M3ASerializer | T3ASerializer, shown: str
) -> None:
    description = serializer.describe_format()

    assert "UI element 3:" in description and shown in description
    assert "`index` param" in description and "may skip numbers" in description
    assert ("bbox_pixels" in description) == isinstance(serializer, T3ASerializer)
