"""Interactive element detection — shared by both cleaning strategies.

Ported from browser-use ``ClickableElementDetector`` with:
- Label/span wrapper detection (has_form_control_descendant)
- AX property-based detection (focusable, editable, settable, checked, etc.)
- Expanded search hints + data-* attribute scanning
- cursor:pointer from computed_styles
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.dom.models import DOMElementNode

# Native interactive HTML tags.
INTERACTIVE_TAGS: frozenset[str] = frozenset(
    {
        "a",
        "button",
        "input",
        "select",
        "textarea",
        "details",
        "summary",
        "option",
        "optgroup",
    }
)

# ARIA roles that imply interactivity.
INTERACTIVE_ARIA_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "link",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "checkbox",
        "radio",
        "tab",
        "slider",
        "switch",
        "combobox",
        "textbox",
        "searchbox",
        "spinbutton",
        "option",
        "treeitem",
        "gridcell",
        "cell",
        "row",
        "scrollbar",
        "progressbar",
        "listbox",
        "search",
    }
)

# HTML attributes that imply interactivity.
_INTERACTIVE_ATTRS: frozenset[str] = frozenset(
    {
        "onclick",
        "onmousedown",
        "onmouseup",
        "onkeydown",
        "onkeyup",
        "tabindex",
        "contenteditable",
    }
)

# A frame at least this large (CSS px, each side) is a target of its own,
# whatever is inside it — browser-use's rule. It is how an agent reaches what
# a frame's elements cannot give it: typing into a rich-text editor whose
# document *is* the editable surface (``<body contenteditable>``, designMode),
# or scrolling the frame.
_FRAME_MIN_PX = 100

# Smallest box a frame whose document was not captured needs to be offered
# as a target: the frame is then its only handle, and its size is no reason
# to hide it — a 304x78 captcha widget is as real as a 600x400 embed. Same
# floor as the icon heuristic below; it keeps out tracking pixels.
_OPAQUE_FRAME_MIN_PX = 10

# Class/ID substrings that suggest a search widget.
_SEARCH_HINTS: frozenset[str] = frozenset(
    {
        "search",
        "magnify",
        "glass",
        "lookup",
        "find",
        "query",
        "search-icon",
        "search-btn",
        "search-button",
        "searchbox",
    }
)


def has_form_control_descendant(node: DOMElementNode, max_depth: int = 2) -> bool:
    """Detect nested form controls within limited depth (handles label/span wrappers)."""
    if max_depth <= 0:
        return False
    for child in node.children:
        if child.tag_name.lower() in ("input", "select", "textarea"):
            return True
        if has_form_control_descendant(child, max_depth - 1):
            return True
    return False


def is_interactive(node: DOMElementNode) -> bool:
    """Return *True* if *node* should be treated as an interactive element.

    Checks (roughly ordered from cheapest to most expensive):
    1. Skip html/body.
    2. JS click listener flag.
    3. Frame owners: large ones, and small ones whose document is not in the tree.
    4. Label/span wrapper detection.
    5. Search widget heuristic.
    6. AX property-based detection (disabled, hidden, focusable, editable, etc.).
    7. Native interactive tag.
    8. Interactive HTML attributes (onclick, tabindex, ...).
    9. ARIA role (from attributes or accessibility tree).
    10. Icon-sized clickable elements.
    11. Cursor:pointer style hint.
    """
    # Only ELEMENT_NODE can be interactive.
    if node.node_type != 1:
        return False

    tag = node.tag_name.lower()

    # Skip root elements.
    if tag in ("html", "body"):
        return False

    # NOTE: No zero-area size check here — matching original browser-use behavior.
    # Zero-area elements can still be interactive (e.g. invisible clickable overlays,
    # <source> elements with cursor:pointer inherited from parent).

    # 0. AX disabled/hidden — check first (cheapest negative signal).
    if node.ax_properties:
        for prop in node.ax_properties:
            if prop.name == "disabled" and prop.value:
                return False
            if prop.name == "hidden" and prop.value:
                return False

    # 1. JS click listener (set by CDP ``getEventListeners``).
    if node.has_js_click_listener:
        return True

    # 2. Frame owner. Its document's elements are indexed where they are
    #    drawn (the cleaner walks frame documents), and they are what a
    #    click on a widget should aim at — a frame's centre is not the
    #    checkbox at its left edge. The frame itself is a target too when it
    #    is large (browser-use's rule, and the only handle on an editor whose
    #    body is the editable surface), or when its document is not in the
    #    tree at all — not captured, timed out — and it is the only handle.
    #    Any other frame falls through to the generic checks below.
    if tag in ("iframe", "frame"):
        if _has_min_size(node, _FRAME_MIN_PX, _FRAME_MIN_PX):
            return True
        if node.content_document is None and _has_min_size(
            node, _OPAQUE_FRAME_MIN_PX, _OPAQUE_FRAME_MIN_PX
        ):
            return True

    # 3. Label wrapper detection — skip labels that proxy via "for".
    if tag == "label":
        if node.attributes.get("for"):
            return False
        if has_form_control_descendant(node, max_depth=2):
            return True

    # 4. Span wrapper detection.
    if tag == "span" and has_form_control_descendant(node, max_depth=2):
        return True

    # 5. Search widget heuristic.
    if _looks_like_search(node):
        return True

    # 6. AX property-based positive detection.
    if node.ax_properties:
        for prop in node.ax_properties:
            # Direct interactiveness indicators
            if prop.name in ("focusable", "editable", "settable") and prop.value:
                return True
            # Interactive state properties (presence indicates interactive widget)
            if prop.name in ("checked", "expanded", "pressed", "selected"):
                return True
            # Form-related interactiveness
            if prop.name in ("required", "autocomplete") and prop.value:
                return True
            # Keyboard shortcuts indicate interactive element
            if prop.name == "keyshortcuts" and prop.value:
                return True

    # 7. Native interactive tag.
    if tag in INTERACTIVE_TAGS:
        return True

    # 8. Interactive attributes.
    attrs = node.attributes
    for attr in _INTERACTIVE_ATTRS:
        if attr in attrs:
            return True

    # 9. ARIA role — from HTML attribute or AX tree.
    role = _effective_role(node)
    if role and role.lower() in INTERACTIVE_ARIA_ROLES:
        return True

    # 10. Small icon element with click-related attrs.
    if _is_clickable_icon(node):
        return True

    # 11. Cursor:pointer hint (from computed styles).
    # Left as a guard rather than collapsed into the return: this is the last
    # item of the numbered list above, and it keeps the shape items 1-10 have
    # so item 12 gets written the same way.
    if node.cursor_style == "pointer":  # noqa: SIM103
        return True

    return False


def is_scrollable_container(node: DOMElementNode) -> bool:
    """Return *True* if *node* is a meaningful scrollable container.

    A scrollable container gets an interactive index only when it has no
    interactive descendants (otherwise the children are the targets).
    """
    if not node.is_scrollable:
        return False
    # Check if it has interactive children — if so, skip assigning index.
    for child in node.children:
        if child.is_interactive or child.element_index is not None:
            return False
    return True


def is_dropdown_container(node: DOMElementNode) -> bool:
    """Return *True* if *node* is a dropdown / combobox container.

    Dropdown containers always get an interactive index regardless of
    descendants.
    """
    role = _effective_role(node)
    if role and role.lower() in ("combobox", "listbox"):
        return True
    tag = node.tag_name.lower()
    return tag == "select"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _effective_role(node: DOMElementNode) -> str | None:
    """Return the effective ARIA role of *node*."""
    # Explicit role attribute takes precedence.
    role = node.attributes.get("role")
    if role:
        return role
    # AX tree role.
    if node.ax_role:
        return node.ax_role
    return None


def _has_min_size(node: DOMElementNode, min_w: float, min_h: float) -> bool:
    if node.bounds is None:
        return False
    return node.bounds.width >= min_w and node.bounds.height >= min_h


def _looks_like_search(node: DOMElementNode) -> bool:
    """Heuristic: element class/id/data-* contains search-related keywords."""
    attrs = node.attributes

    # Check class names.
    class_list = attrs.get("class", "").lower()
    if any(hint in class_list for hint in _SEARCH_HINTS):
        return True

    # Check id.
    element_id = attrs.get("id", "").lower()
    if any(hint in element_id for hint in _SEARCH_HINTS):
        return True

    # Check data attributes for search functionality.
    for attr_name, attr_value in attrs.items():
        if attr_name.startswith("data-") and any(
            hint in attr_value.lower() for hint in _SEARCH_HINTS
        ):
            return True

    return False


def _is_clickable_icon(node: DOMElementNode) -> bool:
    """Small element (10-50px) with click-related attributes."""
    if node.bounds is None:
        return False
    w, h = node.bounds.width, node.bounds.height
    if not (10 <= w <= 50 and 10 <= h <= 50):
        return False
    # Match original ClickableElementDetector icon_attributes set.
    attrs = node.attributes
    return bool(
        attrs.get("class")
        or attrs.get("role")
        or attrs.get("onclick")
        or attrs.get("data-action")
        or attrs.get("aria-label")
    )
