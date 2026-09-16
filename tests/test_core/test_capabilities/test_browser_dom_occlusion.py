"""Paint-order occlusion: covered by something else, not by yourself.

The filter hides an element whose box is fully covered by opaque boxes painted
above it. Upstream browser-use counts the element's own descendants among
those boxes, so a control that draws its face with an inner element vanishes
from the selector map — reCAPTCHA's ``span#recaptcha-anchor`` (role=checkbox)
sits under its own white ``div.recaptcha-checkbox-border``, measured on the
live widget at 28x28 px, paint order 4 under 10. By default it does not;
``descendants_occlude=True`` is the upstream algorithm, unchanged.
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.dom.filters.occlusion import filter_by_paint_order
from spectragent.core.capabilities.browser.dom.models import DOMElementNode, DOMRect

_OPAQUE = {"background-color": "rgb(255, 255, 255)", "opacity": "1"}
_CLEAR = {"background-color": "rgba(0, 0, 0, 0)", "opacity": "1"}


def box(
    backend_id: int,
    rect: tuple[float, float, float, float],
    paint_order: int,
    *,
    opaque: bool = False,
    parent: DOMElementNode | None = None,
) -> DOMElementNode:
    node = DOMElementNode(
        tag_name="div",
        backend_node_id=backend_id,
        bounds=DOMRect(*rect),
        paint_order=paint_order,
        computed_styles=dict(_OPAQUE if opaque else _CLEAR),
    )
    if parent is not None:
        node.parent = parent
        parent.children.append(node)
    return node


def recaptcha_anchor() -> tuple[DOMElementNode, list[DOMElementNode]]:
    holder = box(1, (490, 265, 50, 50), 1)
    anchor = box(2, (501, 275, 28, 28), 4, parent=holder)
    border = box(3, (501, 275, 28, 28), 10, opaque=True, parent=anchor)
    return anchor, [holder, anchor, border]


def test_an_element_is_not_hidden_by_its_own_child() -> None:
    anchor, nodes = recaptcha_anchor()
    assert anchor.backend_node_id not in filter_by_paint_order(nodes)


def test_nor_by_a_deeper_descendant_or_several_of_them() -> None:
    button = box(1, (0, 0, 100, 40), 1)
    face = box(2, (0, 0, 60, 40), 3, parent=button)
    left = box(3, (0, 0, 60, 40), 5, opaque=True, parent=face)
    right = box(4, (60, 0, 40, 40), 5, opaque=True, parent=button)
    assert filter_by_paint_order([button, face, left, right]) == set()


def test_shadow_content_counts_as_the_hosts_own() -> None:
    host = box(1, (0, 0, 100, 40), 1)
    shadow_root = DOMElementNode(tag_name="#document-fragment", node_type=11, backend_node_id=2)
    shadow_root.parent = host
    host.shadow_roots = [shadow_root]
    inner = box(3, (0, 0, 100, 40), 6, opaque=True, parent=shadow_root)
    assert filter_by_paint_order([host, shadow_root, inner]) == set()


def test_something_else_painted_on_top_still_hides_it() -> None:
    anchor, nodes = recaptcha_anchor()
    modal = box(9, (0, 0, 1280, 720), 50, opaque=True)
    hidden = filter_by_paint_order([*nodes, modal])
    assert anchor.backend_node_id in hidden
    assert 3 in hidden  # the border too: nothing of the widget shows


def test_coverage_is_judged_without_the_subtree_not_merely_ignoring_it() -> None:
    """Own child covers the left half, an unrelated overlay the right half.

    Together they cover the element, but only the overlay counts — and it
    leaves half of the element exposed.
    """
    parent = box(1, (0, 0, 100, 40), 1)
    own_half = box(2, (0, 0, 50, 40), 5, opaque=True, parent=parent)
    overlay_half = box(3, (50, 0, 50, 40), 7, opaque=True)
    assert parent.backend_node_id not in filter_by_paint_order([parent, own_half, overlay_half])

    overlay_rest = box(4, (0, 0, 50, 40), 8, opaque=True)
    assert parent.backend_node_id in filter_by_paint_order(
        [parent, own_half, overlay_half, overlay_rest]
    )


def test_same_paint_order_never_occludes() -> None:
    a = box(1, (0, 0, 50, 50), 3, opaque=True)
    b = box(2, (0, 0, 50, 50), 3, opaque=True)
    assert filter_by_paint_order([a, b]) == set()


def test_a_zero_area_element_under_an_overlay_is_still_hidden() -> None:
    source = box(1, (10, 10, 0, 0), 1)
    overlay = box(2, (0, 0, 100, 100), 4, opaque=True)
    assert filter_by_paint_order([source, overlay]) == {1}


def test_upstream_counts_descendants() -> None:
    anchor, nodes = recaptcha_anchor()
    assert filter_by_paint_order(nodes, descendants_occlude=True) == {anchor.backend_node_id}

    button = box(1, (0, 0, 100, 40), 1)
    face = box(2, (0, 0, 60, 40), 3, parent=button)
    left = box(3, (0, 0, 60, 40), 5, opaque=True, parent=face)
    right = box(4, (60, 0, 40, 40), 5, opaque=True, parent=button)
    assert filter_by_paint_order([button, face, left, right], descendants_occlude=True) == {1, 2}


def test_both_modes_agree_when_nothing_is_a_descendant() -> None:
    element = box(1, (10, 10, 20, 20), 1)
    left = box(2, (0, 0, 20, 40), 4, opaque=True)
    right = box(3, (20, 0, 20, 40), 4, opaque=True)
    nodes = [element, left, right]
    assert (
        filter_by_paint_order(nodes)
        == filter_by_paint_order(nodes, descendants_occlude=True)
        == {1}
    )


def test_boxes_far_apart_on_the_page_are_bucketed_correctly() -> None:
    """Overlay and element meet only across a cell edge of the spatial index."""
    element = box(1, (250, 250, 20, 20), 1)
    overlay = box(2, (240, 240, 40, 40), 5, opaque=True)
    far = box(3, (5000, 9000, 10, 10), 1)
    assert filter_by_paint_order([element, overlay, far]) == {1}
