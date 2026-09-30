"""CSS visibility detection for DOM elements.

Determines whether a DOM element is visually rendered based on CSS computed
styles and bounding geometry.  Used by both browser-use and skyvern cleaning
strategies.
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.dom.models import DOMElementNode

# Tags that never carry visible content.
NON_CONTENT_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "head",
        "meta",
        "link",
        "title",
        "noscript",
        "template",
    }
)

# SVG child elements that are purely decorative.
SVG_DECORATIVE_TAGS: frozenset[str] = frozenset(
    {
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "g",
        "defs",
        "clipPath",
        "mask",
        "pattern",
        "linearGradient",
        "radialGradient",
        "stop",
        "use",
        "symbol",
        "marker",
        "filter",
        "feGaussianBlur",
        "feOffset",
        "feBlend",
        "feComposite",
        "feFlood",
        "feMerge",
        "feMergeNode",
        "animate",
        "animateTransform",
        "set",
        "desc",
        "metadata",
    }
)


def is_visible(node: DOMElementNode) -> bool:
    """Return *True* if *node* should be considered visible.

    The ``is_visible`` flag is now set during tree construction via CSS
    computed-style checks (display, visibility, opacity, bounds).
    """
    # TEXT_NODE: visible if has non-empty text (> 1 char).
    if node.node_type == 3:
        return bool(
            node.node_value and node.node_value.strip() and len(node.node_value.strip()) > 1
        )

    # Non-content tags are always invisible.
    if node.tag_name.lower() in NON_CONTENT_TAGS:
        return False

    # Use the is_visible flag set during tree construction (CSS-based).
    return node.is_visible


def is_non_content_tag(tag_name: str) -> bool:
    """Return *True* if *tag_name* is a non-content element."""
    return tag_name.lower() in NON_CONTENT_TAGS


def is_svg_decorative(tag_name: str) -> bool:
    """Return *True* if *tag_name* is a decorative SVG child element."""
    return tag_name in SVG_DECORATIVE_TAGS
