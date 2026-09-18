"""Bounding-box propagation filter.

Ported from browser-use's Stage 4 ``_apply_bounding_box_filtering``.

When a "propagating" element (``<a>``, ``<button>``, ``<div role=button>``,
etc.) contains child elements that are >=99 % inside the parent's bounding
box, the children are marked as *excluded_by_parent*.  This reduces noise
from deeply nested button/link internals.

Exceptions (never excluded even if contained):
- Form elements: input, select, textarea, label
- Other propagating elements (nested buttons)
- Elements with ``onclick``
- Elements with meaningful ``aria-label``
- Elements with interactive ARIA roles
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.dom.filters.interactivity import INTERACTIVE_ARIA_ROLES
from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMRect

# Tags whose bounds propagate to descendants.
PROPAGATING_TAGS: frozenset[str] = frozenset({"a", "button"})

# (tag, role) combos that propagate.
PROPAGATING_ROLES: frozenset[tuple[str, str]] = frozenset({
    ("div", "button"),
    ("span", "button"),
    ("div", "combobox"),
    ("span", "combobox"),
    ("input", "combobox"),
})

# Children with these tags are never excluded.
EXEMPT_CHILD_TAGS: frozenset[str] = frozenset({
    "input", "select", "textarea", "label",
})

_CONTAINMENT_THRESHOLD = 0.99


def propagate_parent_bounds(root: DOMElementNode) -> list[DOMElementNode]:
    """Walk the composed tree, return the nodes excluded by parent bounds.

    Does *not* mutate ``root``; returns the nodes that should be hidden.
    Nodes, not backend node ids: those repeat across the page's targets.

    Shadow-root content propagates like light DOM — it renders inside its
    host. A frame's document starts afresh: an ``<iframe>`` inside a link is
    not the link's label, and its content is a page of its own.
    """
    excluded: list[DOMElementNode] = []
    _walk(root, None, excluded)
    return excluded


def _walk(
    node: DOMElementNode,
    active_parent_bounds: DOMRect | None,
    excluded: list[DOMElementNode],
) -> None:
    """Recurse depth-first, propagating bounds from propagating parents."""
    bounds = active_parent_bounds

    # Check if this node is a propagating element.
    if _is_propagating(node) and node.bounds is not None:
        bounds = node.bounds

    # If there's an active parent, check containment.
    if (
        bounds is not None
        and node.bounds is not None
        and bounds is not node.bounds
        and _is_contained(node.bounds, bounds)
        and not _is_exempt(node)
        and node.backend_node_id is not None
    ):
        excluded.append(node)

    for child in node.children:
        _walk(child, bounds, excluded)
    for shadow_root in node.shadow_roots or ():
        _walk(shadow_root, bounds, excluded)
    if node.content_document is not None:
        _walk(node.content_document, None, excluded)


def _is_propagating(node: DOMElementNode) -> bool:
    tag = node.tag_name.lower()
    if tag in PROPAGATING_TAGS:
        return True
    role = node.attributes.get("role", "")
    return (tag, role) in PROPAGATING_ROLES


def _is_contained(child_b: DOMRect, parent_b: DOMRect) -> bool:
    """Return *True* if child is >= 99% inside parent."""
    # Intersection area.
    ix1 = max(child_b.x, parent_b.x)
    iy1 = max(child_b.y, parent_b.y)
    ix2 = min(child_b.x + child_b.width, parent_b.x + parent_b.width)
    iy2 = min(child_b.y + child_b.height, parent_b.y + parent_b.height)

    if ix2 <= ix1 or iy2 <= iy1:
        return False

    intersection_area = (ix2 - ix1) * (iy2 - iy1)
    child_area = child_b.width * child_b.height
    if child_area <= 0:
        return False

    return intersection_area / child_area >= _CONTAINMENT_THRESHOLD


def _is_exempt(node: DOMElementNode) -> bool:
    """Return *True* if the node should never be excluded by parent bounds."""
    tag = node.tag_name.lower()
    if tag in EXEMPT_CHILD_TAGS:
        return True
    if tag in PROPAGATING_TAGS:
        return True
    attrs = node.attributes
    if "onclick" in attrs:
        return True
    if attrs.get("aria-label"):
        return True
    role = attrs.get("role", "").lower()
    return role in INTERACTIVE_ARIA_ROLES
