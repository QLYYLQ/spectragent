"""The UI tree cleaners: AndroidWorld's element extraction (``docs/architecture_design.md`` §11.2).

Ported from AndroidWorld (``google-research/android_world`` at commit
``55ddaf5edc6f6c25673aed343907d5b9dca66f84``, Apache-2.0; ``THIRD_PARTY_NOTICES.md``):
``representation_utils.py``'s ``forest_to_ui_elements`` with
``exclude_invisible_elements=True``, as its controller calls it, and
``m3a_utils.py``'s ``validate_ui_element``, which M3A and T3A both apply before
they print an element.

AndroidWorld cleans the tree its accessibility forwarder reads, a service
without ``FLAG_INCLUDE_NOT_IMPORTANT_VIEWS``; ours reads the full tree
(``device/forest.py``). So the cleaner first rebuilds the tree that service
reads (:func:`forwarder_tree`), and applies AndroidWorld's rules to it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from spectragent.core.capabilities.android.device.forest import (
    AndroidAccessibilityForest,
    AndroidAccessibilityNodeInfo,
    AndroidAccessibilityTree,
    ProtoRect,
)
from spectragent.core.capabilities.android.ui_tree._registrar import (
    AndroidUITreeCleanerBase,
    ScreenSize,
)
from spectragent.core.capabilities.android.ui_tree.models import UITree, UITreeElement


def forwarder_tree(
    nodes: Sequence[AndroidAccessibilityNodeInfo],
) -> Iterator[tuple[AndroidAccessibilityNodeInfo, bool]]:
    """One window's nodes as a service without ``FLAG_INCLUDE_NOT_IMPORTANT_VIEWS`` reads them: each, and whether it is a leaf there.

    That service reads the window's root, every view important for
    accessibility, and every node an accessibility node provider builds (the
    content of a WebView, the days of a date picker), whatever importance the
    provider reports; a view that is not important is left out, and its
    children take its place under its parent (``ViewGroup.addChildrenForAccessibility``).
    Measured on the API 33 emulator (2026-11-02): every one of a WebView's
    5,111 nodes and of a date picker's 92 day cells reported ``is_important``
    false. What tells a provider's node from a view is its drawing order: the
    platform gives every laid-out view a drawing order of 1 or more
    (``View.populateAccessibilityNodeInfoDrawingOrderInParent``) and a provider
    leaves it 0, as on every node measured; a view that is not laid out has 0
    too, and is read as a provider's.

    The order is that service's walk, which AndroidWorld's forwarder makes:
    breadth first from the root, children in order. A node that takes a left-out
    view's place sits one level higher there, so it can come earlier than in
    the full tree's walk.
    """
    by_id = {node.unique_id: node for node in nodes}
    root = by_id.get(0)
    if root is None:
        return

    def read(node: AndroidAccessibilityNodeInfo) -> bool:
        return node.unique_id == 0 or node.is_important or node.drawing_order == 0

    def children(node: AndroidAccessibilityNodeInfo) -> list[AndroidAccessibilityNodeInfo]:
        found: list[AndroidAccessibilityNodeInfo] = []
        pending = [by_id[child] for child in reversed(node.child_ids) if child in by_id]
        while pending:
            child = pending.pop()
            if read(child):
                found.append(child)
            else:
                pending.extend(
                    by_id[grandchild]
                    for grandchild in reversed(child.child_ids)
                    if grandchild in by_id
                )
        return found

    seen = {root.unique_id}
    layer = [root]
    while layer:
        below: list[AndroidAccessibilityNodeInfo] = []
        for node in layer:
            found = [child for child in children(node) if child.unique_id not in seen]
            seen.update(child.unique_id for child in found)
            below.extend(found)
            yield node, not found
        layer = below


def on_screen(box: ProtoRect, screen: ScreenSize) -> bool:
    """``validate_ui_element``'s bounds test: non-empty, and not wholly off a screen of *screen* pixels."""
    width, height = screen
    return not (
        box.left >= box.right
        or box.left >= width
        or box.right <= 0
        or box.top >= box.bottom
        or box.top >= height
        or box.bottom <= 0
    )


@dataclass
class AndroidWorldCleaner(AndroidUITreeCleanerBase):
    """AndroidWorld's elements: visible leaves, nodes with a description, and scrollable nodes, on the screen.

    Windows in the forest's order, each window's nodes in the order its
    forwarder reads them (:func:`forwarder_tree`). A node becomes an element
    when it is visible to the user and is a leaf there, has a content
    description or is scrollable; elements are numbered in that order. An
    element whose bounds are empty or wholly off the screen is then dropped and
    keeps its number unused, as M3A and T3A skip it while counting it, so the
    numbers are the ones those agents print.
    """

    __registry_key__ = "androidworld"

    def clean(self, forest: AndroidAccessibilityForest, screen: ScreenSize) -> UITree:
        windows = []
        elements: list[UITreeElement] = []
        number = 0
        for window in forest.windows:
            kept: set[int] = set()
            for node, leaf in forwarder_tree(window.tree.nodes):
                if not (leaf or node.content_description or node.is_scrollable):
                    continue
                if not node.is_visible_to_user:
                    continue
                if on_screen(node.bounds_in_screen, screen):
                    kept.add(node.unique_id)
                    elements.append(
                        UITreeElement(index=number, window_id=window.id, unique_id=node.unique_id)
                    )
                number += 1
            nodes = tuple(node for node in window.tree.nodes if node.unique_id in kept)
            windows.append(
                window.model_copy(update={"tree": AndroidAccessibilityTree(nodes=nodes)})
            )
        return UITree(
            forest=AndroidAccessibilityForest(windows=tuple(windows)), elements=tuple(elements)
        )
