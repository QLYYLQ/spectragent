"""The cleaned UI tree: the nodes a cleaner kept of a captured forest, and the numbers it gave them.

``docs/architecture_design.md`` §11.2 ("界面树清洗与序列化"). A cleaner reads the
forest the tree source captured (``device/forest.py``, android_env's
``AndroidAccessibilityForest`` plus ``is_important``) and returns a
:class:`UITree`: the same forest holding only the nodes it kept, and the
elements it numbered. A serializer prints the elements; ``click`` and
``scroll`` find an element's bounds by its number, so the bounds and the
number-to-node map stay here even when the printed text carries no bounds.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spectragent.core.capabilities.android.device.forest import (
    AndroidAccessibilityForest,
    AndroidAccessibilityNodeInfo,
)


class UITreeElement(BaseModel):
    """One element a cleaner numbered: its index, and the node it stands for, by window and node id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    window_id: int
    unique_id: int


class UITree(BaseModel):
    """A cleaned tree: the captured forest holding only the nodes a cleaner kept, and their numbers.

    Every window of the capture stays as it was captured; each window's tree
    holds the nodes the cleaner kept, in their captured order, with their
    fields as captured, so a kept node's ``child_ids`` may name nodes the
    cleaner dropped. ``elements`` lists the numbered ones in index order; a
    cleaner may leave numbers unused (``androidworld`` does, as AndroidWorld
    does), never reuse one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    forest: AndroidAccessibilityForest = AndroidAccessibilityForest()
    elements: tuple[UITreeElement, ...] = ()

    @model_validator(mode="after")
    def _elements_name_kept_nodes(self) -> Self:
        kept = {
            (window.id, node.unique_id)
            for window in self.forest.windows
            for node in window.tree.nodes
        }
        last = -1
        for element in self.elements:
            if element.index <= last:
                raise ValueError(
                    f"element {element.index} follows element {last}: elements are listed in "
                    "increasing index order, each index once"
                )
            if (element.window_id, element.unique_id) not in kept:
                raise ValueError(
                    f"element {element.index} names node {element.unique_id} of window "
                    f"{element.window_id}, which the tree does not hold"
                )
            last = element.index
        return self

    def element_nodes(self) -> list[tuple[int, AndroidAccessibilityNodeInfo]]:
        """Each element's index and node, in index order."""
        nodes = {
            (window.id, node.unique_id): node
            for window in self.forest.windows
            for node in window.tree.nodes
        }
        return [
            (element.index, nodes[element.window_id, element.unique_id])
            for element in self.elements
        ]

    def node(self, index: int) -> AndroidAccessibilityNodeInfo | None:
        """The node element *index* stands for; ``None`` when no element has that index."""
        for number, node in self.element_nodes():
            if number == index:
                return node
        return None
