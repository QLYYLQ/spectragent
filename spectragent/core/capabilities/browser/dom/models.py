"""DOM internal models — dataclass-based for performance.

Reference: browser-use EnhancedDOMTreeNode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DOMRect:
    """Bounding rectangle in document or viewport coordinates."""

    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def contains(self, other: DOMRect) -> bool:
        return (
            self.x <= other.x
            and self.y <= other.y
            and self.x + self.width >= other.x + other.width
            and self.y + self.height >= other.y + other.height
        )


@dataclass(slots=True)
class EnhancedAXProperty:
    """Single accessibility property from the AX tree."""

    name: str
    value: str | bool | float | None = None


@dataclass(slots=True)
class DOMElementNode:
    """Single DOM element with full selector and layout metadata."""

    # Core DOM
    tag_name: str
    node_type: int = 1  # 1=ELEMENT, 3=TEXT, 9=DOCUMENT, 11=DOCUMENT_FRAGMENT
    node_value: str = ""  # TEXT_NODE text content (from DOM.getDocument nodeValue)
    attributes: dict[str, str] = field(default_factory=dict)
    text_content: str = ""
    children: list[DOMElementNode] = field(default_factory=list)

    # Super-selector
    node_id: int | None = None
    backend_node_id: int | None = None  # selector_map KEY (keyed by backend_node_id)
    frame_id: str | None = None
    target_id: str | None = None
    session_id: str | None = None

    # Layout
    bounds: DOMRect | None = None  # Document coords
    client_rect: DOMRect | None = None  # Viewport coords
    scroll_rects: DOMRect | None = None  # Scroll area (content size)
    is_visible: bool = True
    is_scrollable: bool = False

    # CSS computed styles
    computed_styles: dict[str, str] | None = None
    cursor_style: str | None = None

    # Accessibility
    ax_role: str | None = None
    ax_name: str | None = None
    ax_properties: list[EnhancedAXProperty] | None = None

    # Interactivity
    element_index: int | None = None  # Sequential index (for debugging / backward compat)
    is_interactive: bool = False
    has_js_click_listener: bool = False
    is_new: bool = False  # True if element was not in previous snapshot

    # Display control
    should_display: bool = True  # False = skip tag in serialization but recurse children

    # Compound components (virtual sub-components for range, select, etc.)
    _compound_children: list[dict[str, Any]] = field(default_factory=list)

    # Iframe / Shadow DOM
    content_document: DOMElementNode | None = field(default=None, repr=False)
    shadow_root_type: str | None = None
    shadow_roots: list[DOMElementNode] | None = None

    # Paint
    paint_order: int | None = None

    # Tree nav
    parent: DOMElementNode | None = field(default=None, repr=False)

    def descendants(self) -> list[DOMElementNode]:
        """Return all descendant nodes (depth-first)."""
        result: list[DOMElementNode] = []
        stack = list(self.children)
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(node.children)
        return result

    def interactive_elements(self) -> list[DOMElementNode]:
        """Return all interactive descendant elements."""
        return [n for n in self.descendants() if n.is_interactive]


DOMSelectorMap = dict[int, DOMElementNode]


@dataclass(slots=True)
class ScrollInfo:
    """Scroll state for a scrollable container or viewport."""

    scroll_x: float = 0.0
    scroll_y: float = 0.0
    scroll_width: float = 0.0
    scroll_height: float = 0.0
    viewport_width: float = 0.0
    viewport_height: float = 0.0

    @property
    def can_scroll_down(self) -> bool:
        return self.scroll_y + self.viewport_height < self.scroll_height

    @property
    def scroll_progress(self) -> float:
        max_scroll = self.scroll_height - self.viewport_height
        if max_scroll <= 0:
            return 1.0
        return self.scroll_y / max_scroll


@dataclass
class DOMState:
    """Complete DOM snapshot with selector map and scroll info."""

    dom_tree: DOMElementNode | None = None
    selector_map: DOMSelectorMap = field(default_factory=dict)
    scroll_info: ScrollInfo = field(default_factory=ScrollInfo)
    serialized_dom: str = ""

    def get_element(self, backend_node_id: int) -> DOMElementNode | None:
        """Look up an element by its backend_node_id in the selector map."""
        return self.selector_map.get(backend_node_id)
